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
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ECL.plugins.environment_pool import PluginEnvironmentError, _exclusive_lock
from ECL.plugins.package_activation import (
    PluginPackageActivation,
    PluginPackageActivationError,
    PluginPackageActivationStore,
    plugin_name_pattern,
)
from ECL.plugins.package_archive import PluginPackageError
from ECL.plugins.package_preparation import PluginPackagePreflight, PluginPackagePreparer, PluginPreparationError
from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimeError, load_plugin_runtime_asset

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

    def _package_asset(self) -> PluginRuntimeAsset:
        manifest_path = self._resource_path / "resources" / "plugin_runtime_manifest.json"
        return load_plugin_runtime_asset(manifest_path)

    def inspect_package(self, source_path: str) -> PluginPackagePreflight:
        """
        预检本地归档及当前目标依赖，不下载资产或执行插件代码。

        :param source_path: 本地 `.eclplugin` 文件路径
        :return: 已校验的归档信息与未验证来源标记
        :raises PluginPreparationError: 路径或依赖契约不满足要求时抛出
        """
        if not isinstance(source_path, str) or not source_path.strip():
            raise PluginPreparationError("插件包路径无效")
        archive_path = Path(source_path)
        if archive_path.suffix.lower() != ".eclplugin" or not archive_path.is_file():
            raise PluginPreparationError("请选择有效的 .eclplugin 文件")
        return PluginPackagePreparer(self._data_path, self._package_asset()).inspect(archive_path)

    def install_package(
        self,
        source_path: str,
        *,
        confirm_unverified_source: bool,
        allow_network: bool = False,
        offline_runtime_pack: str | None = None,
    ) -> PluginActionResult:
        """
        显式确认后准备并激活归档；失败时保留已提交的旧版本。

        安装可能下载运行时和依赖，异步调用方必须放到后台线程。历史版本和共享
        环境暂不清理；文件安装界面与 Worker 扩展点代理另行接入。

        :param source_path: 本地 `.eclplugin` 文件路径
        :param confirm_unverified_source: 用户是否确认无签名来源警告
        :param allow_network: 是否允许联网补齐缺失的运行时和依赖
        :param offline_runtime_pack: 可选的插件专用运行时离线包路径
        :return: 安装结果；失败时包含可显示的原因
        """
        if confirm_unverified_source is not True:
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "安装无签名插件前必须确认来源未验证")
        if not isinstance(allow_network, bool) or (
            offline_runtime_pack is not None and not isinstance(offline_runtime_pack, str)
        ):
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "插件安装参数无效")
        try:
            preflight = self.inspect_package(source_path)
        except (OSError, PluginPackageError, PluginPreparationError, PluginRuntimeError) as exc:
            return PluginActionResult("", PluginAction.INSTALL, "invalid", str(exc))
        return self._install_preflight_package(
            Path(source_path), preflight, allow_network=allow_network, offline_runtime_pack=offline_runtime_pack
        )

    def _install_preflight_package(
        self,
        archive_path: Path,
        preflight: PluginPackagePreflight,
        *,
        allow_network: bool,
        offline_runtime_pack: str | None,
    ) -> PluginActionResult:
        """
        在管理器锁下完成版本准备与原子激活，失败时清理未提交的新代码。
        """
        name = preflight.package.name
        install_lock_path = self._data_path / "plugin_packages" / f"{name}.install.lock"
        try:
            with _exclusive_lock(install_lock_path), self._package_lock:
                return self._install_preflight_package_locked(
                    archive_path,
                    preflight,
                    allow_network=allow_network,
                    offline_runtime_pack=offline_runtime_pack,
                )
        except (OSError, PluginEnvironmentError) as exc:
            return PluginActionResult(name, PluginAction.INSTALL, "failed", str(exc))

    def _install_preflight_package_locked(
        self,
        archive_path: Path,
        preflight: PluginPackagePreflight,
        *,
        allow_network: bool,
        offline_runtime_pack: str | None,
    ) -> PluginActionResult:
        name = preflight.package.name
        pointer_path = self._data_path / "plugin_packages" / name / "active.json"
        if (pointer_path.exists() or pointer_path.is_symlink()) and name not in self._package_entries:
            return PluginActionResult(name, PluginAction.INSTALL, "failed", "现有归档活动指针尚未恢复")
        if name in self._candidate_map:
            candidate = self._candidate_map[name]
            reason = "不能覆盖系统插件" if candidate.get("is_system", False) else "同名目录插件已安装"
            return PluginActionResult(name, PluginAction.INSTALL, "forbidden", reason)
        if name in self._package_conflicts or self._package_entries.get(name, {}).get("status") == "error":
            return PluginActionResult(name, PluginAction.INSTALL, "failed", "现有归档活动指针无法恢复，请先处理")
        code_path = self._data_path / "plugin_packages" / name / f"pkg-{preflight.package.manifest_sha256[:20]}"
        code_existed = code_path.exists()
        pointer_existed = pointer_path.exists() or pointer_path.is_symlink()
        store = self._package_store
        created_store = False
        try:
            asset = self._package_asset()
            if store is not None and store.runtime_asset != asset:
                raise PluginRuntimeError("插件运行时资产已变化，请重启启动器")
            preparer = PluginPackagePreparer(self._data_path, asset)
            prepared = preparer.prepare(
                archive_path,
                confirm_unverified_source=True,
                allow_network=allow_network,
                offline_runtime_pack=Path(offline_runtime_pack) if offline_runtime_pack else None,
                expected_package=preflight.package,
            )
            if not pointer_existed and (pointer_path.exists() or pointer_path.is_symlink()):
                raise PluginPackageActivationError("安装期间出现新的归档活动指针，请重试")
            if store is None:
                store = PluginPackageActivationStore(self._data_path, asset)
                created_store = True
            previous = store.restore(name)
            active = store.activate(prepared, enabled=previous.enabled if previous is not None else True)
        except (
            OSError,
            PluginEnvironmentError,
            PluginPackageError,
            PluginPackageActivationError,
            PluginPreparationError,
            PluginRuntimeError,
        ) as exc:
            if created_store and store is not None:
                store.close()
            if (
                not code_existed
                and not pointer_path.exists()
                and not code_path.is_symlink()
                and code_path.is_dir()
                and code_path.resolve().is_relative_to(self._data_path.resolve())
            ):
                try:
                    shutil.rmtree(code_path)
                except OSError:
                    self.logger.exception("插件 %s 安装失败后无法清理未激活代码", name)
            return PluginActionResult(name, PluginAction.INSTALL, "failed", str(exc))
        self._package_store = store
        self._set_package_entry(active)
        try:
            self.events.emit("plugin:installed", name)
        except Exception:
            self.logger.exception("归档插件 %s 已安装，但事件通知失败", name)
        return PluginActionResult(name, PluginAction.INSTALL, "installed")

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
        try:
            asset = self._package_asset()
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
