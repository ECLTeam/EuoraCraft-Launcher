# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：准备归档磁盘版本并将已安装包接入主进程插件发现与生命周期。
#
# 公开接口：
#   - class PluginPackages — 归档安装、恢复、冲突检查和重启生效。
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.environment_pool import PluginDependencyDirectory, _exclusive_lock
from ECL.plugins.host_dependencies import HostDependencyPolicy
from ECL.plugins.package_activation import PluginPackageActivation, PluginPackageActivationStore, plugin_name_pattern
from ECL.plugins.package_archive import PluginPackageError
from ECL.plugins.package_preparation import PluginPackagePreflight, PluginPackagePreparer

from .base import _PluginState
from .contracts import PluginAction, PluginActionResult


class PluginPackages(_PluginState):
    """
    将归档安装版本与本进程实际加载版本分开管理。

    安装只准备文件并提交版本；启动或显式启用才使用宿主加载器，不创建 Worker。
    """

    def inspect_package(self, source_path: str) -> PluginPackagePreflight:
        """
        校验本地插件归档，不执行代码或联网。

        :param source_path: .eclplugin 文件路径
        :return: 当前宿主目标的依赖预检结果
        :raises PluginDependencyError: 路径或依赖契约无效
        """
        if not isinstance(source_path, str) or not source_path.strip():
            raise PluginDependencyError("插件包路径无效")
        archive_path = Path(source_path)
        if archive_path.suffix.lower() != ".eclplugin" or not archive_path.is_file():
            raise PluginDependencyError("请选择有效的 .eclplugin 文件")
        return PluginPackagePreparer(self._data_path).inspect(archive_path)

    def _dependency_policy(self) -> HostDependencyPolicy:
        if self._host_dependency_policy is None:
            self._host_dependency_policy = HostDependencyPolicy(self._resource_path)
        return self._host_dependency_policy

    def install_package(
        self, source_path: str, *, confirm_unverified_source: bool, allow_network: bool = False
    ) -> PluginActionResult:
        """
        准备并提交归档安装版本，不执行入口，新版本重启后加载。

        :param source_path: 本地 .eclplugin 路径
        :param confirm_unverified_source: 是否明确确认无签名来源
        :param allow_network: 是否允许下载缺失的锁定 wheel
        :return: 已安装及待重启结果；失败时旧指针保持不变
        """
        if confirm_unverified_source is not True or type(allow_network) is not bool:
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "安装无签名插件前必须确认来源未验证")
        try:
            self.logger.info("开始检查插件归档并准备安装")
            preflight = self.inspect_package(source_path)
            name = preflight.package.name
            self.logger.info("插件归档检查完成；插件：%s", name)
            with _exclusive_lock(self._data_path / "plugin_packages" / f"{name}.install.lock"), self._package_lock:
                candidate = self._candidate_map.get(name)
                if candidate is not None and name not in self._package_entries:
                    return PluginActionResult(name, PluginAction.INSTALL, "forbidden", "不能覆盖同名目录或系统插件")
                if name in self._package_conflicts:
                    return PluginActionResult(name, PluginAction.INSTALL, "failed", "同名目录插件与归档冲突，请先处理")
                store = self._package_store or PluginPackageActivationStore(self._data_path)
                previous_error = None
                try:
                    previous = store.restore(name)
                except PluginDependencyError as exc:
                    # 显式重装可补齐旧迁移缺失的 wheel，失败前不改旧指针。
                    previous = None
                    previous_error = f"旧安装无法恢复，新版本保持禁用，请重启后启用: {exc}"
                prepared = PluginPackagePreparer(self._data_path).prepare(
                    Path(source_path),
                    confirm_unverified_source=True,
                    allow_network=allow_network,
                    expected_package=preflight.package,
                )
                reason = self._dependency_policy().conflict(prepared.dependency_directory) or previous_error
                self.logger.info("插件安装文件与依赖准备完成，开始切换激活指针；插件：%s", name)
                active = store.activate(prepared, enabled=not reason and (previous.enabled if previous else True))
                self._package_store = store
                self._set_package_entry(active, status="pending_restart", error=reason)
                if reason:
                    self._disabled_plugins.add(name)
                    self._save_plugin_state()
                self.events.emit("plugin:installed", name)
                message = "插件已安装，重启后生效" if not reason else f"插件已安装但保持禁用: {reason}"
                if prepared.dependency_directory and prepared.dependency_directory.warnings:
                    message += "; " + "; ".join(prepared.dependency_directory.warnings)
                self.logger.info(
                    "插件安装已提交，重启后生效；插件：%s；状态：%s", name, "保持禁用" if reason else "待启用"
                )
                return PluginActionResult(name, PluginAction.INSTALL, "installed", message)
        except (OSError, PluginPackageError, PluginDependencyError) as exc:
            self.logger.exception("插件安装失败，激活指针未提交")
            return PluginActionResult("", PluginAction.INSTALL, "failed", str(exc))

    def is_package_plugin(self, name: str) -> bool:
        """
        判断名称是否属于已发现归档，供 IPC 调度使用。

        :param name: 插件名称
        :return: 是否存在归档安装条目
        """
        with self._package_lock:
            return name in self._package_entries

    def _set_package_entry(
        self, active: PluginPackageActivation, *, status: str | None = None, error: str | None = None
    ) -> None:
        metadata = json.loads((active.code_path / "plugin.json").read_text(encoding="utf-8"))
        self._package_entries[active.name] = {
            "metadata": metadata,
            "status": status or ("unloaded" if active.enabled else "disabled"),
            "pending_restart": status == "pending_restart",
            "error": error,
            "services": [],
            "active": active,
        }

    def _package_error(self, name: str, message: str) -> None:
        previous = self._package_entries.get(name, {})
        metadata = previous.get("metadata", {"name": name})
        self._package_entries[name] = {
            **previous,
            "metadata": metadata,
            "status": "error",
            "error": message,
            "services": [],
        }
        self.logger.error("归档插件 %s 无法恢复: %s", name, message)

    def _restore_package_plugins(
        self, user_names: set[str], system_names: set[str], *, check_dependencies: bool = True
    ) -> set[str]:
        """
        仅恢复归档文件，整体预检后提供宿主候选项，不执行代码。

        安全模式不登记依赖，手动启用时仍需重新校验。
        """
        self._package_entries.clear()
        self._package_candidates.clear()
        package_root = self._data_path / "plugin_packages"
        if package_root.is_symlink() or not package_root.is_dir():
            return set()
        names = {
            entry.name
            for entry in package_root.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and plugin_name_pattern.fullmatch(entry.name)
            and (entry / "active.json").is_file()
        }
        self._package_conflicts = names & (user_names | system_names)
        self._package_store = PluginPackageActivationStore(self._data_path)
        directories = {}
        for name in sorted(names - system_names):
            if name in self._package_conflicts:
                self._package_error(name, "同名目录插件与归档插件冲突，请先处理")
                continue
            try:
                active = self._package_store.restore(name)
                if active is None:
                    continue
                self._set_package_entry(active)
                if not active.enabled:
                    self._disabled_plugins.add(name)
                self._package_candidates[name] = {
                    "name": name,
                    "plugin_dir": active.code_path,
                    "metadata_path": active.code_path / "plugin.json",
                    "metadata": self._package_entries[name]["metadata"],
                    "is_system": False,
                }
                if active.enabled and name not in self._disabled_plugins:
                    directories[name] = active.dependency_directory
            except (OSError, PluginDependencyError) as exc:
                self._package_error(name, str(exc))
        if check_dependencies and directories:
            self._check_startup_directories(directories)
        return names

    def _check_startup_directories(self, directories: dict[str, PluginDependencyDirectory | None]) -> None:
        """
        在任何用户入口执行前禁用整个冲突组合，而非选择扫描顺序的胜者。
        """
        if directories:
            try:
                errors = self._dependency_policy().combination_conflicts(directories)
                for name, reason in errors.items():
                    self._disabled_plugins.add(name)
                    self._package_entries[name]["status"] = "disabled"
                    self._package_entries[name]["error"] = reason
                    self._plugin_errors[name] = reason
                if errors:
                    self._save_plugin_state()
            except PluginDependencyError as exc:
                for name in directories:
                    self._package_candidates.pop(name, None)
                    self._package_error(name, str(exc))

    def _prepare_package_load(self, name: str) -> None:
        """
        在宿主加载入口前复核磁盘版本并登记兼容依赖，不改模块缓存。
        """
        if name not in self._package_entries:
            return
        entry = self._package_entries[name]
        if entry.get("pending_restart"):
            raise PluginDependencyError("插件安装版本待重启，不能在本进程切换")
        if self._package_store is None:
            raise PluginDependencyError("归档安装状态尚未恢复")
        active = self._package_store.restore(name)
        if active is None:
            raise PluginDependencyError("归档安装指针不存在")
        if active.manifest_sha256 != entry["active"].manifest_sha256:
            raise PluginDependencyError("插件磁盘版本已变化，请重启")
        entry_modules = {f"plugin_{candidate_name}" for candidate_name in self._candidate_map}
        if active.dependency_directory and any(
            package.imports & entry_modules for package in active.dependency_directory.packages
        ):
            raise PluginDependencyError("插件依赖与宿主插件入口模块名称冲突")
        self._dependency_policy().register(active.dependency_directory)

    def _enable_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            entry = self._package_entries[name]
            if entry.get("pending_restart") or name in self._package_conflicts:
                return PluginActionResult(
                    name, PluginAction.ENABLE, "failed", entry.get("error") or "插件安装版本待重启"
                )
            enabled, reason = self._enable(name)
            if enabled and self._package_store:
                try:
                    self._package_store.set_enabled(name, True)
                except (OSError, PluginDependencyError) as exc:
                    return PluginActionResult(
                        name, PluginAction.ENABLE, "failed", f"插件已在内存启用，但下次启动状态保存失败: {exc}"
                    )
                entry["status"] = "enabled"
                entry["error"] = None
            return PluginActionResult(name, PluginAction.ENABLE, "enabled" if enabled else "failed", reason)

    def _disable_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            if name in self._plugins and self._status.get(name) == "enabled":
                result = self.disable(name, _persist_state=False)
                if not result.success:
                    return result
            if self._package_store:
                try:
                    self._package_store.set_enabled(name, False)
                except (OSError, PluginDependencyError) as exc:
                    return PluginActionResult(
                        name, PluginAction.DISABLE, "failed", f"插件下次启动禁用状态保存失败: {exc}"
                    )
            self._disabled_plugins.add(name)
            self._save_plugin_state()
            self._package_entries[name]["status"] = "disabled"
            return PluginActionResult(name, PluginAction.DISABLE, "disabled")

    def _reload_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            if self._package_entries[name].get("pending_restart"):
                return PluginActionResult(name, PluginAction.RELOAD, "failed", "插件更新待重启，不能热切换依赖")
            candidate = self._candidate_map.get(name)
            if candidate is None:
                return PluginActionResult(name, PluginAction.RELOAD, "failed", "归档插件尚未恢复")
            self.unload(name, _persist_state=False)
            self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], False)
            enabled, reason = self._enable(name)
            return PluginActionResult(name, PluginAction.RELOAD, "enabled" if enabled else "failed", reason)

    def _uninstall_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            try:
                if name in self._plugins:
                    self.unload(name, _persist_state=False)
                (self._package_store or PluginPackageActivationStore(self._data_path)).uninstall(name)
            except (OSError, PluginDependencyError) as exc:
                return PluginActionResult(name, PluginAction.UNINSTALL, "failed", str(exc))
            self._package_entries.pop(name, None)
            self._package_candidates.pop(name, None)
            self._candidate_map.pop(name, None)
            self._status.pop(name, None)
            self._plugin_errors.pop(name, None)
            self._disabled_plugins.discard(name)
            self._save_plugin_state()
            self.events.emit("plugin:uninstalled", name)
            return PluginActionResult(name, PluginAction.UNINSTALL, "uninstalled")

    def _package_metadata(self, name: str) -> dict[str, Any] | None:
        entry = self._package_entries.get(name)
        return dict(entry["metadata"]) if entry else None

    def _close_packages(self) -> None:
        self._package_store = None
