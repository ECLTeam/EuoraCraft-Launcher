# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：生成宿主生产依赖清单并检查主解释器中插件依赖的版本及导入冲突。
#
# 公开接口：
#   - class HostDependencyPolicy — 宿主保护、插件组合预检和进程内依赖占用。
#   - build_host_dependency_manifest() — 生成冻结产物的实际生产依赖清单。
# ============================================================

from __future__ import annotations

import importlib.metadata
import json
import sys
import tomllib
from pathlib import Path
from threading import RLock

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.environment_pool import InstalledPackage, PluginDependencyDirectory
from ECL.utils import atomic_write_text


def build_host_dependency_manifest(output_path: Path | None = None) -> dict[str, object]:
    """
    从项目生产需求及实际安装元数据生成版本和导入归属清单。

    不把 pytest、编译器等开发工具当作宿主依赖；解析 extras 和传递需求。

    :param output_path: 可选输出清单位置；提供时原子写入
    :return: 可随冻结产物携带的 JSON 结构
    :raises PluginDependencyError: 生产依赖缺失或元数据无法读取
    """
    project_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))
        pending = [Requirement(item) for item in project["project"]["dependencies"]]
        imports_by_distribution: dict[str, set[str]] = {}
        for import_name, distributions in importlib.metadata.packages_distributions().items():
            for distribution_name in distributions:
                imports_by_distribution.setdefault(canonicalize_name(distribution_name), set()).add(import_name)
        entries: dict[str, dict[str, object]] = {}
        seen: set[tuple[str, frozenset[str]]] = set()
        while pending:
            requirement = pending.pop()
            name = canonicalize_name(requirement.name)
            extras = frozenset(requirement.extras)
            if (name, extras) in seen:
                continue
            seen.add((name, extras))
            distribution = importlib.metadata.distribution(name)
            entries[name] = {
                "name": name,
                "version": distribution.version,
                "imports": sorted(imports_by_distribution.get(name, set())),
            }
            for raw in distribution.requires or ():
                dependency = Requirement(raw)
                if dependency.marker is None or any(
                    dependency.marker.evaluate({"extra": extra}) for extra in {"", *extras}
                ):
                    pending.append(dependency)
        payload: dict[str, object] = {"format_version": 1, "packages": [entries[name] for name in sorted(entries)]}
        if output_path is not None:
            atomic_write_text(output_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return payload
    except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError) as exc:
        raise PluginDependencyError("宿主生产依赖清单无法生成") from exc


class HostDependencyPolicy:
    """
    拒绝共享解释器里的版本覆盖，不伪装成插件私有导入环境。

    已登记的依赖占用保持到进程结束；关闭管理器或禁用插件不清空模块缓存。
    """

    _process_packages: dict[str, InstalledPackage] = {}
    _process_imports: dict[str, str] = {}
    _process_paths: set[str] = set()
    _lock: RLock = RLock()

    def __init__(self, resource_path: Path) -> None:
        """
        从冻结产物的清单或源码环境的生产需求建立宿主保护基线。

        :param resource_path: 启动器资源根目录
        :raises PluginDependencyError: 冻结产物缺少或损坏宿主清单
        """
        manifest_path = resource_path / "resources" / "plugin_host_dependencies.json"
        try:
            if manifest_path.is_file():
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            elif getattr(sys, "frozen", False) or "__compiled__" in globals():
                raise PluginDependencyError("冻结启动器缺少宿主依赖清单")
            else:
                payload = build_host_dependency_manifest()
            if (
                not isinstance(payload, dict)
                or payload.get("format_version") != 1
                or not isinstance(payload.get("packages"), list)
            ):
                raise PluginDependencyError("宿主依赖清单格式无效")
            self.packages: dict[str, str] = {}
            self.imports: dict[str, str] = {}
            for item in payload["packages"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("version"), str)
                    or not isinstance(item.get("imports"), list)
                    or any(not isinstance(name, str) for name in item["imports"])
                ):
                    raise PluginDependencyError("宿主依赖清单字段无效")
                name = canonicalize_name(item["name"])
                self.packages[name] = str(Version(item["version"]))
                for import_name in item["imports"]:
                    self.imports[import_name] = name
        except PluginDependencyError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise PluginDependencyError("宿主依赖清单无法读取") from exc

    def conflict(self, directory: PluginDependencyDirectory | None) -> str | None:
        """
        检查宿主、标准库、当前导入和已经占用的依赖版本。

        :param directory: 候选依赖目录；无依赖时为 None
        :return: 可显示的冲突原因；兼容时为 None
        """
        if directory is None:
            return None
        with self._lock:
            owners: dict[str, str] = {}
            for package in directory.packages:
                host_version = self.packages.get(package.name)
                if host_version is not None and Version(host_version) != Version(package.version):
                    return f"依赖冲突: {package.name}=={package.version} 与宿主 {host_version} 不兼容"
                occupied = self._process_packages.get(package.name)
                if occupied is not None and Version(occupied.version) != Version(package.version):
                    return f"依赖冲突: {package.name}=={package.version} 与本进程已占用版本 {occupied.version} 不兼容，请重启"
                for import_name in package.imports:
                    if import_name in owners and owners[import_name] != package.name:
                        return f"导入冲突: {package.name} 与 {owners[import_name]} 都提供 {import_name}"
                    owners[import_name] = package.name
                    if import_name in sys.stdlib_module_names or import_name in {
                        "ECL",
                        "ecl_plugin_sdk",
                        "pytauri",
                        "pytauri_wheel",
                        "pytauri_plugins",
                        "pytauri_utils",
                    }:
                        return f"依赖不能提供受保护的宿主或标准库模块: {import_name}"
                    host_owner = self.imports.get(import_name)
                    if host_owner is not None and host_owner != package.name:
                        return f"导入冲突: {package.name} 与宿主 {host_owner} 都提供 {import_name}"
                    occupied_owner = self._process_imports.get(import_name)
                    if occupied_owner is not None and occupied_owner != package.name:
                        return f"导入冲突: {package.name} 与已占用包 {occupied_owner} 都提供 {import_name}"
                    if import_name in sys.modules and host_owner is None and occupied_owner is None:
                        return f"导入模块已被本进程占用，无法确认兼容性: {import_name}"
        return None

    def combination_conflicts(self, directories: dict[str, PluginDependencyDirectory | None]) -> dict[str, str]:
        """
        在执行任何插件前检查整个启动组合，避免扫描顺序决定版本胜者。

        :param directories: 已启用插件名与准备结果
        :return: 需要保持禁用的插件及明确冲突原因
        """
        errors: dict[str, str] = {}
        for name, directory in directories.items():
            reason = self.conflict(directory)
            if reason:
                errors[name] = reason
        names = sorted(directories)
        for index, name in enumerate(names):
            directory = directories[name]
            if directory is None:
                continue
            for other_name in names[index + 1 :]:
                other = directories[other_name]
                if other is None:
                    continue
                for package in directory.packages:
                    for other_package in other.packages:
                        if (
                            package.name == other_package.name
                            and Version(package.version) != Version(other_package.version)
                        ) or (package.name != other_package.name and package.imports & other_package.imports):
                            reason = f"插件依赖冲突: {name} 的 {package.name}=={package.version} 与 {other_name} 的 {other_package.name}=={other_package.version}"
                            errors[name] = errors[other_name] = reason
        return errors

    def register(self, directory: PluginDependencyDirectory | None) -> None:
        """
        兼容检查后登记依赖占用，将库目录追加到受保护宿主路径之后。

        :param directory: 已验证的依赖目录；无依赖时不修改任何状态
        :raises PluginDependencyError: 已占用版本或模块冲突
        """
        if directory is None:
            return
        with self._lock:
            reason = self.conflict(directory)
            if reason:
                raise PluginDependencyError(reason)
            for package in directory.packages:
                self._process_packages[package.name] = package
                for import_name in package.imports:
                    self._process_imports[import_name] = package.name
            site_path = str(directory.site_path)
            if site_path not in self._process_paths:
                sys.path.append(site_path)
                self._process_paths.add(site_path)
