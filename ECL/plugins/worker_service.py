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
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from ECL.java.manager import JavaManager
from ECL.java.models import JavaError
from ECL.java.queries import JavaQueries
from ECL.minecraft.adapters.java_api import RuntimeJava
from ECL.minecraft.adapters.public_queries import RuntimePublicQueries
from ECL.minecraft.adapters.resources import RuntimeResources
from ECL.plugins.account_resources import PluginAccountResourceCatalog
from ECL.plugins.api._errors import PluginRuntimeError
from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import JsonValue, ProtocolError
from ECL.plugins.development import DevelopmentSource, PluginDevelopmentService
from ECL.plugins.environment_pool import PluginDependencyDirectory
from ECL.plugins.installation_store import PluginInstallationStore, WorkerManifest
from ECL.plugins.java_resources import PluginJavaResourceCatalog
from ECL.plugins.package_preparation import PluginPackagePreparer, current_plugin_target
from ECL.plugins.resource_catalog import PluginResourceCatalog
from ECL.plugins.run_models import PluginContentBinding, PluginLease
from ECL.plugins.run_store import PluginRunError, PluginRunSession, PluginRunStore
from ECL.plugins.runtime.adapters import McAdapterFactory
from ECL.plugins.runtime.broker import CrossPluginChannel, PluginHostBroker, ToolChannel
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.file_storage import PluginFileStorage
from ECL.plugins.runtime.network_api import PluginNetworkService
from ECL.plugins.runtime.pagination import ResourcePages
from ECL.plugins.runtime.supervisor import WorkerSupervisor
from ECL.plugins.settings import PluginSettingsService
from ECL.services.operations import OperationManager
from ECL.utils import AccountError, get_logger
from ECL.utils.account_reading import AccountSummary
from ECL.utils.instance_reading import InstanceReadError

if TYPE_CHECKING:
    from pytaurix import AppHandle

    from ECL.minecraft.adapters.resources import InstanceQueries
    from ECL.services.game.instance_profile_transactions import InstanceProfileTransactions
    from ECL.services.game.plugin_games import PluginGameCoordinator

# 生命周期钩子允许插件完成模块导入与资源索引；超过上限按执行失败回收进程树。
lifecycle_hook_timeout_seconds = 30.0

# 跨插件调用链深度：防止 A→B→A 之类循环把进程拖进互相等待。
_cross_call_depth: ContextVar[int] = ContextVar("plugin_cross_call_depth", default=0)


@dataclass(slots=True)
class _WorkerEntry:
    manifest: WorkerManifest
    root_path: Path
    installation_id: str
    dependency: PluginDependencyDirectory | None
    source: Literal["directory", "package"]
    supervisor: WorkerSupervisor
    session: PluginRunSession
    broker: PluginHostBroker | None = None
    state: str = "registered"
    cleanup_state: str = "not_needed"
    error_code: str | None = None
    lock: RLock = field(default_factory=RLock)
    source_path: Path | None = None
    development_token: str | None = None


class PluginWorkerService:
    """
    单个管理器拥有全部第三方运行，按安装串行化生命周期并确认实际进程停止。

    此服务的同步操作由管理 API 和启动后台边界执行；不创建桌面或业务服务副本。
    """

    # 实例资源目录只需在许可生效期间保持最新；进程退出检测仍按 250ms 节奏。
    resource_refresh_interval_seconds: float = 2.0

    def __init__(
        self,
        data_path: Path,
        executable: ExecutableLaunchSpec | None = None,
        *,
        instance_queries: InstanceQueries | None = None,
        instance_profiles: InstanceProfileTransactions | None = None,
        instance_roots: Callable[[], tuple[Path, ...]] | None = None,
        mc_factory: McAdapterFactory | None = None,
        operations: OperationManager | None = None,
        game_operations: PluginGameCoordinator | None = None,
        java_queries: JavaQueries | None = None,
        java_manager: JavaManager | None = None,
        account_snapshot: Callable[[], tuple[AccountSummary, ...]] | None = None,
        account_skin: Callable[[str], bytes | None] | None = None,
        public_config: Callable[[], JsonValue] | None = None,
        slot_sink: Callable[[str, dict[str, JsonValue]], None] | None = None,
        hook_sink: Callable[[str, str | None, str | None, bool], None] | None = None,
        crash_sink: Callable[[str, str | None], None] | None = None,
        compat_sink: Callable[[str, str | None, str | None, str | None], None] | None = None,
        tool_spawner: Callable[[str, str, list[str], str], str] | None = None,
        tool_stopper: Callable[[str, str], bool] | None = None,
        auth_sink: Callable[[str, str | None, object | None, PluginLease | None], None] | None = None,
        development_changed: Callable[[], None] | None = None,
        settings_changed: Callable[[str], None] | None = None,
        enable_block_reason: Callable[[str], str | None] | None = None,
    ) -> None:
        """
        绑定唯一安装与运行存储，不发现或执行用户代码。

        :param data_path: 应用数据根目录
        :param executable: 可选真实发行物验收入口
        :param instance_profiles: 应用现有的共享实例资料事务资源
        :param instance_queries: 现有应用拥有的只读游戏查询
        :param operations: 应用唯一任务服务，不创建独立执行器
        :param game_operations: 应用唯一游戏业务入口，缺失时不开放游戏查询及执行
        :param java_queries: 应用唯一 Java 查询服务，不提供安装或全系统扫描
        :param java_manager: 应用唯一 Java 安装服务，不创建第二份登记
        :param account_snapshot: 不刷新认证的账户领域最小摘要
        :param public_config: 应用当前配置快照，公开字段由边界白名单投影
        :param instance_roots: 有效配置的游戏根目录快照
        :param slot_sink: 已获准插槽内容的可信接收方；未提供时不开放界面注入
        :param hook_sink: 启动钩子接收方；阶段为 None 表示撤销该安装全部钩子
        :param crash_sink: 崩溃分析接收方；回调标识为 None 表示撤销该安装全部回调
        :param compat_sink: 实例兼容来源接收方；来源为 None 表示撤销该安装全部来源
        :param tool_spawner: 受管工具启动方；未提供时拒绝工具调用
        :param tool_stopper: 受管工具停止方；未提供时拒绝停止
        :param auth_sink: 认证提供方接收方；提供方为 None 表示撤销
        :param enable_block_reason: 插件管理器维护的会话运行限制，覆盖开发和重启入口
        """
        self.data_path: Path = data_path
        self._enable_block_reason = enable_block_reason or (lambda _name: None)
        self._slot_sink = slot_sink
        self._hook_sink = hook_sink
        self._crash_sink = crash_sink
        self._compat_sink = compat_sink
        self._tool_spawner = tool_spawner
        self._tool_stopper = tool_stopper
        self._auth_sink = auth_sink
        self._window_app: AppHandle | None = None
        self.installations: PluginInstallationStore = PluginInstallationStore(data_path)
        self.runs: PluginRunStore = PluginRunStore()
        self._entries: dict[str, _WorkerEntry] = {}
        self._lock = RLock()
        self._locks_by_name: dict[str, RLock] = {}
        self._executable = executable
        self._closed = Event()
        self.settings = PluginSettingsService(data_path, changed=settings_changed)
        self.development = PluginDevelopmentService(self, data_path, changed=development_changed)
        self._monitor_thread: Thread | None = None
        self._resource_refreshed_at = 0.0
        self._operations = operations
        self._game_operations = game_operations
        self._java_queries = java_queries
        self._java_manager = java_manager
        self._account_catalog = PluginAccountResourceCatalog(data_path, account_snapshot) if account_snapshot else None
        self._public_config = public_config
        self._account_skin = account_skin
        self._java_catalog = PluginJavaResourceCatalog(data_path, java_queries) if java_queries is not None else None
        self._file_storage = PluginFileStorage(data_path)
        self._network_service = PluginNetworkService()
        self._instance_profiles = instance_profiles
        self._instance_queries = instance_queries
        self._mc_factory = mc_factory
        self._catalog = (
            PluginResourceCatalog(data_path, instance_roots, instance_queries.query_instance_names)
            if instance_queries is not None and instance_roots is not None
            else None
        )
        self._resource_pages = ResourcePages()

    def bind_window_app(self, app_handle: AppHandle) -> None:
        """
        在原生宿主就绪后绑定独立窗口入口，不从插件数据接收应用句柄。

        :param app_handle: 当前应用唯一 Tauri 句柄
        :raises RuntimeError: 已有不同的原生宿主时抛出
        """
        with self._lock:
            if self._window_app is not None:
                raise RuntimeError("原生窗口宿主已经绑定")
            self._window_app = app_handle

    def refresh_resources(self) -> int:
        """
        在授权锁外检查共享资源身份，然后发布纯内存的有效范围快照。

        :return: 资源目录 revision
        :raises InstanceReadError: 未提供资源服务或目录记录损坏时抛出
        """
        if self._catalog is None:
            raise InstanceReadError("当前宿主未提供实例资源", "PLUGIN_API_METHOD_NOT_FOUND")
        try:
            revision = self._catalog.refresh(discover=True)
        except (OSError, ValueError) as exc:
            if isinstance(exc, InstanceReadError):
                raise
            raise InstanceReadError("实例资源目录刷新失败", "PLUGIN_RESOURCE_CATALOG_INVALID") from exc
        return revision

    def _refresh_host_resources(self) -> None:
        """
        刷新插件可见的实例、Java 与账户目录，故障仅记录日志，不阻断运行。
        """
        for refresh, error_type, message in (
            (self.refresh_resources, InstanceReadError, "实例资源当前不可用"),
            (self.refresh_java_resources, JavaError, "Java 资源当前不可用"),
            (self.refresh_account_resources, ProtocolError, "账户资源当前不可用"),
        ):
            try:
                refresh()
            except error_type:
                get_logger("PluginWorkerService").debug(message)

    def refresh_account_resources(self, *, discover: bool = True) -> int:
        """
        核验账户授权目录，任何读取故障均撤去相关可用范围。

        :param discover: 仅可信审阅可发现新的账户身份
        :return: 当前账户目录版本
        :raises ProtocolError: 目录或领域快照不可用时抛出
        """
        if self._account_catalog is None:
            raise ProtocolError("当前宿主未提供账户查询", "PLUGIN_API_METHOD_NOT_FOUND")
        try:
            revision = self._account_catalog.refresh(discover=discover)
        except (AccountError, OSError, ValueError, ProtocolError) as error:
            raise ProtocolError("账户授权目录无法刷新", "PLUGIN_RESOURCE_CATALOG_INVALID") from error
        return revision

    def refresh_java_resources(self, *, discover: bool = True) -> int:
        """
        刷新 Java 授权身份，故障时只拒绝相关资源能力。

        :param discover: 仅可信审阅允许发现新身份
        :return: Java 资源目录版本
        :raises JavaError: 服务缺失、索引损坏或对象无法核实时抛出
        """
        if self._java_catalog is None:
            raise JavaError("当前宿主未提供 Java 资源", "PLUGIN_API_METHOD_NOT_FOUND")
        try:
            revision = self._java_catalog.refresh(discover=discover)
        except (OSError, ValueError, JavaError) as exc:
            if isinstance(exc, JavaError):
                raise
            raise JavaError("Java 授权目录无法刷新", "PLUGIN_RESOURCE_CATALOG_INVALID") from exc
        return revision

    def register(
        self,
        metadata: object,
        root_path: Path,
        source: Literal["directory", "package"],
        dependency: PluginDependencyDirectory | None = None,
        *,
        allow_network: bool = False,
        development_token: str | None = None,
        source_path: Path | None = None,
    ) -> None:
        """
        只登记已校验清单和内容身份；内容变更先停止旧运行，再建立新会话。

        :param metadata: JSON 清单边界输入
        :param root_path: 已安装代码根目录
        :param source: 宿主确定的目录或归档来源
        :param dependency: 已完成校验的锁定依赖
        :param allow_network: 安装阶段明确允许时补齐锁内 Wheel；发现阶段默认离线
        :param development_token: 仅宿主开发服务传入的会话准入
        :param source_path: 开发运行的实际安装目录，普通运行省略
        :raises ValueError: 清单、内容或安装身份无效时抛出
        """
        if self._closed.is_set():
            raise ProtocolError("插件宿主已经关闭", "PLUGIN_HOST_CLOSED")
        manifest = WorkerManifest.model_validate(metadata)
        with self._lock:
            previous = self._entries.get(manifest.name)
        if (
            development_token is None
            and previous is not None
            and previous.development_token is not None
            and root_path == previous.source_path
            and self.development.manages(manifest.name)
        ):
            return
        lock_file = root_path / "locks" / f"{current_plugin_target()}.txt"
        binding = PluginContentBinding.capture(
            root_path, current_plugin_target(), lock_file if lock_file.is_file() else None
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
                    and previous.root_path == root_path
                    and previous.development_token == development_token
                ):
                    return
            if previous:
                self.stop(manifest.name)
            if source == "directory" and development_token is None:
                dependency = PluginPackagePreparer(self.data_path).prepare_dependencies(
                    root_path, tuple(manifest.python_dependencies), allow_network=allow_network
                )
            supervisor = WorkerSupervisor(
                self._executable, host_handler=lambda request: self._handle(manifest.name, request)
            )
            session = self.runs.open_session(installation_id, binding, supervisor.run_id)
            entry = _WorkerEntry(
                manifest,
                root_path,
                installation_id,
                dependency,
                source,
                supervisor,
                session,
                lock=installation_lock,
                source_path=source_path or root_path,
                development_token=development_token,
            )
            with self._lock:
                self._entries[manifest.name] = entry

    def directory_sources(self) -> tuple[DevelopmentSource, ...]:
        """
        返回宿主登记的第三方目录来源，执行副本不替代实际安装路径。

        :return: 目录安装的独立摘要
        """
        with self._lock:
            return tuple(
                DevelopmentSource(
                    name,
                    entry.installation_id,
                    (entry.source_path or entry.root_path).resolve(),
                    entry.root_path.resolve(),
                    entry.manifest,
                    entry.state,
                    entry.development_token is not None,
                    entry.cleanup_state == "pending",
                )
                for name, entry in self._entries.items()
                if entry.source == "directory"
            )

    def development_approval(self, name: str) -> str:
        """
        为已登记的目录安装建立本进程开发凭据，仅标记开发运行。

        :param name: 正常流程决定启用的目录安装
        :return: 本进程的开发会话凭据
        """
        self._entry(name)
        return uuid4().hex

    def activate_development(self, name: str, code_path: Path, token: str, *, frontend_ready: bool = True) -> None:
        """
        复用原安装及依赖，在旧运行实际停止后启动稳定开发副本。

        :param name: 安装名称
        :param code_path: 开发服务创建并验证的副本
        :param token: 已批准开发会话
        :param frontend_ready: 可信主界面是否已经就绪，首次启动不得提前调用前端生命周期
        """
        entry = self._entry(name)
        with entry.lock:
            if not self.development.wants_enabled(name):
                raise ProtocolError("开发运行已被用户停止", "PLUGIN_SESSION_EXPIRED")
            if entry.development_token is None:
                entry.session.binding.verify()
            self.register(
                json.loads((code_path / "plugin.json").read_bytes()),
                code_path,
                "directory",
                entry.dependency,
                development_token=token,
                source_path=entry.source_path or entry.root_path,
            )
            self.enable(name, frontend_ready=frontend_ready)

    def restore_directory(self, name: str, source_path: Path) -> None:
        """
        停止开发运行后恢复实际源目录的普通审阅资格，不自动启用。

        :param name: 安装名称
        :param source_path: 宿主原始安装目录
        """
        self.stop(name)
        if not source_path.is_dir():
            with self._lock:
                self._entries.pop(name, None)
            return
        self.register(json.loads((source_path / "plugin.json").read_bytes()), source_path, "directory")

    def settings_view(self, name: str) -> dict[str, JsonValue]:
        """
        为可信管理页读取当前定义，插件禁用后也不执行代码探测。

        :param name: 已安装名称
        :return: 统一配置视图
        """
        self.run_identity(name)
        entry = self._entry(name)
        with entry.lock:
            return self.settings.view(
                entry.installation_id,
                entry.session.binding.content_digest,
                entry.session.run_id if entry.state == "enabled" else None,
            )

    def update_settings(
        self,
        name: str,
        content_digest: str,
        schema_revision: str,
        expected_revision: int,
        values: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """
        为可信主窗口保存统一配置，同时复核内容、定义和实际运行准入。

        :param name: 安装名称
        :param content_digest: 用户看到的内容身份
        :param schema_revision: 用户看到的定义版本
        :param expected_revision: 用户看到的值版本
        :param values: 本次明确提交的键值
        :return: 实际保存后的版本
        """
        entry = self._entry(name)
        with entry.lock:
            if content_digest != entry.session.binding.content_digest:
                raise ProtocolError("插件内容已经变化", "PLUGIN_SETTING_SCHEMA_CHANGED")
            lease = entry.session.lease() if entry.state == "enabled" else None

            def accept() -> None:
                entry.session.binding.verify()
                if entry.cleanup_state == "pending":
                    raise ProtocolError("插件仍在清理", "PLUGIN_HOST_CLEANUP_PENDING")
                if lease is not None:
                    entry.session.accept(lease, lambda: None)

            revision = self.settings.update(
                entry.installation_id,
                content_digest,
                values,
                expected_revision,
                accept,
                run_id=entry.session.run_id if entry.state == "enabled" else None,
                schema_revision=schema_revision,
            )
            return {"revision": revision}

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
        建立新运行进程，生命周期实际成功后报告 enabled。

        :param name: 当前安装名称
        :param frontend_ready: 可信主界面是否已经就绪
        :raises ProtocolError: 平台、授权或生命周期未完成时抛出
        """
        reason = self._enable_block_reason(name)
        if reason:
            raise ProtocolError("本次运行禁止启用此插件，请调整启动参数后重启", reason)
        entry = self._entry(name)
        with entry.lock:
            if self._closed.is_set():
                raise ProtocolError("插件宿主已经关闭", "PLUGIN_HOST_CLOSED")
            if entry.state == "enabled":
                return
            if entry.cleanup_state == "pending":
                raise ProtocolError("上一运行仍在完成清理", "PLUGIN_HOST_CLEANUP_PENDING")
            if entry.session.view().error_code == "PLUGIN_SESSION_EXPIRED":
                entry.supervisor = WorkerSupervisor(
                    self._executable, host_handler=lambda request: self._handle(name, request)
                )
                entry.session = self.runs.open_session(
                    entry.installation_id, entry.session.binding, entry.supervisor.run_id
                )
            self._refresh_host_resources()
            entry.session.binding.verify()
            view = entry.session.view()
            if view.error_code:
                raise PluginRunError("插件运行记录不可用", view.error_code)
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
                    mc_factory=self._mc_factory,
                    public_queries=RuntimePublicQueries(
                        entry.session,
                        self._account_catalog,
                        self.refresh_account_resources,
                        self._public_config,
                        self._account_skin,
                    ),
                    java=RuntimeJava(
                        entry.session,
                        self._java_queries,
                        self._java_catalog,
                        self.refresh_java_resources,
                        self._java_manager,
                    )
                    if self._java_queries is not None and self._java_catalog is not None
                    else None,
                    resources=RuntimeResources(
                        entry.session, self._catalog, self._instance_queries, self.refresh_resources
                    )
                    if self._catalog is not None and self._instance_queries is not None
                    else None,
                    slot_sink=partial(self._publish_slot, name),
                    hook_sink=partial(self._publish_hook, name),
                    crash_sink=partial(self._publish_crash, name),
                    compat_sink=partial(self._publish_compat, name),
                    online_sink=partial(self._publish_auth, name),
                    custom_ui=entry.manifest.custom_ui,
                    window_app=lambda: self._window_app,
                    plugin_name=entry.manifest.name,
                    settings_service=self.settings,
                    tool_channel=ToolChannel(
                        run=partial(self._tool_run, name),
                        stop=partial(self._tool_stop, name),
                    )
                    if self._tool_spawner is not None and self._tool_stopper is not None
                    else None,
                    cross_plugin=CrossPluginChannel(
                        list_commands=partial(self._cross_plugin_list, name),
                        call=partial(self._cross_plugin_call, name),
                    ),
                )
                entry.broker.initialize_settings()
                entry.supervisor.authorize_load(
                    entry.session, entry.manifest.entry_file, entry.dependency, entry_class=entry.manifest.entry_class
                )
                self._phase(entry, "load", "loaded")
                self._phase(entry, "enable", "loaded")
                self.settings.publish(entry.installation_id, entry.session.run_id)
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
        reply = entry.supervisor.request(
            "lifecycle.invoke",
            {"phase": phase, "run_state": previous},
            timeout=lifecycle_hook_timeout_seconds,
        )
        if reply.get("ok") is not True:
            raise ProtocolError("插件生命周期执行失败", "PLUGIN_WORKER_EXECUTION_FAILED")

    def _publish_slot(self, name: str, payload: dict[str, JsonValue]) -> None:
        """
        把已获准的插槽内容交给可信界面；未绑定接收方时明确失败。

        :param name: 安装名称
        :param payload: 已通过边界校验的插槽内容
        :raises ProtocolError: 宿主未提供界面注入时抛出
        """
        if self._slot_sink is None:
            raise ProtocolError("当前宿主未提供界面注入", "PLUGIN_API_METHOD_NOT_FOUND")
            raise ProtocolError("界面注入已被用户关闭", "PLUGIN_UI_INJECTION_DISABLED")
        self._slot_sink(name, payload)

    def _publish_hook(self, name: str, phase: str | None, callback_ref: str | None, required: bool) -> None:
        """
        把已获准的启动钩子登记交给管理器；未绑定接收方时明确失败。

        :param name: 安装名称
        :param phase: 钩子阶段，None 表示撤销全部
        :param callback_ref: 本运行登记的回调标识
        :param required: 前置失败时是否阻止启动
        """
        if self._hook_sink is None:
            raise ProtocolError("当前宿主未提供启动钩子", "PLUGIN_API_METHOD_NOT_FOUND")
        self._hook_sink(name, phase, callback_ref, required)

    def _publish_crash(self, name: str, callback_ref: str | None) -> None:
        """
        把已获准的崩溃分析回调登记交给管理器。

        :param name: 安装名称
        :param callback_ref: 本运行登记的回调标识，None 表示撤销
        """
        if self._crash_sink is None:
            raise ProtocolError("当前宿主未提供崩溃分析", "PLUGIN_API_METHOD_NOT_FOUND")
        self._crash_sink(name, callback_ref)

    def _publish_auth(self, name: str, kind: str, definition: object | None) -> None:
        """
        把已获准的认证提供方交给管理器。

        :param name: 安装名称
        :param kind: 登记类型，固定为 auth
        :param definition: 提供方定义；None 表示撤销
        """
        if self._auth_sink is None:
            raise ProtocolError("当前宿主未提供第三方登录", "PLUGIN_API_METHOD_NOT_FOUND")
        provider_id = None
        lease = None
        if definition is not None:
            entry = self._entry(name)
            lease = entry.session.lease()
            provider_id = f"worker.{entry.installation_id}.{getattr(definition, 'provider_id', '')}"
        self._auth_sink(name, provider_id, definition, lease)

    def _clear_auth(self, name: str) -> None:
        """
        撤销该安装登记的全部认证提供方，禁用后不再出现在登录列表。

        :param name: 安装名称
        """
        if self._auth_sink is not None:
            self._auth_sink(name, None, None, None)

    def _tool_run(self, name: str, definition: object, args: tuple[str, ...]) -> str:
        """
        拼装受管工具命令并启动进程，可执行文件必须落在插件包内。

        :param name: 安装名称
        :param definition: 已校验的工具声明
        :param args: 调用方追加的参数
        :return: 进程标识
        :raises ProtocolError: 通道缺失、文件越界或不存在时抛出
        """
        if self._tool_spawner is None:
            raise ProtocolError("当前宿主未提供受管工具", "PLUGIN_API_METHOD_NOT_FOUND")
        entry = self._entry(name)
        root = entry.root_path.resolve()
        executable = str(getattr(definition, "executable", ""))
        target = (root / executable).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ProtocolError("工具可执行文件不在插件包内", "PLUGIN_TOOL_INVALID")
        title = str(getattr(definition, "title", "tool"))
        fixed = tuple(getattr(definition, "args", ()))
        return self._tool_spawner(name, title, [str(target), *fixed, *args], str(root))

    def _tool_stop(self, name: str, process_ref: str) -> bool:
        """
        停止本插件启动的工具进程，归属不符时拒绝。

        :param name: 安装名称
        :param process_ref: 进程标识
        :return: 是否已确认停止
        :raises ProtocolError: 通道缺失或归属不符时抛出
        """
        if self._tool_stopper is None:
            raise ProtocolError("当前宿主未提供受管工具", "PLUGIN_API_METHOD_NOT_FOUND")
        return self._tool_stopper(name, process_ref)

    def _publish_compat(self, name: str, source: str | None, title: str | None, callback_ref: str | None) -> None:
        """
        把已获准的实例兼容来源交给管理器。

        :param name: 安装名称
        :param source: 来源标识，None 表示撤销
        :param title: 来源标题
        :param callback_ref: 本运行登记的回调标识
        """
        if self._compat_sink is None:
            raise ProtocolError("当前宿主未提供实例兼容", "PLUGIN_API_METHOD_NOT_FOUND")
        self._compat_sink(name, source, title, callback_ref)

    def _clear_compat(self, name: str) -> None:
        """
        撤销该安装登记的全部实例兼容来源，禁用或卸载后不再参与识别。

        :param name: 安装名称
        """
        if self._compat_sink is not None:
            self._compat_sink(name, None, None, None)

    def instance_ref(self, root_path: Path, version_id: str) -> str | None:
        """
        把实例路径换算成已索引的资源引用，未索引时返回空。

        :param root_path: 实例所属游戏根
        :param version_id: 实例版本目录名
        :return: 资源引用或空
        """
        if self._catalog is None:
            return None
        return self._catalog.instance_ref_for(root_path, version_id)

    def available_instance_refs(self, name: str) -> frozenset[str]:
        """
        返回该安装启用时可投递的实例引用，供宿主判断能否交付实例上下文。

        :param name: 安装名称
        :return: 当前目录中有效的实例或目录引用
        """
        entry = self._entry(name)
        if entry.state != "enabled" or entry.session is None or self._catalog is None:
            return frozenset()
        return self._catalog.active_root_refs()

    def _clear_crash(self, name: str) -> None:
        """
        撤销该安装登记的全部分析回调，禁用或卸载后不再参与分析。

        :param name: 安装名称
        """
        if self._crash_sink is not None:
            self._crash_sink(name, None)

    def _clear_hooks(self, name: str) -> None:
        """
        撤销该安装登记的启动钩子，禁用或卸载后不再触发。

        :param name: 安装名称
        """
        if self._hook_sink is not None:
            self._hook_sink(name, None, None, False)

    def invoke_hook(
        self,
        name: str,
        callback_ref: str,
        payload: dict[str, JsonValue],
        timeout: float = 5.0,
        *,
        lease: PluginLease | None = None,
    ) -> JsonValue:
        """
        宿主调用插件登记的钩子回调，失败按命令失败上报。

        :param name: 安装名称
        :param callback_ref: 本运行登记的回调标识
        :param payload: 有界启动上下文
        :param timeout: 最长等待秒数
        :param lease: 敏感登记的原始准入；调用与交付前均须继续有效
        :return: 回调返回值
        :raises ProtocolError: 插件未启用或回调失败时抛出
        """
        entry = self._entry(name)
        if entry.state != "enabled" or entry.broker is None:
            raise ProtocolError("插件未启用", "PLUGIN_TARGET_DISABLED")
        if lease is not None:
            entry.session.check(lease)
        reply = entry.broker.invoke_callback(callback_ref, payload, timeout)
        if lease is not None:
            entry.session.check(lease)
        if reply.get("ok") is not True or not isinstance(reply.get("data"), dict):
            raise ProtocolError("启动钩子执行失败", "PLUGIN_COMMAND_FAILED")
        return reply["data"].get("value")

    def _cross_plugin_list(self, caller: str) -> list[dict[str, JsonValue]]:
        """
        列出其他已启用插件导出的命令，只含公开描述。

        :param caller: 发起发现的安装名称
        :return: 带所属插件名的命令描述
        """
        items: list[dict[str, JsonValue]] = []
        for name, entry in tuple(self._entries.items()):
            if name == caller or entry.state != "enabled" or entry.broker is None:
                continue
            for command in entry.broker.exported_commands():
                items.append({"plugin": name, **command})
        return items

    def _cross_plugin_call(
        self, caller: str, target: str, command: str, args: dict[str, JsonValue], timeout: float
    ) -> JsonValue:
        """
        调用目标插件已导出的命令，双方都必须处于可调用状态。

        :param caller: 发起调用的安装名称
        :param target: 目标安装名称
        :param command: 目标命令名
        :param args: 已由目标 schema 复核的输入
        :param timeout: 剩余等待时间
        :return: 目标命令的输出
        :raises ProtocolError: 目标未启用、未导出该命令或调用层数超限时抛出
        """
        if target == caller:
            raise ProtocolError("不能跨插件调用自身", "PLUGIN_TARGET_INVALID")
        depth = _cross_call_depth.get()
        if depth >= 3:
            raise ProtocolError("跨插件调用层数超过上限", "PLUGIN_CALL_DEPTH_EXCEEDED")
        entry = self._entry(target)
        if entry.state != "enabled" or entry.broker is None:
            raise ProtocolError("目标插件未启用", "PLUGIN_TARGET_DISABLED")
        if command not in {item["command"] for item in entry.broker.exported_commands()}:
            raise ProtocolError("目标命令未导出", "PLUGIN_TARGET_COMMAND_NOT_EXPORTED")
        token = _cross_call_depth.set(depth + 1)
        try:
            return entry.broker.call_command(command, args, timeout)
        finally:
            _cross_call_depth.reset(token)

    def clear_all_slots(self) -> None:
        """
        撤销全部第三方插槽内容，用户关闭总开关后立即生效。

        只清理第三方安装；系统插件的界面不受该开关影响。
        """
        if self._slot_sink is None:
            return
        for name in tuple(self._entries):
            self._slot_sink(name, {"kind": "clear"})

    def reload_ui_injection_plugins(self) -> None:
        """
        总开关重新打开后重启已获准注入的插件，让它们重新登记插槽内容。

        只处理已启用且当前授权允许 ``ui.inject`` 的第三方安装：插槽内容只在插件
        加载时登记，关闭总开关时宿主已撤销全部内容，因此恢复显示必须重跑一次加载。
        单个插件重启失败只记录警告，不阻断其余插件，也不改变开关本身。

        :raises ProtocolError: 宿主未提供界面注入或重启后仍无法启用
        """
        for name in tuple(self._entries):
            entry = self._entries.get(name)
            if entry is None or entry.state != "enabled":
                continue
            try:
                allowed = any(
                    item.capability == "ui.inject" and item.decision == "allow" for item in entry.session.view().items
                )
            except (ProtocolError, ValueError):
                continue
            if not allowed:
                continue
            try:
                self.stop(name)
                self.enable(name)
            except (ProtocolError, OSError, RuntimeError):
                get_logger("PluginWorkerService").warning("插件界面注入恢复时无法重新加载 %s", name)

    def _clear_slots(self, name: str) -> None:
        """
        撤销该安装已注入的主窗口内容，禁用或卸载后不再保留。

        :param name: 安装名称
        """
        if self._slot_sink is not None:
            self._slot_sink(name, {"kind": "clear"})

    def stop(self, name: str) -> None:
        """
        先撤销本运行准入和注册，再有界回收 Worker；失败保留实际清理状态。

        :param name: 要停止的安装
        :raises PluginRuntimeError: 进程树尚未确认回收时抛出
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
                        raise PluginRuntimeError("插件宿主调用仍在完成中", "PLUGIN_HOST_CLEANUP_PENDING")
            except (ProtocolError, OSError, PluginRuntimeError) as exc:
                entry.cleanup_state = "pending"
                entry.error_code = getattr(exc, "error_code", "PLUGIN_JOB_STOP_PENDING")
                raise
            entry.broker = None
            self._clear_slots(name)
            self._clear_hooks(name)
            self._clear_crash(name)
            self._clear_compat(name)
            self._clear_auth(name)
            entry.state = "disabled"
            entry.cleanup_state = "completed"
            if entry.error_code in {"PLUGIN_JOB_STOP_PENDING", "PLUGIN_HOST_CLEANUP_PENDING"}:
                entry.error_code = None

    def run_identity(self, name: str) -> dict[str, JsonValue]:
        """
        为可信管理入口返回本安装的运行身份，停止后建立新的待启用会话。

        :param name: 安装名称
        :return: 安装身份、内容摘要与当前运行代次
        """
        entry = self._entry(name)
        root_path = entry.source_path or entry.root_path
        try:
            metadata = json.loads((root_path / "plugin.json").read_bytes())
        except OSError as exc:
            raise ProtocolError("插件安装目录不可用", "PLUGIN_RESOURCE_NOT_FOUND") from exc
        self.register(metadata, root_path, entry.source, entry.dependency)
        entry = self._entry(name)
        with entry.lock:
            if entry.session.view().error_code == "PLUGIN_SESSION_EXPIRED" and entry.cleanup_state != "pending":
                entry.supervisor = WorkerSupervisor(
                    self._executable, host_handler=lambda request: self._handle(name, request)
                )
                entry.session = self.runs.open_session(
                    entry.installation_id, entry.session.binding, entry.supervisor.run_id
                )
            self._refresh_host_resources()
            view = entry.session.view()
            return {
                "installation_id": view.installation_id,
                "content_digest": view.content_digest,
                "run_epoch": view.run_epoch,
                "error_code": view.error_code,
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
            self.development.forget(name)
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
        在运行中检测实际进程退出、通信故障和内容失效，不依赖页面轮询清理。

        进程存活检查保持 250ms 节奏；实例、Java 与账户目录刷新按
        `resource_refresh_interval_seconds` 降频，避免宿主常驻线程以 4Hz
        全量校验目录。
        """
        while not self._closed.wait(0.25):
            with self._lock:
                entries = tuple(self._entries.items())
            should_refresh_resources = (
                monotonic() - self._resource_refreshed_at >= self.resource_refresh_interval_seconds and entries
            )
            if should_refresh_resources:
                self._resource_refreshed_at = monotonic()
                self._refresh_host_resources()
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
                            and not view.error_code
                        ):
                            continue
                        failure_code = entry.broker.error_code if entry.broker else None
                        self.stop(name)
                        entry.state = "failed"
                        entry.error_code = view.error_code or failure_code or "PLUGIN_WORKER_EXITED"
                    except (OSError, ValueError, PluginRuntimeError):
                        entry.cleanup_state = "pending"
                        get_logger("PluginWorkerService").warning("插件运行清理尚未完成，需要重试停止")

    def close(self) -> None:
        """
        停止全部已登记 Worker，失败时保留对象和句柄供重试。
        """
        failures: list[BaseException] = []
        try:
            self.development.close()
        except (OSError, ValueError, PluginRuntimeError) as exc:
            failures.append(exc)
        self._closed.set()
        self._resource_pages.clear()
        if self._monitor_thread is not None:
            self._monitor_thread.join(1)
        for name in reversed(tuple(self._entries)):
            try:
                self.stop(name)
            except (OSError, ValueError, PluginRuntimeError) as exc:
                failures.append(exc)
        is_network_idle = self._network_service.close()
        if failures:
            raise PluginRuntimeError("部分插件进程树尚未确认停止", "PLUGIN_JOB_STOP_PENDING") from failures[0]
        if not self._file_storage.close() or not is_network_idle:
            raise PluginRuntimeError("插件宿主资源尚未释放", "PLUGIN_HOST_CLEANUP_PENDING")
