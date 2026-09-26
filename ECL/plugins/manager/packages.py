# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将已提交归档插件的 Worker 状态接入插件管理器，不执行宿主插件导入。
#
# 公开接口：
#   - class PluginPackages — 恢复归档插件并路由其基础生命周期操作。
# ============================================================

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ECL.plugins.environment_pool import PluginEnvironmentError, _exclusive_lock
from ECL.plugins.package_activation import (
    PluginPackageActivation,
    PluginPackageActivationError,
    PluginPackageActivationStore,
    plugin_name_pattern,
)
from ECL.plugins.runtime_assets import PluginRuntimeError, load_plugin_runtime_asset

from .base import _PluginState
from .contracts import PluginAction, PluginActionResult


@dataclass(frozen=True, slots=True)
class _PackageEventRef:
    name: str


class PluginPackages(_PluginState):
    """
    恢复数据目录中已提交的归档活动指针，并托管其基础生命周期。

    归档插件不会进入宿主 `_plugins` 或目录发现流程；Worker SDK 注册项迁移完成前，
    命令、设置及其他扩展点仍不可作为已兼容能力对外承诺。
    """

    def is_package_plugin(self, name: str) -> bool:
        """
        判断名称是否属于归档活动指针，供 async IPC 选择后台执行路径。

        :param name: 插件名称
        :return: 已发现归档活动指针时返回 True
        """
        with self._package_lock:
            return name in self._package_entries

    def _discover_package_names(self) -> set[str]:
        """
        仅发现具有合法名称与活动指针的普通目录，忽略半成品版本。
        """
        package_root = self._data_path / "plugin_packages"
        if package_root.is_symlink() or not package_root.is_dir():
            return set()
        return {
            entry.name
            for entry in package_root.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and plugin_name_pattern.fullmatch(entry.name) is not None
            and (entry / "active.json").is_file()
        }

    def _set_package_entry(self, active: PluginPackageActivation) -> None:
        """
        从已验证的版本目录读取显示元数据，不让 UI 依赖宿主插件实例。
        """
        try:
            metadata = json.loads((active.code_path / "plugin.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata = {}
        self._package_entries[active.name] = {
            "metadata": metadata if isinstance(metadata, dict) else {},
            "status": "enabled" if active.enabled else "disabled",
            "error": None,
        }

    def _package_error(self, name: str, message: str) -> None:
        self._package_entries[name] = {"metadata": {"name": name}, "status": "error", "error": message}
        self.logger.error("归档插件 %s 无法恢复: %s", name, message)

    def _restore_package_plugins(self, user_names: set[str], system_names: set[str]) -> set[str]:
        """
        在目录发现前恢复归档 Worker；失败时保留错误状态并阻止宿主回退。

        同名目录插件与归档冲突时两者都不启动，避免误加载未隔离代码。
        """
        self._close_packages()
        self._package_entries.clear()
        self._package_conflicts.clear()
        names = self._discover_package_names()
        if not names:
            return names
        self._package_conflicts = names & user_names
        for name in sorted(self._package_conflicts):
            self._package_error(name, "同名目录插件与归档插件冲突，请先手动处理")
        for name in sorted(names & system_names):
            self.logger.error("归档插件 %s 与系统插件重名，忽略归档活动指针", name)
        restorable_names = names - self._package_conflicts - system_names
        if not restorable_names:
            return names
        manifest_path = self._resource_path / "resources" / "plugin_runtime_manifest.json"
        try:
            asset = load_plugin_runtime_asset(manifest_path)
        except PluginRuntimeError as exc:
            for name in sorted(restorable_names):
                self._package_error(name, str(exc))
            return names
        self._package_store = PluginPackageActivationStore(self._data_path, asset)
        for name in sorted(restorable_names):
            try:
                active = self._package_store.restore(name)
                if active is not None:
                    self._set_package_entry(active)
            except (OSError, PluginEnvironmentError, PluginPackageActivationError) as exc:
                self._package_error(name, str(exc))
        return names

    def _enable_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            return self._enable_package_locked(name)

    def _enable_package_locked(self, name: str) -> PluginActionResult:
        store = self._package_store
        if store is None or name in self._package_conflicts:
            return PluginActionResult(
                name, PluginAction.ENABLE, "failed", self._package_entries.get(name, {}).get("error", "归档插件不存在")
            )
        try:
            if store.restore(name) is None:
                return PluginActionResult(name, PluginAction.ENABLE, "not_found", "归档活动指针不存在")
            active = store.set_enabled(name, True)
        except (OSError, PluginEnvironmentError, PluginPackageActivationError) as exc:
            self._package_error(name, str(exc))
            return PluginActionResult(name, PluginAction.ENABLE, "failed", str(exc))
        self._set_package_entry(active)
        self.events.emit("plugin:enabled", _PackageEventRef(name))
        return PluginActionResult(name, PluginAction.ENABLE, "enabled")

    def _disable_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            return self._disable_package_locked(name)

    def _disable_package_locked(self, name: str) -> PluginActionResult:
        store = self._package_store
        if store is None or name in self._package_conflicts:
            return PluginActionResult(
                name, PluginAction.DISABLE, "failed", self._package_entries.get(name, {}).get("error", "归档插件不存在")
            )
        try:
            active = store.set_enabled(name, False)
        except (OSError, PluginEnvironmentError, PluginPackageActivationError) as exc:
            return PluginActionResult(name, PluginAction.DISABLE, "failed", str(exc))
        self._set_package_entry(active)
        self.events.emit("plugin:disabled", _PackageEventRef(name))
        return PluginActionResult(name, PluginAction.DISABLE, "disabled")

    def _reload_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            return self._reload_package_locked(name)

    def _reload_package_locked(self, name: str) -> PluginActionResult:
        store = self._package_store
        if store is None or name in self._package_conflicts:
            return PluginActionResult(
                name, PluginAction.RELOAD, "failed", self._package_entries.get(name, {}).get("error", "归档插件不存在")
            )
        try:
            active = store.reload(name)
        except (OSError, PluginEnvironmentError, PluginPackageActivationError) as exc:
            return PluginActionResult(name, PluginAction.RELOAD, "failed", str(exc))
        self._set_package_entry(active)
        return PluginActionResult(name, PluginAction.RELOAD, "enabled")

    def _uninstall_package(self, name: str) -> PluginActionResult:
        with self._package_lock:
            return self._uninstall_package_locked(name)

    def _uninstall_package_locked(self, name: str) -> PluginActionResult:
        if name in self._package_conflicts:
            return PluginActionResult(name, PluginAction.UNINSTALL, "failed", self._package_entries[name]["error"])
        try:
            if self._package_store is not None:
                self._package_store.uninstall(name)
            else:
                pointer_path = self._data_path / "plugin_packages" / name / "active.json"
                with _exclusive_lock(pointer_path.with_suffix(".lock")):
                    pointer_path.unlink(missing_ok=True)
        except (OSError, PluginEnvironmentError, PluginPackageActivationError) as exc:
            return PluginActionResult(name, PluginAction.UNINSTALL, "failed", str(exc))
        self._package_entries.pop(name, None)
        self._disabled_plugins.discard(name)
        self._save_plugin_state()
        self.events.emit("plugin:unloaded", name)
        self.events.emit("plugin:uninstalled", name)
        return PluginActionResult(name, PluginAction.UNINSTALL, "uninstalled")

    def _package_metadata(self, name: str) -> dict[str, Any] | None:
        with self._package_lock:
            entry = self._package_entries.get(name)
            return dict(entry["metadata"]) if entry is not None else None

    def _close_packages(self) -> None:
        with self._package_lock:
            if self._package_store is not None:
                self._package_store.close()
                self._package_store = None
