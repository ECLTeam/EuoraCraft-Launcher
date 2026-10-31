# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：维护第三方安装的授权、Worker 生命周期和本运行注册，不执行宿主插件入口。
#
# 公开接口：
#   - class PluginWorkerService — 管理器唯一持有的第三方运行服务。
# ============================================================

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Literal

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api._windows import WindowsRuntimeError
from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.environment_pool import PluginDependencyDirectory
from ECL.plugins.grant_models import (
    InstanceCapability,
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession, PluginGrantStore
from ECL.plugins.installation_store import PluginInstallationStore, WorkerManifest
from ECL.plugins.package_preparation import PluginPackagePreparer, current_plugin_target
from ECL.plugins.resource_catalog import PluginResourceCatalog
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.file_storage import PluginFileStorage
from ECL.plugins.runtime.network_api import PluginNetworkService
from ECL.plugins.runtime.resources import InstanceQueries, ResourcePages, RuntimeResources
from ECL.plugins.runtime.supervisor import WorkerSupervisor
from ECL.services.operations import OperationManager
from ECL.utils import get_logger
from ECL.utils.instance_reading import InstanceReadError

if TYPE_CHECKING:
    from ECL.services.game.instance_profile_transactions import InstanceProfileTransactions
    from ECL.services.game.plugin_games import PluginGameCoordinator


@dataclass(slots=True)
class _WorkerEntry:
    manifest: WorkerManifest
    root_path: Path
    installation_id: str
    dependency: PluginDependencyDirectory | None
    source: Literal["directory", "package"]
    supervisor: WorkerSupervisor
    session: PluginGrantSession
    broker: PluginHostBroker | None = None
    state: str = "review_required"
    cleanup_state: str = "not_needed"
    error_code: str | None = None
    lock: RLock = field(default_factory=RLock)


class PluginWorkerService:
    """
    单个管理器拥有全部第三方运行，按安装串行化生命周期并确认实际进程停止。

    此服务的同步操作由管理 API 和启动后台边界执行；不创建桌面或业务服务副本。
    """

    def __init__(
        self,
        data_path: Path,
        executable: ExecutableLaunchSpec | None = None,
        *,
        instance_queries: InstanceQueries | None = None,
        instance_profiles: InstanceProfileTransactions | None = None,
        instance_roots: Callable[[], tuple[Path, ...]] | None = None,
        operations: OperationManager | None = None,
        game_operations: PluginGameCoordinator | None = None,
    ) -> None:
        """
        绑定唯一安装及授权存储，不发现或执行用户代码。

        :param data_path: 应用数据根目录
        :param executable: 可选真实发行物验收入口
        :param instance_profiles: 应用现有的共享实例资料事务资源
        :param instance_queries: 现有应用拥有的只读游戏查询
        :param operations: 应用唯一任务服务，不创建独立执行器
        :param game_operations: 应用唯一游戏业务入口，缺失时不开放游戏查询及执行
        :param instance_roots: 有效配置的游戏根目录快照
        """
        self.data_path: Path = data_path
        self.installations: PluginInstallationStore = PluginInstallationStore(data_path)
        self.grants: PluginGrantStore = PluginGrantStore(
            data_path, implemented_capabilities=PluginHostBroker.implemented_capabilities
        )
        self._entries: dict[str, _WorkerEntry] = {}
        self._lock = RLock()
        self._locks_by_name: dict[str, RLock] = {}
        self._executable = executable
        self._closed = Event()
        self._monitor_thread: Thread | None = None
        self._operations = operations
        self._game_operations = game_operations
        self._file_storage = PluginFileStorage(data_path)
        self._network_service = PluginNetworkService()
        self._instance_profiles = instance_profiles
        self._instance_queries = instance_queries
        self._catalog = (
            PluginResourceCatalog(data_path, instance_roots, instance_queries.query_instance_names)
            if instance_queries is not None and instance_roots is not None
            else None
        )
        self._resource_pages = ResourcePages()

    def refresh_resources(self) -> int:
        """
        在授权锁外检查共享资源身份，然后发布纯内存的有效范围快照。

        :return: 资源目录 revision
        :raises InstanceReadError: 未提供资源服务或目录记录损坏时抛出
        """
        if self._catalog is None:
            raise InstanceReadError("当前宿主未提供实例资源", "PLUGIN_API_METHOD_NOT_FOUND")
        try:
            revision = self._catalog.refresh()
        except (OSError, ValueError) as exc:
            if not isinstance(exc, InstanceReadError) or exc.error_code != "PLUGIN_RESOURCE_REVISION_CONFLICT":
                self.grants.update_resource_scope(frozenset())
            if isinstance(exc, InstanceReadError):
                raise
            raise InstanceReadError("实例资源目录刷新失败", "PLUGIN_RESOURCE_CATALOG_INVALID") from exc
        self.grants.update_resource_scope(self._catalog.active_root_refs())
        return revision

    def _refresh_declared_resources(self, session: PluginGrantSession) -> None:
        if any(
            item.capability in PermissionDeclaration.host_selected_capabilities for item in session.binding.declarations
        ):
            try:
                self.refresh_resources()
            except InstanceReadError:
                get_logger("PluginWorkerService").debug("实例资源当前不可用，保持相关许可拒绝")

    def permission_resources(
        self,
        name: str,
        content_digest: str,
        parent_ref: str | None,
        search: str,
        cursor: str | None,
        limit: int,
        capability: InstanceCapability = "instances.read",
    ) -> dict[str, JsonValue]:
        """
        为可信权限界面发现配置目录或展开一个目录，不替用户授予许可。

        :param name: 当前安装名
        :param content_digest: 用户审阅的内容
        :param parent_ref: 需展开的宿主目录身份
        :param search: 本级名称过滤
        :param cursor: 本次查询的短期游标
        :param limit: 页大小
        :param capability: 独立审阅的实例读或具体实例写能力
        :return: 候选分页与资源目录版本
        :raises ProtocolError: 内容或已声明能力不匹配时抛出
        """
        if self._closed.is_set():
            raise ProtocolError("插件宿主已经关闭", "PLUGIN_HOST_CLOSED")
        entry = self._entry(name)
        try:
            entry.session.binding.verify()
        except ValueError as exc:
            raise ProtocolError("插件内容已变化，请重新审阅", "PLUGIN_CONTENT_CHANGED") from exc
        if content_digest != entry.session.binding.content_digest:
            raise ProtocolError("插件内容已变化", "PLUGIN_GRANT_CONTENT_MISMATCH")
        if not any(item.capability == capability for item in entry.session.binding.declarations):
            raise ProtocolError("插件未申请此实例能力", "PLUGIN_PERMISSION_DENIED")
        if self._catalog is None:
            raise ProtocolError("当前宿主未提供实例资源", "PLUGIN_API_METHOD_NOT_FOUND")
        if parent_ref is not None:
            raise ProtocolError("游戏权限只选择根目录", "PLUGIN_SCHEMA_INVALID")
        revision = self._catalog.refresh(discover=cursor is None, parent_ref=parent_ref)
        self.grants.update_resource_scope(self._catalog.active_root_refs())
        selected = entry.session.selected_resources(capability)
        signature = json.dumps([entry.installation_id, content_digest, capability, parent_ref, search, limit])

        page = self._resource_pages.page(
            signature,
            str(revision),
            cursor,
            limit,
            lambda: self._resource_candidates(parent_ref, search, selected, capability),
        )
        return {**page, "catalog_revision": revision}

    def _resource_candidates(
        self, parent_ref: str | None, search: str, selected: tuple[str, ...], capability: InstanceCapability
    ) -> tuple[dict[str, JsonValue], ...]:
        """
        将宿主目录快照投影为可信审阅候选，保留已选择的失效项以便撤销。
        """
        assert self._catalog is not None
        resources = self._catalog.resources()
        default_ref = self._catalog.default_root_ref()
        items: list[dict[str, JsonValue]] = []
        for item in resources:
            if item.version_id is not None:
                continue
            if not item.is_active and item.resource_id not in selected:
                continue
            if search.casefold() not in item.label.casefold():
                continue
            items.append(
                {
                    "resource_id": item.resource_id,
                    "kind": "instance_root" if item.version_id is None else "instance",
                    "label": item.label,
                    "parent_ref": parent_ref,
                    "version_id": item.version_id,
                    "available": item.is_active,
                    "selected": item.resource_id in selected,
                    "is_default": item.resource_id == default_ref,
                }
            )
        return tuple(items)

    def register(
        self,
        metadata: object,
        root_path: Path,
        source: Literal["directory", "package"],
        dependency: PluginDependencyDirectory | None = None,
        *,
        allow_network: bool = False,
    ) -> None:
        """
        只登记已校验清单和内容身份；内容变更先停止旧运行，再要求重新审阅。

        :param metadata: JSON 清单边界输入
        :param root_path: 已安装代码根目录
        :param source: 宿主确定的目录或归档来源
        :param dependency: 已完成校验的锁定依赖
        :param allow_network: 安装阶段明确允许时补齐锁内 Wheel；发现阶段默认离线
        :raises ValueError: 清单、内容或安装身份无效时抛出
        """
        if self._closed.is_set():
            raise ProtocolError("插件宿主已经关闭", "PLUGIN_HOST_CLOSED")
        manifest = WorkerManifest.model_validate(metadata)
        lock_file = root_path / "locks" / f"{current_plugin_target()}.txt"
        binding = PluginContentBinding.capture(
            root_path, manifest.permissions, current_plugin_target(), lock_file if lock_file.is_file() else None
        )
        with self._lock:
            installation_lock = self._locks_by_name.setdefault(manifest.name, RLock())
        with installation_lock:
            installation_id = self.installations.ensure(manifest.name, source)
            with self._lock:
                previous = self._entries.get(manifest.name)
                if (
                    previous
                    and previous.installation_id == installation_id
                    and previous.session.binding.content_digest == binding.content_digest
                ):
                    return
            if previous:
                self.stop(manifest.name)
                self._check_file_recoveries(installation_id)
            if source == "directory":
                dependency = PluginPackagePreparer(self.data_path).prepare_dependencies(
                    root_path, tuple(manifest.python_dependencies), allow_network=allow_network
                )
            supervisor = WorkerSupervisor(
                self._executable, host_handler=lambda request: self._handle(manifest.name, request)
            )
            session = self.grants.open_session(installation_id, binding, supervisor.run_id)
            entry = _WorkerEntry(
                manifest, root_path, installation_id, dependency, source, supervisor, session, lock=installation_lock
            )
            with self._lock:
                self._entries[manifest.name] = entry

    def _entry(self, name: str) -> _WorkerEntry:
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            raise ProtocolError("插件安装未登记", "PLUGIN_RESOURCE_NOT_FOUND")
        return entry

    def name_for_installation(self, installation_id: str) -> str:
        """
        由可信入口把安装标识解析为管理器名称，不接受插件自报归属。

        :param installation_id: 宿主持久化的安装标识
        :return: 当前安装名称
        :raises ProtocolError: 标识没有活动安装时抛出
        """
        with self._lock:
            for name, entry in self._entries.items():
                if entry.installation_id == installation_id:
                    return name
        raise ProtocolError("插件安装不存在", "PLUGIN_RESOURCE_NOT_FOUND")

    def _handle(self, name: str, request: RpcRequest) -> dict[str, JsonValue]:
        broker = self._entry(name).broker
        if broker is None:
            raise ProtocolError("插件运行尚未接收业务请求", "PLUGIN_SESSION_EXPIRED")
        return broker.handle(request)

    def enable(self, name: str, *, frontend_ready: bool = False) -> None:
        """
        审阅及必要授权满足后才创建进程，生命周期实际成功后报告 enabled。

        :param name: 当前安装名称
        :param frontend_ready: 可信主界面是否已经就绪
        :raises ProtocolError: 平台、授权或生命周期未完成时抛出
        """
        entry = self._entry(name)
        with entry.lock:
            if self._closed.is_set():
                raise ProtocolError("插件宿主已经关闭", "PLUGIN_HOST_CLOSED")
            if entry.state == "enabled":
                return
            if entry.cleanup_state == "pending":
                raise ProtocolError("上一运行仍在完成清理", "PLUGIN_HOST_CLEANUP_PENDING")
            if platform.system() != "Windows" or platform.machine().casefold() not in {"amd64", "x86_64"}:
                raise ProtocolError("当前平台尚未支持第三方插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")
            self._check_file_recoveries(entry.installation_id)
            if entry.session.view().error_code == "PLUGIN_SESSION_EXPIRED":
                entry.supervisor = WorkerSupervisor(
                    self._executable, host_handler=lambda request: self._handle(name, request)
                )
                entry.session = self.grants.open_session(
                    entry.installation_id, entry.session.binding, entry.supervisor.run_id
                )
            self._refresh_declared_resources(entry.session)
            entry.session.binding.verify()
            view = entry.session.view()
            if view.error_code:
                raise PluginGrantError("插件授权记录不可用", view.error_code)
            if view.revision == 0:
                raise PluginGrantError("插件需要用户审阅", "PLUGIN_GRANT_REVIEW_REQUIRED")
            if not view.required_satisfied:
                raise PluginGrantError("插件缺少必要授权", "PLUGIN_REQUIRED_PERMISSION_DENIED")
            entry.state = "starting"
            try:
                entry.supervisor.start()
                entry.broker = PluginHostBroker(
                    entry.session,
                    self.data_path,
                    entry.supervisor.request,
                    run_state=lambda: entry.state,
                    operations=self._operations,
                    file_storage=self._file_storage,
                    network_service=self._network_service,
                    instance_profiles=self._instance_profiles,
                    game_operations=self._game_operations,
                    resources=RuntimeResources(
                        entry.session, self._catalog, self._instance_queries, self.refresh_resources
                    )
                    if self._catalog is not None and self._instance_queries is not None
                    else None,
                )
                entry.supervisor.authorize_load(
                    entry.session, entry.manifest.entry_file, entry.dependency, entry_class=entry.manifest.entry_class
                )
                self._phase(entry, "load", "loaded")
                self._phase(entry, "enable", "loaded")
                entry.state = "enabled"
                entry.error_code = None
                self._start_monitor()
                if frontend_ready:
                    self._phase(entry, "frontend_ready", "enabled")
            except BaseException:
                entry.state = "failed"
                self.stop(name)
                entry.state = "failed"
                raise

    @staticmethod
    def _phase(entry: _WorkerEntry, phase: str, previous: str) -> None:
        reply = entry.supervisor.request("lifecycle.invoke", {"phase": phase, "run_state": previous})
        if reply.get("ok") is not True:
            raise ProtocolError("插件生命周期执行失败", "PLUGIN_WORKER_EXECUTION_FAILED")

    def stop(self, name: str) -> None:
        """
        先撤销本运行准入和注册，再有界回收 Worker；失败保留实际清理状态。

        :param name: 要停止的安装
        :raises WindowsRuntimeError: 进程树尚未确认回收时抛出
        """
        entry = self._entry(name)
        with entry.lock:
            was_enabled = entry.state == "enabled"
            entry.state = "stopping"
            entry.cleanup_state = "pending"
            entry.session.close()
            if entry.broker:
                entry.broker.close()
            try:
                if was_enabled:
                    try:
                        self._phase(entry, "disable", "enabled")
                        self._phase(entry, "unload", "disabled")
                    except (ProtocolError, OSError):
                        get_logger("PluginWorkerService").warning("插件退出钩子未完成，继续按实际进程树回收")
                entry.supervisor.close()
                if entry.broker:
                    entry.broker.close()
                    if not entry.broker.wait_idle(1):
                        raise WindowsRuntimeError("插件宿主调用仍在完成中", "PLUGIN_HOST_CLEANUP_PENDING")
            except (ProtocolError, OSError, WindowsRuntimeError) as exc:
                entry.cleanup_state = "pending"
                entry.error_code = getattr(exc, "error_code", "PLUGIN_JOB_STOP_PENDING")
                raise
            entry.broker = None
            entry.state = "disabled"
            entry.cleanup_state = "completed"
            if entry.error_code in {"PLUGIN_JOB_STOP_PENDING", "PLUGIN_HOST_CLEANUP_PENDING"}:
                entry.error_code = None

    def _check_file_recoveries(self, installation_id: str) -> None:
        """
        Windows原生文件事务未结束时阻止重新启用、覆盖安装或卸载。
        """
        if sys.platform != "win32":
            return
        if self._instance_profiles is not None:
            self._instance_profiles.refresh(installation_id)
            if self._instance_profiles.has_pending(installation_id=installation_id):
                raise ProtocolError("实例资料需要完成恢复", "PLUGIN_INSTANCE_PROFILE_RECOVERY_REQUIRED")
        self._file_storage.replacements.refresh(installation_id)
        if self._file_storage.replacements.has_pending(installation_id):
            raise ProtocolError("插件文件需要先完成恢复", "PLUGIN_FILE_RECOVERY_REQUIRED")

    def profile_recoveries(self, cursor: str | None = None, limit: int = 50) -> dict[str, JsonValue]:
        """
        向可信管理入口提供实例资料恢复，不依赖旧插件仍然安装或获得授权。

        :param cursor: 当前宿主的有界分页引用
        :param limit: 返回项上限
        :return: 不含路径或资料正文的恢复页面
        :raises ProtocolError: 当前宿主未提供游戏资料领域服务
        """
        if self._instance_profiles is None:
            raise ProtocolError("当前宿主未提供实例资料", "PLUGIN_API_METHOD_NOT_FOUND")
        return self._instance_profiles.page(cursor, limit)

    def resolve_profile_recovery(
        self,
        reference: str,
        revision: int,
        etag: str,
        action: str,
        suffix: str | None,
        caller: PermissionManagementCaller,
    ) -> dict[str, str]:
        """
        仅接纳原生主窗口的实例原资料恢复，不赋予旧Worker控制权。

        :param reference: 当前宿主恢复引用
        :param revision: 用户审阅的记录版本
        :param etag: 用户审阅的实际对象摘要
        :param action: 明确恢复选择
        :param suffix: 另存资料的受限显式后缀
        :param caller: 宿主确认的真实调用窗口身份
        :return: 稳定接纳回执，实际结果由应用任务服务确认
        :raises ProtocolError: 非主窗口或没有游戏资料/应用任务服务
        """
        if caller is not PermissionManagementCaller.main_webview:
            raise ProtocolError("此入口不能恢复实例资料", "PLUGIN_MANAGEMENT_CALLER_DENIED")
        if self._instance_profiles is None or self._operations is None:
            raise ProtocolError("当前宿主未提供实例资料任务", "PLUGIN_API_METHOD_NOT_FOUND")
        return self._instance_profiles.resolve(reference, revision, etag, action, suffix, self._operations)

    def file_recoveries(self, cursor: str | None = None, limit: int = 50) -> dict[str, JsonValue]:
        """
        为可信管理入口查询登记事务，不要求安装仍存在或Worker可启用。

        :param cursor: 当前宿主分页引用
        :param limit: 有界项数
        :return: 脱敏恢复页面
        """
        return self._file_storage.replacements.page(cursor, limit)

    def resolve_file_recovery(
        self,
        reference: str,
        revision: int,
        etag: str,
        action: str,
        saved_name: str | None,
        caller: PermissionManagementCaller,
    ) -> dict[str, str]:
        """
        只允许主窗口接纳用户恢复任务，不恢复旧插件主体或业务授权。

        :param reference: 当次宿主的恢复引用
        :param revision: 审阅的记录版本
        :param etag: 审阅的对象摘要
        :param action: 明确恢复动作
        :param saved_name: 另存时的新名称
        :param caller: 原生注入的可信调用身份
        :return: 应用用户任务回执
        :raises ProtocolError: 来源不可信或应用未提供任务服务
        """
        if caller is not PermissionManagementCaller.main_webview:
            raise ProtocolError("此入口不能恢复插件文件", "PLUGIN_MANAGEMENT_CALLER_DENIED")
        if self._operations is None:
            raise ProtocolError("当前应用未提供任务服务", "PLUGIN_API_METHOD_NOT_FOUND")
        return self._file_storage.replacements.resolve(reference, revision, etag, action, saved_name, self._operations)

    def permissions(self, name: str) -> dict[str, JsonValue]:
        """
        为可信管理入口读取本安装审阅信息，停止后建立新的待启用会话。

        :param name: 安装名称
        :return: 内容身份、revision 和能力的实际决定
        """
        entry = self._entry(name)
        self.register(
            json.loads((entry.root_path / "plugin.json").read_bytes()), entry.root_path, entry.source, entry.dependency
        )
        entry = self._entry(name)
        with entry.lock:
            if entry.session.view().error_code == "PLUGIN_SESSION_EXPIRED" and entry.cleanup_state != "pending":
                entry.supervisor = WorkerSupervisor(
                    self._executable, host_handler=lambda request: self._handle(name, request)
                )
                entry.session = self.grants.open_session(
                    entry.installation_id, entry.session.binding, entry.supervisor.run_id
                )
            self._refresh_declared_resources(entry.session)
            view = entry.session.view()
            return {
                "installation_id": view.installation_id,
                "content_digest": view.content_digest,
                "revision": view.revision,
                "grant_epoch": view.grant_epoch,
                "required_satisfied": view.required_satisfied,
                "error_code": view.error_code,
                "items": [
                    dict(
                        asdict(item),
                        resource_refs=list(item.resource_refs),
                        selected_resource_refs=list(entry.session.selected_resources(item.capability)),
                        requested_resource_refs=list(
                            next(
                                declaration.resource_refs
                                for declaration in entry.session.binding.declarations
                                if declaration.capability == item.capability
                            )
                        ),
                        implemented=item.capability in self.grants.implemented_capabilities,
                    )
                    for item in view.items
                ],
            }

    def update_permissions(
        self,
        name: str,
        content_digest: str,
        expected_revision: int,
        decisions: tuple[PermissionDecision, ...],
        caller: PermissionManagementCaller,
        resource_revision: int | None = None,
    ) -> dict[str, JsonValue]:
        """
        由可信主界面持久化决定，改变运行授权后清理原运行，避免旧注册继续使用。

        :param name: 当前安装
        :param content_digest: 用户审阅的内容摘要
        :param expected_revision: 并发更新版本
        :param decisions: 用户实际选择
        :param caller: 真实调用入口的分类
        :param resource_revision: 审阅资源候选的目录版本，允许实例读取时必填
        :return: 落盘结果和独立清理状态
        :raises PluginGrantError: 主体、内容、版本或持久化失败时抛出
        """
        entry = self._entry(name)
        if any(
            item.capability in PermissionDeclaration.host_selected_capabilities and item.decision == "allow"
            for item in decisions
        ) and (resource_revision is None or resource_revision != self.refresh_resources()):
            raise ProtocolError("资源候选已经变化，请刷新后重试", "PLUGIN_RESOURCE_REVISION_CONFLICT")
        # 不持有生命周期锁保存授权，运行中的长回调不能延迟拒绝屏障。
        view = self.grants.update(
            entry.session, decisions, expected_revision=expected_revision, content_digest=content_digest, caller=caller
        )
        if entry.broker:
            entry.broker.refresh_permissions()
        if not view.required_satisfied and entry.state == "enabled":
            try:
                self.stop(name)
            except WindowsRuntimeError:
                get_logger("PluginWorkerService").warning("插件授权已保存，进程树仍等待清理确认")
        return {
            "revision": view.revision,
            "grant_epoch": view.grant_epoch,
            "persisted": True,
            "required_satisfied": view.required_satisfied,
            "cleanup_state": entry.cleanup_state,
        }

    def runtime_info(self, name: str) -> dict[str, JsonValue]:
        """
        返回当前运行和清理状态，不把管道等待结果当成进程已停止。

        :param name: 当前安装
        :return: 安装身份、运行身份及实际状态
        """
        entry = self._entry(name)
        return {
            "installation_id": entry.installation_id,
            "run_id": entry.session.run_id,
            "run_state": entry.state,
            "pid": entry.supervisor.pid,
            "cleanup_state": entry.cleanup_state,
            "last_error": {"code": entry.error_code, "message": "插件运行未完成"} if entry.error_code else None,
        }

    def broker(self, name: str) -> PluginHostBroker:
        """
        只返回已启用安装的本运行门面，供可信管理器调用命令或设置。

        :param name: 安装名称
        :return: 已启用的最小宿主接口
        :raises ProtocolError: 安装未运行时抛出
        """
        entry = self._entry(name)
        if entry.state != "enabled" or entry.broker is None:
            raise ProtocolError("插件未启用", "PLUGIN_TARGET_DISABLED")
        return entry.broker

    def notify_frontend_ready(self) -> None:
        """
        仅向已启用的 Worker 发送就绪钩子，不允许其注入主窗口脚本。
        """
        for entry in tuple(self._entries.values()):
            if entry.state == "enabled":
                self._phase(entry, "frontend_ready", "enabled")

    def remove(self, name: str, remove_files: Callable[[], None]) -> None:
        """
        确认停止后使安装资格失效，再执行受控文件卸载；失败保持默认拒绝。

        :param name: 当前安装
        :param remove_files: 由管理器提供的受控卸载操作
        """
        entry = self._entry(name)
        with entry.lock:
            self.stop(name)
            self._check_file_recoveries(entry.installation_id)
            self.installations.retire(name)
            remove_files()
            with self._lock:
                self._entries.pop(name, None)

    def _start_monitor(self) -> None:
        with self._lock:
            if self._monitor_thread is None:
                self._monitor_thread = Thread(target=self._monitor, name="plugin_worker_monitor", daemon=True)
                self._monitor_thread.start()

    def _monitor(self) -> None:
        """
        在运行中检测实际进程退出、通信故障和必要授权失效，不依赖页面轮询清理。
        """
        while not self._closed.wait(0.25):
            with self._lock:
                entries = tuple(self._entries.items())
            if any(
                any(
                    item.capability in PermissionDeclaration.host_selected_capabilities
                    for item in entry.manifest.permissions
                )
                for _, entry in entries
            ):
                try:
                    self.refresh_resources()
                except InstanceReadError:
                    get_logger("PluginWorkerService").debug("实例资源刷新失败，相关许可保持拒绝")
            for name, entry in entries:
                with entry.lock:
                    with self._lock:
                        if self._entries.get(name) is not entry:
                            continue
                    if entry.state not in {"enabled", "stopping"}:
                        continue
                    try:
                        if entry.state == "stopping":
                            if entry.broker is None or entry.broker.wait_idle():
                                self.stop(name)
                            continue
                        if entry.broker:
                            entry.broker.refresh_pending_work()
                        view = entry.session.view()
                        if (
                            entry.supervisor.is_alive()
                            and not (entry.broker and entry.broker.is_closed)
                            and view.required_satisfied
                            and not view.error_code
                        ):
                            continue
                        failure_code = entry.broker.error_code if entry.broker else None
                        self.stop(name)
                        entry.state = "failed"
                        entry.error_code = view.error_code or failure_code or "PLUGIN_WORKER_EXITED"
                    except (OSError, ValueError, WindowsRuntimeError):
                        entry.cleanup_state = "pending"
                        get_logger("PluginWorkerService").warning("插件运行清理尚未完成，需要重试停止")

    def close(self) -> None:
        """
        停止全部已登记 Worker，失败时保留对象和句柄供重试。
        """
        self._closed.set()
        self._resource_pages.clear()
        if self._monitor_thread is not None:
            self._monitor_thread.join(1)
        failures = []
        for name in reversed(tuple(self._entries)):
            try:
                self.stop(name)
            except (OSError, ValueError, WindowsRuntimeError) as exc:
                failures.append(exc)
        is_network_idle = self._network_service.close()
        if failures:
            raise WindowsRuntimeError("部分插件进程树尚未确认停止", "PLUGIN_JOB_STOP_PENDING") from failures[0]
        if not self._file_storage.close() or not is_network_idle:
            raise WindowsRuntimeError("插件宿主资源尚未释放", "PLUGIN_HOST_CLEANUP_PENDING")
