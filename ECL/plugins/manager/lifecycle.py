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
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ECL.plugins.contracts import PluginRuntimeError
from ECL.plugins.dependencies import DependencyRequirement, parse_dependency, parse_version
from ECL.utils import remove_tree

from .base import _PluginState
from .contracts import PluginAction, PluginActionResult
from .install_transaction import (
    PluginInstallTransaction,
    PluginInstallTransactionError,
    recover_plugin_install_transactions,
)


def _manifest_error_summary(exc: ValidationError) -> str:
    """
    把清单校验异常归纳为字段和错误类型，避免日志写出用户填写的原始值。

    :param exc: `PluginManifest` 校验抛出的异常
    :return: 以逗号分隔的“字段(错误类型)”说明，无法识别字段时返回错误数量
    """
    details = [
        f"{'.'.join(str(part) for part in error['loc'])}({error['type']})" for error in exc.errors() if error.get("loc")
    ]
    if details:
        return "，".join(details)
    return f"共 {exc.error_count()} 处错误"


class PluginLifecycle(_PluginState):
    """
    安装、启用、禁用、重载和卸载插件。
    """

    def _enable_all(self) -> None:
        # 按依赖拓扑顺序启用全部已登记插件；被禁用的插件不参与启用。
        enabled_count = 0
        for name in self._dependency_resolution.load_order:
            if name in self._disabled_plugins:
                continue
            enabled, _reason = self._enable(name)
            enabled_count += int(enabled)
        self.logger.debug("插件批量启用完成：启用数量：%d", enabled_count)

    def enable(self, name: str) -> PluginActionResult:
        """
        启用插件。

        :param name: 插件名称
        """
        with self._plugin_action_lock(name):
            reason = self._session_disable_reason(name)
            if reason:
                return PluginActionResult(name, PluginAction.ENABLE, "forbidden", reason)
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
        # 启用单个插件：统一走唯一运行服务（系统插件进程内，用户插件子进程）。
        reason = self._session_disable_reason(name)
        if reason:
            return False, reason
        return self._enable_worker(name)

    def _enable_worker(self, name: str) -> tuple[bool, str]:
        """
        完成插件的依赖校验、内容审阅准入和运行服务启动后提交运行状态。

        返回失败时保留既有持久禁用偏好，不将准备成功误报为已启用。
        """
        candidate = self._candidate_map[name]
        if not self._are_dependencies_satisfied(candidate["metadata"]):
            self._plugin_errors[name] = "PLUGIN_DEPENDENCY_CONFLICT"
            return False, "PLUGIN_DEPENDENCY_CONFLICT"
        self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], candidate["is_system"])
        if self._status.get(name) == "unsupported":
            return False, self._plugin_errors[name]
        try:
            assert self.worker_service is not None
            if not candidate["is_system"]:
                self.worker_service.development.intent(name, True)
                self.worker_service.development.prepare_enable(name, frontend_ready=self._frontend_ready)
                if (
                    self.worker_service.development.manages(name)
                    and self.worker_service.runtime_info(name)["run_state"] != "enabled"
                ):
                    self.worker_service.development.retry(name)
            self.worker_service.enable(name, frontend_ready=self._frontend_ready)
            self._status[name] = "enabled"
            self._disabled_plugins.discard(name)
            self._save_plugin_state()
            self._plugin_errors.pop(name, None)
            self.events.emit("plugin:status_changed", {"plugin": name, "status": "enabled"})
            self.logger.info("插件已启用：%s", name)
            return True, ""
        except (OSError, ValueError, PluginRuntimeError) as exc:
            code = getattr(exc, "error_code", "PLUGIN_WORKER_EXECUTION_FAILED")
            # 前端只收到错误码；真实原因（含 Worker 回传的失败明细）必须留在日志中，否则无法排查。
            self.logger.warning("插件启用失败：%s；错误码：%s", name, code, exc_info=exc)
            self._status[name] = "error"
            self._plugin_errors[name] = code
            return False, code

    def disable(self, name: str, _persist_state: bool = True) -> PluginActionResult:
        """
        禁用插件，清理其注册的前端内容与事件处理器。

        :param name: 插件名称
        :param _persist_state: 是否将状态变化写入持久化文件
        """
        with self._plugin_action_lock(name):
            candidate = self._candidate_map.get(name)
            if candidate and candidate.get("is_system", False):
                return PluginActionResult(name, PluginAction.DISABLE, "forbidden", "系统插件不能禁用")
            if _persist_state and name in self._package_entries:
                return self._disable_package(name)
            return self._disable_worker(name, _persist_state)

    def _disable_worker(self, name: str, persist_state: bool) -> PluginActionResult:
        """
        先保存用户禁用选择，再尽力停止第三方 Worker。

        禁用偏好与进程回收是两件事：停止失败只影响本次运行状态，返回 "stopping"
        让调用方提示重试，不会因为进程树回收失败而丢掉用户已表达的禁用意图。
        """
        if persist_state:
            self._disabled_plugins.add(name)
            if self.worker_service is not None:
                self.worker_service.development.intent(name, False)
            self._save_plugin_state()
            if name in self._package_entries and self._package_store:
                self._package_store.set_enabled(name, False)
        try:
            assert self.worker_service is not None
            if self._status.get(name) != "unsupported":
                self.worker_service.stop(name)
        except (OSError, ValueError, PluginRuntimeError) as exc:
            return PluginActionResult(
                name, PluginAction.DISABLE, "stopping", getattr(exc, "error_code", "PLUGIN_JOB_STOP_PENDING")
            )
        if self._status.get(name) != "unsupported":
            self._status[name] = "disabled"
        self.events.emit("plugin:status_changed", {"plugin": name, "status": "disabled"})
        if persist_state:
            self.logger.info("插件已禁用：%s", name)
        return PluginActionResult(name, PluginAction.DISABLE, "disabled")

    def unload(self, name: str, _persist_state: bool = True) -> PluginActionResult:
        """
        卸载插件，清理其注册的路由、插槽内容与事件处理器。

        :param name: 插件名称
        :param _persist_state: 是否将状态变化写入持久化文件
        """
        with self._plugin_action_lock(name):
            candidate = self._candidate_map.get(name)
            if candidate and candidate.get("is_system", False):
                return PluginActionResult(name, PluginAction.UNLOAD, "forbidden", "系统插件不能卸载")
            result = self.disable(name, _persist_state)
            return PluginActionResult(
                name, PluginAction.UNLOAD, "unloaded" if result.success else result.status, result.message
            )

    def uninstall(self, name: str) -> PluginActionResult:
        """
        卸载并删除用户插件的安装目录。

        :param name: 插件名称
        """
        with self._plugin_action_lock(name):
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None:
                return PluginActionResult(str(name), PluginAction.UNINSTALL, "invalid", "插件名包含非法字符")
            if name in self._package_entries:
                return self._uninstall_package(name)
            return self._uninstall_directory(name)

    def _uninstall_directory(self, name: str) -> PluginActionResult:
        """
        删除普通目录插件；调用方已排除归档指针和非法名称。
        """
        journal_path = self._data_path / "plugins" / "transactions" / f"{name}.json"
        if journal_path.exists():
            failed = recover_plugin_install_transactions(self._data_path, self._plugin_dir, self._plugin_action_lock)
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

        if not candidate["is_system"] and self._status.get(name) != "unsupported":
            unloaded = self.unload(name, _persist_state=False)
            if not unloaded.success:
                return PluginActionResult(name, PluginAction.UNINSTALL, "failed", unloaded.message)

        try:
            assert self.worker_service is not None

            def remove_files() -> None:
                remove_tree(resolved_plugin_dir)

            if self._status.get(name) == "unsupported":
                self.worker_service.installations.retire(name)
                remove_files()
            else:
                self.worker_service.remove(name, remove_files)
        except OSError as exc:
            self._status[name] = "error"
            self._plugin_errors[name] = (
                "删除插件目录失败：文件被占用或为只读，请关闭占用该目录的程序后重试"
                if isinstance(exc, PermissionError)
                else f"删除插件目录失败: {exc}"
            )
            return PluginActionResult(name, PluginAction.UNINSTALL, "failed", self._plugin_errors[name])

        self._candidate_map.pop(name, None)
        self._status.pop(name, None)
        self._plugin_errors.pop(name, None)
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
        with self._plugin_action_lock(name):
            reason = self._session_disable_reason(name)
            if reason:
                return PluginActionResult(name, PluginAction.RELOAD, "forbidden", reason)
            candidate = self._candidate_map.get(name)
            if candidate is None:
                return PluginActionResult(
                    name,
                    PluginAction.RELOAD,
                    self._status.get(name, "not_found"),
                    f"插件不存在: {name}",
                )
            if candidate["is_system"]:
                return self._reload_system_plugin(name, candidate)
            if name in self._package_entries:
                return self._reload_package(name)
            if self.worker_service is not None and self.worker_service.development.manages(name):
                self.worker_service.development.intent(name, True)
                try:
                    self.worker_service.development.retry(name)
                    self.worker_service.enable(name, frontend_ready=self._frontend_ready)
                except (OSError, ValueError, PluginRuntimeError) as exc:
                    code = getattr(exc, "error_code", "PLUGIN_WORKER_EXECUTION_FAILED")
                    self.logger.warning("插件重载失败：%s；错误码：%s", name, code, exc_info=exc)
                    return PluginActionResult(name, PluginAction.RELOAD, "failed", code)
                self.events.emit("plugin:status_changed", {"plugin": name, "status": "enabled"})
                return PluginActionResult(name, PluginAction.RELOAD, "enabled")
            stopped = self.disable(name, _persist_state=False)
            if not stopped.success:
                return PluginActionResult(name, PluginAction.RELOAD, stopped.status, stopped.message)
            enabled = self.enable(name)
            return PluginActionResult(name, PluginAction.RELOAD, enabled.status, enabled.message)

    def _reload_system_plugin(self, name: str, candidate: dict[str, Any]) -> PluginActionResult:
        """
        重载系统插件：停止当前运行、按当前目录内容重新登记并启用，不写禁用偏好。
        """
        assert self.worker_service is not None
        try:
            if self.worker_service.runtime_info(name)["run_state"] == "enabled":
                self.worker_service.stop(name)
            self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], True)
            self.worker_service.enable(name, frontend_ready=self._frontend_ready)
        except (OSError, ValueError, PluginRuntimeError) as exc:
            code = getattr(exc, "error_code", "PLUGIN_WORKER_EXECUTION_FAILED")
            self.logger.warning("系统插件重载失败：%s；错误码：%s", name, code, exc_info=exc)
            self._status[name] = "error"
            self._plugin_errors[name] = code
            return PluginActionResult(name, PluginAction.RELOAD, "failed", code)
        self._status[name] = "enabled"
        self.events.emit("plugin:status_changed", {"plugin": name, "status": "enabled"})
        return PluginActionResult(name, PluginAction.RELOAD, "enabled")

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
        from ECL.plugins.installation_store import PluginManifest, manifest_validation_error_code

        try:
            PluginManifest.model_validate(metadata)
        except ValidationError as exc:
            self.logger.warning("插件 %s 清单校验失败：%s", source, _manifest_error_summary(exc))
            return PluginActionResult("", PluginAction.INSTALL, "invalid", manifest_validation_error_code(exc))
        target_name = metadata.get("name")
        if not isinstance(target_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", target_name):
            return PluginActionResult(str(target_name), PluginAction.INSTALL, "invalid", "插件名包含非法字符")
        if target_name in self._package_entries:
            return PluginActionResult(target_name, PluginAction.INSTALL, "forbidden", "同名归档插件已安装")
        return self._install_directory(source, target_name, metadata, allow_network=allow_network)

    def _install_directory(
        self, source: Path, target_name: str, metadata: dict[str, Any], *, allow_network: bool = False
    ) -> PluginActionResult:
        """
        校验普通目录插件元数据并在恢复日志保护下完成安装。
        """
        with self._plugin_action_lock(target_name):
            if self._candidate_map.get(target_name, {}).get("is_system", False):
                return PluginActionResult(target_name, PluginAction.INSTALL, "forbidden", "不能覆盖系统插件")
            metadata_error = self._install_metadata_error(metadata)
            if metadata_error is not None:
                self.logger.warning("插件 %s 安装前置校验失败：%s", target_name, metadata_error)
                return PluginActionResult(target_name, PluginAction.INSTALL, "invalid", metadata_error)
            missing_dependencies = self._unsatisfied_dependencies(metadata)
            if missing_dependencies:
                missing_text = "、".join(missing_dependencies)
                self.logger.warning("插件 %s 依赖未满足：%s", target_name, missing_text)
                return PluginActionResult(
                    target_name,
                    PluginAction.INSTALL,
                    "failed",
                    f"插件依赖未满足：{missing_text}",
                )

            transaction = PluginInstallTransaction(self._data_path, self._plugin_dir, target_name)
            try:
                with self._plugin_action_lock(target_name):
                    if transaction.journal_path.exists():
                        self.logger.warning("插件 %s 存在未恢复的安装事务，已拒绝覆盖", target_name)
                        return PluginActionResult(target_name, PluginAction.INSTALL, "failed", "旧安装事务尚未恢复")
                    transaction.stage(source)
                    return self._install_staged_plugin(transaction, metadata, allow_network=allow_network)
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
        self, transaction: PluginInstallTransaction, metadata: dict[str, Any], *, allow_network: bool = False
    ) -> PluginActionResult:
        """
        原子提交新代码和安装身份，不以执行插件验证安装成功。
        """
        name = transaction.name
        previous_candidate = self._candidate_map.get(name)
        previous_status = self._status.get(name)
        has_begun = False
        try:
            staged_metadata = json.loads((transaction.stage_path / "plugin.json").read_bytes())
            if staged_metadata != metadata:
                raise PluginInstallTransactionError("插件清单在预检后发生变化")
            if previous_candidate and previous_status != "unsupported":
                stopped = self.disable(name, _persist_state=False)
                if not stopped.success:
                    raise PluginInstallTransactionError("旧插件尚未停止，不能替换代码")
            has_begun = True
            self.worker_service.development.forget(name)
            transaction.begin()
            assert self.worker_service is not None
            self.worker_service.register(metadata, transaction.target_path, "directory", allow_network=allow_network)
            if not transaction.commit():
                self.logger.warning("插件安装已提交，事务备份等待清理")
        except (OSError, ValueError, PluginInstallTransactionError) as exc:
            self.logger.warning("插件 %s 安装提交失败，正在回滚：%s", name, exc)
            if has_begun:
                transaction.rollback()
            else:
                transaction.discard_stage()
            if previous_candidate:
                self._candidate_map[name] = previous_candidate
                self._status[name] = "disabled"
            return PluginActionResult(name, PluginAction.INSTALL, "failed", str(exc))
        self._candidate_map[name] = {
            "name": name,
            "metadata": metadata,
            "plugin_dir": transaction.target_path,
            "metadata_path": transaction.target_path / "plugin.json",
            "is_system": False,
        }
        self._status[name] = "disabled"
        self._plugin_errors.pop(name, None)
        self._disabled_plugins.add(name)
        self._save_plugin_state()
        self._dependency_resolution = self._resolve_candidate_dependencies(list(self._candidate_map.values()))
        self.events.emit("plugin:installed", name)
        return PluginActionResult(name, PluginAction.INSTALL, "installed", "插件已安装，重启后可在插件页启用")

    def _are_dependencies_satisfied(self, metadata: dict[str, Any]) -> bool:
        # 检查插件元数据中的依赖是否已被当前加载的插件满足。
        return not self._unsatisfied_dependencies(metadata)

    def _unsatisfied_dependencies(self, metadata: dict[str, Any]) -> list[str]:
        """
        列出当前未满足的必需依赖，供安装前置校验和失败原因使用。

        清单中格式非法的依赖条目由清单校验负责，此处跳过；可选依赖缺失
        不算失败，与启用阶段的判定保持一致。

        :param metadata: 已通过清单结构校验的插件清单
        :return: 未满足的依赖名称，全部满足时为空列表
        """
        unsatisfied: list[str] = []
        for dep_name, dep_value in metadata.get("dependencies", {}).items():
            requirement = parse_dependency(dep_name, dep_value)
            if requirement is None or requirement.optional:
                continue
            if not self._is_dependency_satisfied(requirement):
                unsatisfied.append(requirement.name)
        return unsatisfied

    def _is_dependency_satisfied(self, requirement: DependencyRequirement) -> bool:
        """
        判断单个依赖是否由已登记且已启用的插件安装满足。

        依赖方经唯一运行服务查询实际运行状态，避免安装顺序强迫用户
        先重启一次；版本不满足的已启用插件一律视为不满足。

        :param requirement: 解析后的依赖要求
        :return: 依赖名称与版本都满足时为 True
        """
        candidate = self._candidate_map.get(requirement.name)
        if (
            candidate
            and self.worker_service is not None
            and not self._session_disable_reason(requirement.name)
            and self.worker_service.runtime_info(requirement.name)["run_state"] == "enabled"
        ):
            version = parse_version(candidate["metadata"].get("version", "0.0.0"))
            if version is not None and requirement.specifier.contains(version, prereleases=True):
                return True
        return requirement.optional

    def on_frontend_ready(self) -> None:
        """
        通知所有已启用插件前端已就绪；重复调用无效。
        """
        if self._frontend_ready:
            return
        self._frontend_ready = True
        if self.worker_service is not None:
            self.worker_service.notify_frontend_ready()
        if self._sidebar_collapsed is not None:
            self.events.emit("frontend:sidebar_changed", {"collapsed": self._sidebar_collapsed})

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
        停止全部插件运行并解除框架事件订阅。

        运行服务未能全部停止时只记录失败，仍然完成包与订阅的本地清理，
        最后再向上抛出：退出流程不能因为进程树回收失败而遗留悬挂资源。
        """
        self.events.unsubscribe("config:updated", self._on_instance_config_changed)
        worker_failure: Exception | None = None
        if self.worker_service is not None:
            try:
                self.worker_service.close()
            except Exception as exc:
                worker_failure = exc
                self.logger.exception("插件运行服务未能全部停止，继续释放本地资源")
        self._close_packages()

        if self._event_handlers_registered:
            self.events.unsubscribe("plugin:html_injected", self._on_html_injected)
            self.events.unsubscribe("plugin:vue_slot_registered", self._on_vue_slot_registered)
            self._event_handlers_registered = False

        self._routes.clear()
        self._vue_routes.clear()
        self._slots.clear()
        self._vue_slots.clear()
        self._vue_components.clear()
        self._disabled_plugins.clear()
        self._sidebar_collapsed = None
        if worker_failure is not None:
            raise worker_failure
        self.logger.info("插件框架已退出")
