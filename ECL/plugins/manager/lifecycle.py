# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实现插件的安装、启用、禁用、重载和卸载。
#
# 公开接口：
#   - class PluginLifecycle — 安装、启用、禁用、重载和卸载插件。
#       - enable(name) -> PluginActionResult — 启用插件。
#       - disable(name, _persist_state=…) -> PluginActionResult — 禁用插件，清理其注册的前端内容与事件处理器。
#       - unload(name, _persist_state=…) -> PluginActionResult — 卸载插件，清理其注册的路由、插槽内容与事件处理器。
#       - uninstall(name) -> PluginActionResult — 卸载并删除用户插件的安装目录。
#       - reload(name) -> PluginActionResult — 重新加载插件。
#       - install(source_path) -> PluginActionResult — 安装插件。
#       - on_frontend_ready() -> None — 通知所有已启用插件前端已就绪；重复调用无效。
#       - set_sidebar_state(collapsed) -> None — 记录侧栏状态，并在前端就绪后通知插件。
#       - close() -> None — 按依赖拓扑的逆序卸载已加载插件并解除框架事件订阅。
# ============================================================

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from ECL.plugins.dependencies import DependencyResolution, parse_dependency, parse_version
from ECL.plugins.environment_pool import _exclusive_lock

from .base import _PluginState
from .contracts import PluginAction, PluginActionResult
from .install_transaction import (
    PluginInstallTransaction,
    PluginInstallTransactionError,
    recover_plugin_install_transactions,
)


class PluginLifecycle(_PluginState):
    """
    安装、启用、禁用、重载和卸载插件。
    """

    def _enable_all(self) -> None:
        # 按依赖拓扑顺序启用已加载插件；被禁用的插件不参与启用。
        enabled_count = 0
        for name in self._dependency_resolution.load_order:
            if name in self._disabled_plugins:
                continue
            if name in self._plugins:
                enabled, _reason = self._enable(name)
                enabled_count += int(enabled)
        self.logger.debug("插件批量启用完成：启用数量：%d", enabled_count)

    def enable(self, name: str) -> PluginActionResult:
        """
        启用插件。

        :param name: 插件名称
        """
        if name in self._package_entries:
            return self._enable_package(name)
        enabled, reason = self._enable(name)
        return PluginActionResult(
            plugin_name=name,
            action=PluginAction.ENABLE,
            status="enabled" if enabled else self._status.get(name, "failed"),
            message=reason,
        )

    def _enable(self, name: str) -> tuple[bool, str]:
        # 启用单个插件；若因被禁用而未加载则先按候选信息加载。
        plugin = self._plugins.get(name)
        # 若插件因被禁用或安全模式（--disable-plugins）而未加载，先按候选信息加载
        if (
            plugin is None
            and name in self._candidate_map
            and (name in self._disabled_plugins or not self._startup_auto_enable or name in self._package_entries)
        ):
            candidate = self._candidate_map[name]
            self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], candidate["is_system"])
            plugin = self._plugins.get(name)
        if plugin is None:
            reason = self._plugin_errors.get(name) or self._dependency_resolution.errors.get(name)
            return False, reason or f"插件不存在或未加载: {name}"
        current = self._status.get(name)
        if current not in ("loaded", "disabled"):
            reason = self._plugin_errors.get(name) or self._dependency_resolution.errors.get(name)
            return False, reason or f"插件当前状态为 {current}，无法启用"
        # 加载后若依赖仍不满足，则不应启用
        if not self._are_dependencies_satisfied(plugin.metadata):
            reason = f"插件 {name} 依赖未满足"
            self.logger.warning(reason)
            self._plugin_errors[name] = reason
            return False, reason
        self._status[name] = "enabling"
        if not self._call_plugin_hook(plugin, "on_enable", fail_status="loaded"):
            self.instance_compatibility.unregister_owner(name)
            self.connector_extensions.unregister_owner(name)
            self.launch_hooks.unregister_owner(name)
            self.auth_providers.unregister_owner(name)
            self.crash_extensions.unregister_owner(name)
            reason = self._plugin_errors.get(name, f"插件 {name} on_enable 钩子执行失败")
            return False, reason
        self._status[name] = "enabled"
        # 从禁用列表移除并持久化，确保下次启动会加载该插件
        self._disabled_plugins.discard(name)
        self._save_plugin_state()
        self._plugin_errors.pop(name, None)
        self.events.emit("plugin:enabled", plugin)
        self.logger.info("插件已启用: %s", name)
        # 若前端已就绪，单独通知该插件注入 UI 资源，避免重复通知所有插件
        if self._frontend_ready:
            self._call_plugin_hook(plugin, "on_frontend_ready")
        return True, ""

    def disable(self, name: str, _persist_state: bool = True) -> PluginActionResult:
        """
        禁用插件，清理其注册的前端内容与事件处理器。

        :param name: 插件名称
        :param _persist_state: 是否将状态变化写入持久化文件
        """
        if _persist_state and name in self._package_entries:
            return self._disable_package(name)
        plugin = self._plugins.get(name)
        if plugin is not None and getattr(plugin, "is_system", False) is True:
            return PluginActionResult(name, PluginAction.DISABLE, "forbidden", "系统插件不能禁用")
        if plugin is None or self._status.get(name) != "enabled":
            reason = self._plugin_errors.get(name) or self._dependency_resolution.errors.get(name)
            return PluginActionResult(
                name,
                PluginAction.DISABLE,
                self._status.get(name, "not_found"),
                reason or "插件未启用",
            )
        self._status[name] = "disabling"
        self._call_plugin_hook(plugin, "on_disable")
        self._status[name] = "disabled"
        # 清理禁用插件注册的前端内容和事件处理器，重新启用时由插件重新注册
        self._routes = [r for r in self._routes if r["plugin"] != name]
        self._vue_routes = [r for r in self._vue_routes if r["plugin"] != name]
        for slot_id, entries in self._slots.items():
            self._slots[slot_id] = [entry for entry in entries if entry["plugin"] != name]
        for slot_id, entries in self._vue_slots.items():
            self._vue_slots[slot_id] = [entry for entry in entries if entry["plugin"] != name]
        self._vue_components = {
            component_name: component
            for component_name, component in self._vue_components.items()
            if component["plugin"] != name
        }
        self.events.remove_handlers_by_owner(name)
        self.instance_compatibility.unregister_owner(name)
        self.connector_extensions.unregister_owner(name)
        self.launch_hooks.unregister_owner(name)
        self.auth_providers.unregister_owner(name)
        self.crash_extensions.unregister_owner(name)
        # 持久化禁用状态，下次启动时跳过该插件；关闭流程中不写入，避免把所有插件标为禁用
        if _persist_state:
            self._disabled_plugins.add(name)
            self._save_plugin_state()
            self.events.emit("plugin:disabled", plugin)
        self.logger.info("插件已禁用: %s", name)
        return PluginActionResult(name, PluginAction.DISABLE, "disabled")

    def unload(self, name: str, _persist_state: bool = True) -> PluginActionResult:
        """
        卸载插件，清理其注册的路由、插槽内容与事件处理器。

        :param name: 插件名称
        :param _persist_state: 是否将状态变化写入持久化文件
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            reason = self._plugin_errors.get(name) or self._dependency_resolution.errors.get(name)
            return PluginActionResult(
                name,
                PluginAction.UNLOAD,
                self._status.get(name, "not_found"),
                reason or "插件不存在或未加载",
            )
        current = self._status.get(name)
        if current == "enabled":
            self.disable(name, _persist_state=_persist_state)
        self._status[name] = "unloading"
        self._call_plugin_hook(plugin, "on_unload")
        # 清理注册的路由和状态
        self._routes = [r for r in self._routes if r["plugin"] != name]
        self._vue_routes = [r for r in self._vue_routes if r["plugin"] != name]
        # 清理插槽注入
        for slot_id, entries in self._slots.items():
            self._slots[slot_id] = [entry for entry in entries if entry["plugin"] != name]
        for slot_id, entries in self._vue_slots.items():
            self._vue_slots[slot_id] = [entry for entry in entries if entry["plugin"] != name]
        # 清理已注册的 Vue 组件
        self._vue_components = {k: v for k, v in self._vue_components.items() if v["plugin"] != name}
        # 清理事件处理器
        self.events.remove_handlers_by_owner(name)
        self.instance_compatibility.unregister_owner(name)
        self.connector_extensions.unregister_owner(name)
        self.launch_hooks.unregister_owner(name)
        self.auth_providers.unregister_owner(name)
        self.crash_extensions.unregister_owner(name)
        self._plugins.pop(name, None)
        self._status.pop(name, None)
        self.events.emit("plugin:unloaded", name)
        self.logger.info("插件已卸载: %s", name)
        return PluginActionResult(name, PluginAction.UNLOAD, "unloaded")

    def uninstall(self, name: str) -> PluginActionResult:
        """
        卸载并删除用户插件的安装目录。

        :param name: 插件名称
        """
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None:
            return PluginActionResult(str(name), PluginAction.UNINSTALL, "invalid", "插件名包含非法字符")
        if name in self._package_entries:
            return self._uninstall_package(name)
        return self._uninstall_directory(name)

    def _uninstall_directory(self, name: str) -> PluginActionResult:
        """
        删除普通目录插件；调用方已排除归档指针和非法名称。
        """
        journal_path = self._data_path / "plugin_install_transactions" / f"{name}.json"
        if journal_path.exists():
            failed = recover_plugin_install_transactions(self._data_path, self._plugin_dir)
            if name in failed or journal_path.exists():
                return PluginActionResult(name, PluginAction.UNINSTALL, "failed", "插件安装事务尚未完成，暂不能卸载")
        candidate = self._candidate_map.get(name)
        if candidate is None:
            return PluginActionResult(name, PluginAction.UNINSTALL, "not_found", f"插件不存在: {name}")
        if candidate.get("is_system", False):
            return PluginActionResult(name, PluginAction.UNINSTALL, "forbidden", "系统插件不能卸载")

        plugin_dir = Path(candidate["plugin_dir"])
        plugin_root = self._plugin_dir.resolve()
        resolved_plugin_dir = plugin_dir.resolve()
        if resolved_plugin_dir.parent != plugin_root:
            return PluginActionResult(name, PluginAction.UNINSTALL, "invalid", "插件目录超出用户插件路径")

        if name in self._plugins:
            unloaded = self.unload(name, _persist_state=False)
            if not unloaded.success:
                return PluginActionResult(name, PluginAction.UNINSTALL, "failed", unloaded.message)

        try:
            if resolved_plugin_dir.exists():
                shutil.rmtree(resolved_plugin_dir)
        except OSError as exc:
            self._status[name] = "error"
            self._plugin_errors[name] = f"删除插件目录失败: {exc}"
            return PluginActionResult(name, PluginAction.UNINSTALL, "failed", self._plugin_errors[name])

        self._candidate_map.pop(name, None)
        self._status.pop(name, None)
        self._plugin_errors.pop(name, None)
        self._config_paths.pop(name, None)
        self._permission_manager.unregister_plugin(name)
        if name in self._disabled_plugins:
            self._disabled_plugins.discard(name)
            self._save_plugin_state()
        self.events.emit("plugin:uninstalled", name)
        self.logger.info("插件已删除: %s", name)
        return PluginActionResult(name, PluginAction.UNINSTALL, "uninstalled")

    def reload(self, name: str) -> PluginActionResult:
        """
        重新加载插件。

        :param name: 插件名称
        """
        if name in self._package_entries:
            return self._reload_package(name)
        plugin = self._plugins.get(name)
        if plugin is None:
            reason = self._plugin_errors.get(name) or self._dependency_resolution.errors.get(name)
            return PluginActionResult(
                name,
                PluginAction.RELOAD,
                self._status.get(name, "not_found"),
                reason or "插件不存在或未加载",
            )
        plugin_dir = plugin.plugin_dir
        is_system = getattr(plugin, "is_system", False)
        # 重载是临时卸载，不应把插件写入禁用状态文件
        self.unload(name, _persist_state=False)
        metadata_path = plugin_dir / "plugin.json"
        if not metadata_path.is_file():
            return PluginActionResult(name, PluginAction.RELOAD, "invalid", "插件清单不存在")
        self._load_plugin(plugin_dir, metadata_path, is_system)
        enabled, reason = self._enable(name)
        return PluginActionResult(name, PluginAction.RELOAD, "enabled" if enabled else "failed", reason)

    def install(
        self,
        source_path: str,
        *,
        confirm_unverified_source: bool = False,
        allow_network: bool = False,
    ) -> PluginActionResult:
        """
        从目录或归档安装插件，失败时保留旧版代码与启用状态。

        归档必须显式确认来源未验证，安装后重启加载。所有插件共享宿主解释器，
        仅归档依赖的安装目录独立，不提供不同版本并行导入。

        :param source_path: 待安装插件的本地归档或源目录
        :param confirm_unverified_source: 是否确认归档来源未验证
        :param allow_network: 是否允许补齐缺失 wheel
        :return: 安装结果；失败时旧插件尽可能保持可用
        """
        if not isinstance(source_path, str) or not source_path.strip():
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "插件源目录路径无效")
        source = Path(source_path)
        if source.suffix.lower() == ".eclplugin":
            return self.install_package(
                source_path,
                confirm_unverified_source=confirm_unverified_source,
                allow_network=allow_network,
            )
        if not source.is_dir():
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "插件源目录不存在")
        metadata_path = source / "plugin.json"
        if not metadata_path.is_file():
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "插件清单不存在")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "插件清单不是有效的 UTF-8 JSON")
        if not isinstance(metadata, dict):
            return PluginActionResult("", PluginAction.INSTALL, "invalid", "插件清单必须是对象")
        target_name = metadata.get("name")
        if not isinstance(target_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", target_name):
            return PluginActionResult(str(target_name), PluginAction.INSTALL, "invalid", "插件名包含非法字符")
        if target_name in self._package_entries:
            return PluginActionResult(target_name, PluginAction.INSTALL, "forbidden", "同名归档插件已安装")
        return self._install_directory(source, target_name, metadata)

    def _install_directory(self, source: Path, target_name: str, metadata: dict[str, Any]) -> PluginActionResult:
        """
        校验普通目录插件元数据并在恢复日志保护下完成安装。
        """
        if self._candidate_map.get(target_name, {}).get("is_system", False):
            return PluginActionResult(target_name, PluginAction.INSTALL, "forbidden", "不能覆盖系统插件")
        metadata_error = self._install_metadata_error(metadata)
        if metadata_error is not None:
            return PluginActionResult(target_name, PluginAction.INSTALL, "invalid", metadata_error)
        if not self._are_dependencies_satisfied(metadata):
            return PluginActionResult(target_name, PluginAction.INSTALL, "failed", "插件依赖未满足")

        transaction = PluginInstallTransaction(self._data_path, self._plugin_dir, target_name)
        try:
            with _exclusive_lock(transaction.lock_path):
                if transaction.journal_path.exists():
                    return PluginActionResult(target_name, PluginAction.INSTALL, "failed", "旧安装事务尚未恢复")
                transaction.stage(source)
                return self._install_staged_plugin(transaction, metadata)
        except (OSError, PluginInstallTransactionError) as exc:
            self.logger.exception("插件 %s 安装事务失败", target_name)
            return PluginActionResult(target_name, PluginAction.INSTALL, "failed", str(exc))

    @staticmethod
    def _install_metadata_error(metadata: dict[str, Any]) -> str | None:
        """
        在卸载旧实例前校验目录插件的稳定清单字段，避免无效输入造成状态变更。
        """
        if not isinstance(metadata.get("version", "0.0.0"), str) or not isinstance(
            metadata.get("entry_point", "main:Plugin"), str
        ):
            return "插件版本或入口无效"
        permissions = metadata.get("permissions", [])
        if not isinstance(permissions, list) or any(not isinstance(item, dict) for item in permissions):
            return "插件权限声明必须是对象数组"
        dependencies = metadata.get("dependencies", {})
        if not isinstance(dependencies, dict) or any(
            not isinstance(dep_name, str)
            or not isinstance(dep_value, (str, dict))
            or (isinstance(dep_value, dict) and not isinstance(dep_value.get("version", ""), str))
            for dep_name, dep_value in dependencies.items()
        ):
            return "插件间依赖必须是对象"
        return None

    def _install_staged_plugin(
        self, transaction: PluginInstallTransaction, metadata: dict[str, Any]
    ) -> PluginActionResult:
        """
        切换已暂存目录，验证新实例，必要时恢复旧实例及注册状态。
        """
        name = transaction.name
        previous_candidate = self._candidate_map.get(name)
        if previous_candidate is None and transaction.target_path.is_dir():
            previous_metadata_path = transaction.target_path / "plugin.json"
            if previous_metadata_path.is_file():
                try:
                    previous_metadata = json.loads(previous_metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    previous_metadata = None
                if isinstance(previous_metadata, dict):
                    previous_candidate = {
                        "name": name,
                        "plugin_dir": transaction.target_path,
                        "metadata_path": previous_metadata_path,
                        "metadata": previous_metadata,
                        "is_system": False,
                    }
        previous_status = self._status.get(name)
        previous_error = self._plugin_errors.get(name)
        previous_resolution = self._dependency_resolution
        was_loaded = name in self._plugins
        was_disabled = name in self._disabled_plugins
        candidate = {
            "name": name,
            "plugin_dir": transaction.target_path,
            "metadata_path": transaction.target_path / "plugin.json",
            "metadata": metadata,
            "is_system": False,
        }
        try:
            if was_loaded and not self.unload(name, _persist_state=False).success:
                raise PluginInstallTransactionError("旧插件无法卸载")
            transaction.begin()
            self._candidate_map[name] = candidate
            self._plugin_errors.pop(name, None)
            if was_disabled:
                self._status[name] = "disabled"
                self._permission_manager.register_plugin_permissions(name, metadata.get("permissions", []))
            else:
                self._load_plugin(transaction.target_path, candidate["metadata_path"], is_system=False)
                if name not in self._plugins or name in self._plugin_errors:
                    raise PluginInstallTransactionError(self._plugin_errors.get(name, "插件加载失败"))
                enabled, reason = self._enable(name)
                if not enabled:
                    raise PluginInstallTransactionError(reason or "插件启用失败")
            self._dependency_resolution = self._resolve_candidate_dependencies(list(self._candidate_map.values()))
            if not transaction.commit():
                self.logger.warning("插件 %s 已安装，旧版备份将在下次启动时清理", name)
            try:
                self.events.emit("plugin:installed", name)
            except Exception:
                self.logger.exception("插件 %s 安装完成，但安装事件通知失败", name)
            return PluginActionResult(name, PluginAction.INSTALL, "installed")
        except Exception as exc:
            self.logger.exception("插件 %s 安装失败，尝试恢复旧版", name)
            self._rollback_staged_plugin(
                transaction,
                previous_candidate,
                previous_status,
                previous_error,
                previous_resolution,
                was_loaded,
                was_disabled,
            )
            return PluginActionResult(name, PluginAction.INSTALL, "failed", str(exc))

    def _rollback_staged_plugin(
        self,
        transaction: PluginInstallTransaction,
        previous_candidate: dict[str, Any] | None,
        previous_status: str | None,
        previous_error: str | None,
        previous_resolution: DependencyResolution,
        was_loaded: bool,
        was_disabled: bool,
    ) -> None:
        """
        卸载未提交的新实例并恢复旧版；恢复失败时保留日志供启动恢复。
        """
        name = transaction.name
        if name in self._plugins:
            self.unload(name, _persist_state=False)
        if transaction.journal_path.exists():
            transaction.rollback()
        elif transaction.stage_path.exists():
            shutil.rmtree(transaction.stage_path)
        self._candidate_map.pop(name, None)
        self._status.pop(name, None)
        self._plugin_errors.pop(name, None)
        self._permission_manager.unregister_plugin(name)
        self._dependency_resolution = previous_resolution
        if previous_candidate is not None:
            self._candidate_map[name] = previous_candidate
        if was_disabled:
            self._disabled_plugins.add(name)
            self._status[name] = "disabled"
            if previous_candidate is not None:
                self._permission_manager.register_plugin_permissions(
                    name, previous_candidate["metadata"].get("permissions", [])
                )
        elif was_loaded:
            old_metadata_path = transaction.target_path / "plugin.json"
            self._load_plugin(transaction.target_path, old_metadata_path, is_system=False)
            if name not in self._plugins:
                raise PluginInstallTransactionError("旧版目录已恢复，但旧插件加载失败")
            if previous_status == "enabled":
                enabled, reason = self._enable(name)
                if not enabled:
                    raise PluginInstallTransactionError(f"旧版目录已恢复，但旧插件启用失败: {reason}")
        if previous_error is not None:
            self._plugin_errors[name] = previous_error

    def _are_dependencies_satisfied(self, metadata: dict[str, Any]) -> bool:
        # 检查插件元数据中的依赖是否已被当前加载的插件满足。
        deps = metadata.get("dependencies", {})
        for dep_name, dep_value in deps.items():
            req = parse_dependency(dep_name, dep_value)
            if req is None:
                continue
            dep_plugin = self._plugins.get(req.name)
            if dep_plugin is None:
                if not req.optional:
                    return False
                continue
            dep_version = parse_version(dep_plugin.version)
            if dep_version is None or not req.specifier.contains(dep_version, prereleases=True):
                return False
        return True

    def on_frontend_ready(self) -> None:
        """
        通知所有已启用插件前端已就绪；重复调用无效。
        """
        if self._frontend_ready:
            self.logger.debug("忽略重复的插件前端就绪通知")
            return
        self.logger.info("正在通知已启用插件前端已就绪")
        self._frontend_ready = True
        notified = 0
        for name, plugin in self._plugins.items():
            if self._status.get(name) != "enabled":
                continue
            self._call_plugin_hook(plugin, "on_frontend_ready")
            notified += 1
        if self._sidebar_collapsed is not None:
            self.events.emit("frontend:sidebar_changed", {"collapsed": self._sidebar_collapsed})
        self.logger.info(
            "插件前端就绪通知完成：通知数量：%d",
            notified,
        )

    def set_sidebar_state(self, collapsed: bool) -> None:
        """
        记录侧栏状态，并在前端就绪后通知插件。

        :param collapsed: 侧栏是否处于折叠状态
        """
        if collapsed == self._sidebar_collapsed:
            return
        self._sidebar_collapsed = collapsed
        if self._frontend_ready:
            self.events.emit("frontend:sidebar_changed", {"collapsed": collapsed})

    def close(self) -> None:
        """
        按依赖拓扑的逆序卸载已加载插件并解除框架事件订阅。
        """
        self._close_packages()
        loaded_plugins = set(self._plugins)
        plugin_names = [name for name in reversed(self._dependency_resolution.load_order) if name in loaded_plugins]
        plugin_names.extend([name for name in reversed(list(self._plugins.keys())) if name not in plugin_names])
        self.logger.info("正在退出插件框架，共 %d 个已加载插件", len(plugin_names))
        for name in plugin_names:
            try:
                # 关闭流程中不持久化禁用状态，避免把所有插件写入 plugin_state.json
                if not self.unload(name, _persist_state=False).success:
                    self.logger.warning("退出时未能卸载插件: %s", name)
            except Exception:
                self.logger.exception("退出时卸载插件失败: %s", name)

        if self._event_handlers_registered:
            self.events.unsubscribe("plugin:html_injected", self._on_html_injected)
            self.events.unsubscribe("plugin:vue_slot_registered", self._on_vue_slot_registered)
            self._event_handlers_registered = False

        self._routes.clear()
        self._vue_routes.clear()
        self._slots.clear()
        self._vue_slots.clear()
        self._vue_components.clear()
        self._config_values.clear()
        self._config_paths.clear()
        self._permission_manager.clear()
        self._disabled_plugins.clear()
        self._sidebar_collapsed = None
        self._command_executor.shutdown(wait=False)
        self.logger.info("插件框架已退出")
