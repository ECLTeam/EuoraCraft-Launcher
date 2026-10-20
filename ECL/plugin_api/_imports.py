# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在 Worker 中优先解析批准的代码和锁定依赖，不替换已加载模块。
#
# 公开接口：
#   - class DependencySnapshot — 共享依赖目录的有界完整性复核。
#   - class LockedImports — 插件导入和依赖元数据的独立解析边界。
# ============================================================

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, PathFinder
from importlib.metadata import Distribution, DistributionFinder, MetadataPathFinder
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import ClassVar, Protocol

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugin_api.snapshot import CodeSnapshot


class _DllDirectory(Protocol):
    def close(self) -> None: ...


class DependencySnapshot(CodeSnapshot):
    """
    沿用依赖池的数量和大小上限，包含就绪、安装清单及库文件。
    """

    max_files: ClassVar[int] = 100000
    max_bytes: ClassVar[int] = 1024 * 1024 * 1024


class LockedImports(MetaPathFinder):
    """
    在冻结导入器之前解析本插件模块，避免宿主打包版本抢占。

    只负责当前 Worker 的导入，不赋予磁盘只读权限或禁止任意 Python OS API。
    """

    protected_imports: ClassVar[frozenset[str]] = frozenset(
        {"ECL", "pytauri", "pytauri_wheel", "pytauri_plugins", "pytauri_utils"}
    )

    def __init__(self, roots: Mapping[str, Path], site_path: Path | None, packages: frozenset[str]) -> None:
        """
        保存受信加载许可中的导入归属，不允许提供标准库或 SDK 名称。

        :param roots: 顶层模块到批准目录的映射
        :param site_path: 依赖池的 site-packages 目录
        :param packages: 锁定的发行包名称，用于优先解析 metadata
        :raises ProtocolError: 名称冲突或模块已被加载时抛出
        """
        reserved = self.protected_imports | sys.stdlib_module_names
        if any(not name.isidentifier() or name in reserved or name in sys.modules for name in roots):
            raise ProtocolError("插件依赖与受保护或已加载模块冲突", "PLUGIN_DEPENDENCY_CONFLICT")
        self._roots: Mapping[str, Path] = MappingProxyType({name: root.resolve() for name, root in roots.items()})
        self._site_path = site_path.resolve() if site_path is not None else None
        self._packages = frozenset(self._package_name(name) for name in packages)
        self._dll_directories: list[_DllDirectory] = []
        self._is_installed = False

    def install(self) -> None:
        """
        在当前 Worker 安装解析器并保留原生库搜索句柄。

        :raises ProtocolError: 解析器重复安装或库目录无法打开时抛出
        """
        if self._is_installed:
            raise ProtocolError("插件导入上下文已经安装")
        if sys.platform == "win32" and self._site_path is not None:
            paths = [self._site_path]
            paths.extend(path for path in self._site_path.iterdir() if path.is_dir() and path.name.endswith(".libs"))
            try:
                for path in paths:
                    self._dll_directories.append(os.add_dll_directory(str(path)))
            except OSError as exc:
                self.close()
                raise ProtocolError("无法设置插件原生库搜索目录", "PLUGIN_DEPENDENCY_CONFLICT") from exc
        sys.meta_path.insert(0, self)
        self._is_installed = True

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        """
        仅从批准目录解析受控模块，缺失时拒绝回退冻结宿主版本。

        :param fullname: Python 请求的模块名
        :param path: 父包的搜索路径
        :param target: 标准导入协议的重载对象
        :return: 本插件的解析结果；非本插件模块返回 None
        :raises ModuleNotFoundError: 锁声明的模块无法从批准目录解析时抛出
        """
        root_path = self._roots.get(fullname.split(".", 1)[0])
        if root_path is None:
            return None
        search_paths = (
            [str(root_path)]
            if path is None
            else [item for item in path if Path(item).resolve().is_relative_to(root_path)]
        )
        spec = PathFinder.find_spec(fullname, search_paths, target)
        if spec is None or (spec.origin is not None and not Path(spec.origin).resolve().is_relative_to(root_path)):
            raise ModuleNotFoundError("锁定模块无法从批准目录解析", name=fullname)
        if spec.submodule_search_locations and any(
            not Path(item).resolve().is_relative_to(root_path) for item in spec.submodule_search_locations
        ):
            raise ModuleNotFoundError("锁定包搜索路径超出批准目录", name=fullname)
        return spec

    def find_distributions(self, context: DistributionFinder.Context | None = None) -> Iterator[Distribution]:
        """
        优先提供批准锁的包元数据，不能误报宿主中另一版本。

        :param context: 标准库元数据查询上下文
        :return: 仅来自批准依赖目录的发行记录
        """
        if self._site_path is None:
            return iter(())
        name = context.name if context is not None else None
        if name is not None and self._package_name(name) not in self._packages:
            return iter(())
        query = DistributionFinder.Context(name=name, path=[str(self._site_path)])
        return (
            item
            for item in MetadataPathFinder.find_distributions(query)
            if self._package_name(item.metadata["Name"]) in self._packages
        )

    @staticmethod
    def _package_name(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    def close(self) -> None:
        """
        移除本解析器并释放 DLL 搜索句柄，不清空模块缓存。
        """
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for handle in self._dll_directories:
            handle.close()
        self._dll_directories.clear()
        self._is_installed = False
