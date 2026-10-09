# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件发现：扫描插件目录并解析 plugin.json 元数据。
#
# 公开接口：
#   - class PluginDiscovery — 负责插件候选项的发现、依赖解析与实例化加载。
# ============================================================

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

from ECL.plugins.dependencies import (
    DependencyResolution,
    PluginDependencyInfo,
    parse_dependency,
    resolve_dependencies,
)
from ECL.plugins.plugin import Plugin

from .base import _PluginState


class PluginDiscovery(_PluginState):
    """
    负责插件候选项的发现、依赖解析与实例化加载。
    """

    def _collect_candidates(self, base_dir: Path, is_system: bool) -> list[dict[str, Any]]:
        # 扫描目录，读取各插件的 plugin.json 并构造候选条目列表。
        candidates: list[dict[str, Any]] = []
        if not base_dir.is_dir():
            return candidates
        for plugin_dir in sorted(base_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            metadata_path = plugin_dir / "plugin.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.logger.warning("插件元数据解析失败: %s", metadata_path)
                continue
            name = metadata.get("name")
            if not name:
                continue
            candidates.append(
                {
                    "name": name,
                    "plugin_dir": plugin_dir,
                    "metadata_path": metadata_path,
                    "metadata": metadata,
                    "is_system": is_system,
                }
            )
        return candidates

    def _resolve_candidate_dependencies(self, candidates: list[dict[str, Any]]) -> DependencyResolution:
        # 根据候选插件元数据解析依赖关系与加载顺序。
        seen: set[str] = set()
        infos: list[PluginDependencyInfo] = []
        for candidate in candidates:
            name = candidate["name"]
            if name in seen:
                self.logger.warning("插件 %s 重复，跳过", name)
                continue
            seen.add(name)
            metadata = candidate["metadata"]
            deps_meta = metadata.get("dependencies", {})
            deps: list[Any] = []
            for dep_name, dep_value in deps_meta.items():
                req = parse_dependency(dep_name, dep_value)
                if req is None:
                    self.logger.warning("插件 %s 的依赖 %s 格式无效", name, dep_name)
                    continue
                deps.append(req)
            infos.append(
                PluginDependencyInfo(
                    name=name,
                    version=metadata.get("version", "0.0.0"),
                    dependencies=deps,
                    is_system=candidate["is_system"],
                )
            )
        return resolve_dependencies(infos)

    def _load_plugins_in_order(self, candidates: list[dict[str, Any]], load_order: list[str]) -> None:
        # 按依赖拓扑顺序实例化并加载插件；被禁用的插件直接跳过。
        candidate_map = {c["name"]: c for c in candidates}
        for name in self._disabled_plugins:
            candidate = candidate_map.get(name)
            if candidate is None:
                continue
            self._permission_manager.register_plugin_permissions(name, candidate["metadata"].get("permissions", []))
            self._status[name] = "disabled"
        for name in load_order:
            if name in self._disabled_plugins:
                self.logger.info("插件 %s 已被禁用，跳过加载", name)
                continue
            candidate = candidate_map.get(name)
            if candidate is None:
                continue
            self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], candidate["is_system"])

    def _load_plugin(self, plugin_dir: Path, metadata_path: Path, is_system: bool) -> None:
        # 加载单个插件：先注册权限声明、读取配置，再实例化并调用 on_load 钩子。
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.logger.warning("插件元数据解析失败: %s", metadata_path)
            return
        name = metadata.get("name")
        if not name:
            return
        if name in self._plugins:
            self.logger.warning("插件 %s 重复，跳过", name)
            return
        entry_point = metadata.get("entry_point", "main:Plugin")
        self.logger.debug(
            "开始加载插件：名称：%s；入口：%s；系统插件：%s；目录：%s",
            name,
            entry_point,
            ("是" if is_system else "否"),
            plugin_dir,
        )
        # 在实例化之前注册权限声明，使 __init__ 中的装饰器注册能立即生效
        permissions_meta = metadata.get("permissions", [])
        self._permission_manager.register_plugin_permissions(name, permissions_meta)
        self._load_plugin_config(name, metadata)

        self._status[name] = "loading"
        try:
            self._prepare_package_load(name)
            plugin = self._create_instance(name, plugin_dir, metadata, entry_point, is_system)
        except PermissionError as exc:
            detail = str(exc)
            self.logger.error("插件 %s 权限声明不足，无法实例化: %s", name, detail)
            self._status[name] = "permission_denied"
            self._plugin_errors[name] = detail
            return
        except Exception as exc:
            self.logger.exception("插件 %s 实例化失败", name)
            self._status[name] = "error"
            self._plugin_errors[name] = str(exc)
            return
        self._plugins[name] = plugin
        self._status[name] = "loaded"
        if not self._call_plugin_hook(plugin, "on_load", fail_status="error"):
            return
        self.logger.info("插件已加载: %s v%s", name, plugin.version)

    def _create_instance(
        self, name: str, plugin_dir: Path, metadata: dict[str, Any], entry_point: str, is_system: bool
    ) -> Plugin:
        """
        校验包内入口并在主进程创建使用宿主构造参数的插件实例。

        entry_point 为文件名或“文件名:类名”；执行模块前确认文件仍在插件目录内，
        不再识别无参数 SDK 或使用命令适配器。入口代码仍具有本进程能力。
        """
        parts = entry_point.split(":", 1)
        module_name = parts[0]
        class_name = parts[1] if len(parts) > 1 else "Plugin"
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*", module_name) is None
            or not class_name.isidentifier()
        ):
            raise ValueError("插件入口必须是包内 Python 文件和合法类名")
        main_py = plugin_dir / f"{module_name}.py"
        if not main_py.resolve().is_relative_to(plugin_dir.resolve()):
            raise ValueError("插件入口文件超出插件目录")
        if not main_py.is_file():
            raise FileNotFoundError(f"入口文件不存在: {main_py}")
        # 使用隔离的模块名避免命名冲突
        spec = importlib.util.spec_from_file_location(f"plugin_{name}", main_py)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        # 不写归档代码目录，也不临时修改影响其他线程的全局字节码开关。
        source = spec.loader.get_source(spec.name)
        exec(compile(source, str(main_py), "exec", dont_inherit=True), module.__dict__)
        plugin_class = getattr(module, class_name)
        return plugin_class(self, plugin_dir, metadata, is_system)

    def _call_plugin_hook(self, plugin: Plugin, method_name: str, *, fail_status: str | None = None) -> bool:
        # 调用插件启用或关闭时的回调；失败时记日志，并按需恢复原状态。
        started = perf_counter()
        succeeded = False
        self.logger.debug("开始执行插件钩子：插件：%s；钩子：%s", plugin.name, method_name)
        try:
            getattr(plugin, method_name)()
            succeeded = True
            return True
        except PermissionError as exc:
            detail = str(exc)
            self.logger.error("插件 %s %s 权限不足: %s", plugin.name, method_name, detail)
            self._status[plugin.name] = "permission_denied"
            self._plugin_errors[plugin.name] = detail
            return False
        except Exception as exc:
            self.logger.exception("插件 %s %s 失败", plugin.name, method_name)
            self._plugin_errors[plugin.name] = str(exc)
            if fail_status is not None:
                self._status[plugin.name] = fail_status
            return False
        finally:
            duration = perf_counter() - started
            if duration >= 2.0:
                self.logger.warning(
                    "插件钩子执行缓慢：插件：%s；钩子：%s；成功：%s",
                    plugin.name,
                    method_name,
                    ("是" if succeeded else "否"),
                )
            else:
                self.logger.debug(
                    "插件钩子执行完成：插件：%s；钩子：%s；成功：%s",
                    plugin.name,
                    method_name,
                    ("是" if succeeded else "否"),
                )
