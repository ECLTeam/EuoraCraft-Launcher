# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将单次 Worker RPC 绑定到授权和自身命令、设置、事件，不转发宿主命令表。
#
# 公开接口：
#   - class PluginHostBroker — 最小 Host API 的身份、去重及结果交付边界。
# ============================================================

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from pydantic import ValidationError

from ECL.foundation.version import __version__
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.plugins.grant_models import GrantLease, PermissionDeclaration
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.runtime.download_api import RuntimeDownloads
from ECL.plugins.runtime.file_api import RuntimeFiles
from ECL.plugins.runtime.file_storage import PluginFileStorage
from ECL.plugins.runtime.game_install import RuntimeGameInstall
from ECL.plugins.runtime.game_launch import RuntimeGameLaunch
from ECL.plugins.runtime.game_queries import RuntimeGameCatalog
from ECL.plugins.runtime.game_resources import RuntimeGameResources
from ECL.plugins.runtime.instance_profiles import RuntimeInstanceProfiles
from ECL.plugins.runtime.java_api import RuntimeJava
from ECL.plugins.runtime.models import (
    CommandDefinition,
    HtmlSlotDefinition,
    LaunchHookDefinition,
    PanelData,
    PanelDefinition,
    PanelGroupNode,
    PanelNode,
    PanelSettingNode,
    SettingDefinition,
    VueSlotDefinition,
    validate_fields,
)
from ECL.plugins.runtime.modpacks import RuntimeModpacks
from ECL.plugins.runtime.network_api import PluginNetworkService, RuntimeNetwork
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.plugins.runtime.public_queries import RuntimePublicQueries
from ECL.plugins.runtime.resource_queries import RuntimeResourceCatalog
from ECL.plugins.runtime.resources import RuntimeResources
from ECL.services.operations import OperationManager
from ECL.utils import atomic_write_text, get_logger
from ECL.utils.instance_reading import InstanceReadError

if TYPE_CHECKING:
    from ECL.services.game.instance_profile_transactions import InstanceProfileTransactions
    from ECL.services.game.plugin_games import PluginGameCoordinator


@dataclass(frozen=True, slots=True)
class CrossPluginChannel:
    """
    跨插件发现与调用的宿主通道，由运行服务实现并保证双方准入。

    发现只返回已导出命令的公开描述；调用在目标插件进程内按自身命令
    schema 校验，调用方拿不到对方的回调、凭据或对象。
    """

    list_commands: Callable[[], list[dict[str, JsonValue]]]
    call: Callable[[str, str, dict[str, JsonValue], float], JsonValue]


@dataclass(slots=True)
class _Receipt:
    fingerprint: str
    grant_epoch: int
    ready: Event = field(default_factory=Event)
    result: dict[str, JsonValue] | None = None
    error_code: str | None = None
    is_owner_control: bool = False


def _panel_setting_keys(nodes: list[PanelNode]) -> Iterator[str]:
    """
    收集面板树引用的设置键，供注册时与当前设置 schema 核对。

    :param nodes: 面板节点树
    :return: 设置键迭代器，不包含其他节点类型
    """
    for node in nodes:
        if isinstance(node, PanelSettingNode):
            yield node.key
        elif isinstance(node, PanelGroupNode):
            yield from _panel_setting_keys(node.children)


class PluginHostBroker:
    """
    只处理本连接的已实现能力；所有对象在运行结束或授权失效后不可继续使用。

    回调与磁盘操作不持有注册表锁。变更回执在本 run 内保留，容量耗尽时拒绝新提交。
    """

    implemented_capabilities: ClassVar[frozenset[str]] = frozenset(
        {
            "instances.read",
            "instances.manage",
            "network.read",
            "java.read",
            "java.use",
            "java.catalog.read",
            "java.install",
            "accounts.read",
            "ui.inject",
        }
    )
    mutation_methods: ClassVar[frozenset[str]] = frozenset(
        {
            "mods.install",
            "mods.delete",
            "mods.set_enabled",
            "resourcepacks.install",
            "resourcepacks.delete",
            "shaders.install",
            "shaders.delete",
            "modpacks.prepare",
            "modpacks.install",
            "modpacks.export_preview",
            "modpacks.export",
            "commands.register",
            "commands.unregister",
            "panels.register",
            "ui.register_html",
            "hooks.register",
            "ui.register_vue",
            "settings.define",
            "settings.set",
            "events.subscribe",
            "events.unsubscribe",
            "commands.call",
            "operations.cancel",
            "files.write_begin",
            "files.write_chunk",
            "files.write_commit",
            "files.delete",
            "files.replace_begin",
            "instances.profile_patch",
            "downloads.submit",
            "game.install",
            "game.launch",
            "game.stop",
            "java.install_plan",
            "java.install",
        }
    )
    max_receipts: ClassVar[int] = 1024

    def __init__(
        self,
        session: PluginGrantSession,
        data_path: Path,
        invoke: Callable[[str, dict[str, JsonValue], float], dict[str, JsonValue]],
        *,
        run_state: Callable[[], str],
        resources: RuntimeResources | None = None,
        operations: OperationManager | None = None,
        file_storage: PluginFileStorage | None = None,
        network_service: PluginNetworkService | None = None,
        instance_profiles: InstanceProfileTransactions | None = None,
        game_operations: PluginGameCoordinator | None = None,
        java: RuntimeJava | None = None,
        public_queries: RuntimePublicQueries | None = None,
        slot_sink: Callable[[dict[str, JsonValue]], None] | None = None,
        cross_plugin: CrossPluginChannel | None = None,
        hook_sink: Callable[[str, str, bool], None] | None = None,
    ) -> None:
        """
        绑定宿主持有的会话和回调通道，不接受客户端提供主体。

        :param session: 当前运行的唯一授权会话
        :param data_path: 宿主数据根目录
        :param invoke: 当前 Worker 的受控请求方法
        :param run_state: 唯一运行服务提供的当前状态
        :param operations: 应用唯一任务服务；没有该服务时不开放任务控制
        :param resources: 当前运行的共享实例资源表；未提供时拒绝共享资源 API
        :param file_storage: 应用拥有的私有文件存储；缺少任务服务时不开放文件 API
        :param network_service: 应用唯一只读网络服务；未提供时不开放网络方法
        :param instance_profiles: 应用唯一共享实例资料事务，缺失时拒绝资料写接口
        :param game_operations: 应用唯一游戏业务入口，未提供时拒绝游戏方法
        :param java: 当前运行的 Java 查询及实际探测资源，缺失时拒绝 Java 方法
        :param public_queries: 明确选定账户与公开配置的白名单查询
        :param slot_sink: 可信界面插槽接收方；未提供时拒绝界面注入方法
        :param cross_plugin: 跨插件发现与调用通道；未提供时拒绝相关方法
        :param hook_sink: 启动钩子接收方；未提供时拒绝钩子登记
        """
        self.session: PluginGrantSession = session
        self.error_code: str | None = None
        self._invoke = invoke
        self._run_state = run_state
        self._slot_sink = slot_sink
        self._cross_plugin = cross_plugin
        self._hook_sink = hook_sink
        self._resources = resources
        self.game_catalog: RuntimeGameCatalog | None = (
            RuntimeGameCatalog(session, game_operations) if game_operations is not None else None
        )
        self.operations: RuntimeOperations | None = RuntimeOperations(session, operations) if operations else None
        self.game_install: RuntimeGameInstall | None = (
            RuntimeGameInstall(session, game_operations, resources, self.operations)
            if game_operations is not None and resources is not None and self.operations is not None
            else None
        )
        self.game_launch: RuntimeGameLaunch | None = (
            RuntimeGameLaunch(session, game_operations, resources, self.operations, java)
            if game_operations is not None and resources is not None and self.operations is not None
            else None
        )
        self.files: RuntimeFiles | None = (
            RuntimeFiles(session, file_storage, self.operations) if file_storage and self.operations else None
        )
        self.network: RuntimeNetwork | None = RuntimeNetwork(session, network_service) if network_service else None
        self.java: RuntimeJava | None = java
        self.public_queries = public_queries
        self.resource_catalog = (
            RuntimeResourceCatalog(session, game_operations) if game_operations is not None else None
        )
        self.game_resources: RuntimeGameResources | None = (
            RuntimeGameResources(resources, game_operations, self.files, self.operations)
            if resources is not None
            and game_operations is not None
            and self.files is not None
            and self.operations is not None
            else None
        )
        self.modpacks: RuntimeModpacks | None = (
            RuntimeModpacks(session, game_operations, resources, self.files, self.operations, self.network, data_path)
            if game_operations is not None
            and resources is not None
            and self.files is not None
            and self.operations is not None
            and self.network is not None
            else None
        )
        self.downloads: RuntimeDownloads | None = (
            RuntimeDownloads(session, self.files, self.network, self.operations)
            if self.files is not None and self.network is not None and self.operations is not None
            else None
        )
        self.instance_profiles: RuntimeInstanceProfiles | None = (
            RuntimeInstanceProfiles(session, resources, instance_profiles, self.operations)
            if resources is not None and instance_profiles is not None and self.operations is not None
            else None
        )
        self._settings_path = data_path / "plugin_settings_v2" / f"{session.view().installation_id}.json"
        self._lock = RLock()
        self._settings_lock = RLock()
        self._closed = Event()
        self._idle = Event()
        self._idle.set()
        self._active_requests = 0
        self._commands: dict[str, tuple[str, CommandDefinition, GrantLease]] = {}
        self._panels: dict[str, tuple[PanelDefinition, GrantLease]] = {}
        self._definitions: dict[str, SettingDefinition] = {}
        self._subscriptions: dict[str, tuple[str, GrantLease]] = {}
        self._receipts: dict[str, _Receipt] = {}
        self._events: Queue[tuple[str, dict[str, JsonValue], GrantLease]] = Queue(32)
        self._sequence = 0
        self._observed_epoch = session.view().grant_epoch
        self._event_thread = Thread(target=self._deliver_events, name="plugin_settings_events", daemon=True)
        self._event_thread.start()

    def handle(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        校验有界请求并仅分派本轮开放的方法；变更请求按本运行去重。

        :param request: 已认证 RPC 请求
        :return: 方法结果或明确失败
        :raises ProtocolError: 授权、输入、内容、状态或方法不允许时抛出
        """
        with self._track_request():
            try:
                request.check_deadline()
                if request.method in {"operations.get", "operations.list", "operations.cancel", "files.write_abort"}:
                    self._ensure_accepting()
                else:
                    self._check_open()
                payload = _json_value(request.params)
                if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False).encode()) > 65536:
                    raise ProtocolError("插件业务载荷超限", "PLUGIN_MESSAGE_TOO_LARGE")
                if request.method not in self.mutation_methods:
                    return self._bounded_result(self._dispatch(request))
                return self._mutate(request, payload)
            except PluginGrantError as exc:
                raise ProtocolError("插件权限检查未通过", exc.error_code) from exc
            except InstanceReadError as exc:
                raise ProtocolError(str(exc), exc.error_code) from exc
            except ValidationError as exc:
                raise ProtocolError("插件方法参数无效", "PLUGIN_SCHEMA_INVALID") from exc

    @contextmanager
    def _track_request(self) -> Iterator[None]:
        """
        跟踪已进入宿主的真实调用，进程退出不替代写入线程的完成确认。
        """
        with self._lock:
            self._active_requests += 1
            self._idle.clear()
        try:
            yield
        finally:
            with self._lock:
                self._active_requests -= 1
                if self._active_requests == 0:
                    self._idle.set()

    def wait_idle(self, timeout: float = 0) -> bool:
        """
        有界等待本运行宿主调用结束，不取消或强杀执行线程。

        :param timeout: 等待上限，零只查询
        :return: 所有已接纳调用均已实际返回时为 True
        """
        if not self._idle.wait(timeout):
            return False
        if self.operations is not None and not self.operations.is_idle():
            return False
        if self.game_launch is not None:
            self.game_launch.release()
        if self.network is not None and not self.network.is_idle():
            return False
        return self.files is None or self.files.is_idle()

    @staticmethod
    def _bounded_result(result: dict[str, JsonValue]) -> dict[str, JsonValue]:
        value = _json_value(result)
        if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 65536:
            raise ProtocolError("插件业务结果超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        return value

    def _mutate(self, request: RpcRequest, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        保存本运行有限回执；重复提交只等待同一结果，不重放已经接受的副作用。
        """
        if not request.mutation_key or len(request.mutation_key) > 64:
            raise ProtocolError("变更请求缺少有效去重键", "PLUGIN_INVALID_REQUEST")
        fingerprint = json.dumps([request.method, payload], sort_keys=True, ensure_ascii=False)
        epoch = 0 if request.method == "operations.cancel" else self.session.view().grant_epoch
        with self._lock:
            receipt = self._receipts.get(request.mutation_key)
            is_new = receipt is None
            if receipt is None:
                if len(self._receipts) >= self.max_receipts:
                    raise ProtocolError("本运行变更回执已达上限", "PLUGIN_RATE_LIMITED")
                receipt = _Receipt(
                    fingerprint,
                    epoch,
                    is_owner_control=request.method
                    in {"operations.cancel", "files.write_commit", "downloads.submit", "files.delete"},
                )
                self._receipts[request.mutation_key] = receipt
            elif receipt.fingerprint != fingerprint:
                raise ProtocolError("同一去重键不能对应不同请求", "PLUGIN_MUTATION_CONFLICT")
        if not is_new:
            return self._wait_receipt(request, receipt)
        try:
            receipt.result = self._bounded_result(self._dispatch(request))
            return receipt.result
        except (ProtocolError, PluginGrantError) as exc:
            receipt.error_code = exc.error_code
            raise
        except ValidationError as exc:
            receipt.error_code = "PLUGIN_SCHEMA_INVALID"
            raise ProtocolError("插件方法参数无效", receipt.error_code) from exc
        except (OSError, ValueError) as exc:
            receipt.error_code = "PLUGIN_STORAGE_FAILED"
            raise ProtocolError("插件变更未能持久化", receipt.error_code) from exc
        except Exception as exc:
            receipt.error_code = "PLUGIN_HOST_FAILED"
            get_logger("PluginHostBroker").exception("插件宿主变更发生未预期异常")
            raise ProtocolError("插件宿主操作未完成", receipt.error_code) from exc
        finally:
            receipt.ready.set()

    def _wait_receipt(self, request: RpcRequest, receipt: _Receipt) -> dict[str, JsonValue]:
        """
        有界等待同一提交，不在回执锁内执行或重新发起业务动作。
        """
        if not receipt.ready.wait(max(0, request.deadline - monotonic())):
            raise ProtocolError("变更结果尚未确认", "PLUGIN_OUTCOME_PENDING")
        if receipt.is_owner_control:
            self._ensure_accepting()
        else:
            self._check_open()
        if not receipt.is_owner_control and receipt.grant_epoch != self.session.view().grant_epoch:
            raise ProtocolError("回执授权版本已失效", "PLUGIN_PERMISSION_REVOKED")
        if receipt.error_code:
            raise ProtocolError("此前变更未完成", receipt.error_code)
        assert receipt.result is not None
        return receipt.result

    def _dispatch(self, request: RpcRequest) -> dict[str, JsonValue]:
        method = request.method
        if method.startswith(("game.", "mods.", "resourcepacks.", "shaders.", "modpacks.")):
            return self._game_service(request)
        if method.startswith(("files.", "java.", "accounts.", "config.")) or method in {
            "operations.get",
            "operations.list",
            "operations.cancel",
            "resources.list",
            "instances.scan",
            "instances.get",
            "instances.profile_patch",
            "network.request",
            "downloads.submit",
        }:
            return self._query_service(request)
        if method.startswith("panels."):
            return self._panel_service(request)
        if method.startswith("ui."):
            return self._ui_service(request)
        if method.startswith("plugins."):
            return self._cross_plugin_service(request)
        if method.startswith("hooks."):
            return self._hook_service(request)
        return self._control_service(request)

    def _control_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        处理运行信息、变更回执、权限视图与自身命令、设置、事件登记。
        """
        method, params = request.method, request.params
        if method == "runtime.info":
            self._keys(params, set())
            return {
                "api_version": 2,
                "protocol_version": 1,
                "launcher_version": __version__,
                "platform": platform.system(),
                "python_abi": sys.implementation.cache_tag,
                "run_state": self._run_state(),
            }
        if method == "mutations.get":
            return self._receipt_view(params)
        if method == "permissions.list":
            return self._permission_view(params)
        if method == "commands.register":
            return self._register_command(params)
        if method in {"commands.unregister", "events.unsubscribe"}:
            return self._release_registration(method, params)
        if method == "commands.call":
            self._keys(params, {"command_ref", "args"})
            reference, args = params["command_ref"], params["args"]
            if not isinstance(reference, str) or not isinstance(args, dict):
                raise ProtocolError("命令调用参数无效", "PLUGIN_SCHEMA_INVALID")
            return {"value": self.call_command(reference, args, max(0.001, request.deadline - monotonic()))}
        if method in {"settings.define", "settings.get", "settings.set"}:
            return self._settings(request)
        if method == "events.subscribe":
            return self._subscribe(params)
        raise ProtocolError("此 Host API 尚未接线", "PLUGIN_API_METHOD_NOT_FOUND")

    def _profile_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        资料写入只交给绑定应用唯一存储的适配器，不从请求重新创建领域服务。
        """
        if self.instance_profiles is None:
            raise ProtocolError("当前宿主未提供实例资料修改", "PLUGIN_API_METHOD_NOT_FOUND")
        return self.instance_profiles.submit(request.params, min(request.deadline, monotonic() + 5))

    @staticmethod
    def _is_java_query(request: RpcRequest) -> bool:
        return request.method.startswith("java.") or (
            request.method == "resources.list" and request.params.get("capability") == "java.read"
        )

    def _query_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        method, params = request.method, request.params
        if self._is_public_query(request):
            return self._public_query(request)
        if self._is_java_query(request):
            return self._java_query(request)
        if method == "instances.profile_patch":
            return self._profile_service(request)
        if method in {"downloads.submit", "network.request"}:
            return self._network_query(request)
        if self._is_file_query(request):
            if self.files is None:
                raise ProtocolError("当前宿主未提供私有文件", "PLUGIN_API_METHOD_NOT_FOUND")
            deadline = min(request.deadline, monotonic() + (30 if method == "files.read" else 5))
            return self.files.handle(method, params, deadline)
        if method in {"operations.get", "operations.list", "operations.cancel"}:
            if self.operations is None:
                raise ProtocolError("当前宿主未提供任务控制", "PLUGIN_API_METHOD_NOT_FOUND")
            return self.operations.handle(method, params)
        if method in {"resources.list", "instances.scan", "instances.get"}:
            return self._instance_query(request)
        raise ProtocolError("服务方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")

    @staticmethod
    def _is_file_query(request: RpcRequest) -> bool:
        return request.method.startswith("files.") or (
            request.method == "resources.list" and request.params.get("capability") == "plugin.base"
        )

    def _network_query(self, request: RpcRequest) -> dict[str, JsonValue]:
        if request.method == "downloads.submit":
            if self.downloads is None:
                raise ProtocolError("当前宿主未提供下载任务", "PLUGIN_API_METHOD_NOT_FOUND")
            return self.downloads.submit(request.params, request.deadline)
        if self.network is None:
            raise ProtocolError("当前宿主未提供网络读取", "PLUGIN_API_METHOD_NOT_FOUND")
        result = self.network.handle(request.params, request.deadline)
        request.check_deadline()
        return result

    @staticmethod
    def _is_public_query(request: RpcRequest) -> bool:
        return request.method.startswith(("accounts.", "config.")) or (
            request.method == "resources.list" and request.params.get("capability") == "accounts.read"
        )

    def _public_query(self, request: RpcRequest) -> dict[str, JsonValue]:
        if self.public_queries is None:
            raise ProtocolError("当前宿主未提供公开查询", "PLUGIN_API_METHOD_NOT_FOUND")
        return self.public_queries.handle(request.method, request.params, request.deadline)

    def _game_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        仅转给当前应用实际绑定的游戏查询或控制门面，未知方法保持拒绝。
        """
        if request.method.startswith("modpacks."):
            if self.modpacks is None:
                raise ProtocolError("当前宿主未提供整合包管理", "PLUGIN_API_METHOD_NOT_FOUND")
            return self.modpacks.handle(request.method, request.params, request.deadline)
        if request.method.startswith(("mods.", "resourcepacks.", "shaders.")):
            if (
                request.method.endswith((".search", ".versions", ".download_info"))
                and self.resource_catalog is not None
            ):
                return self.resource_catalog.handle(request.method, request.params, request.deadline)
            if self.game_resources is None:
                raise ProtocolError("当前宿主未提供游戏资源管理", "PLUGIN_API_METHOD_NOT_FOUND")
            return self.game_resources.handle(request.method, request.params, request.deadline)
        if request.method in {"game.catalog", "game.loaders"} and self.game_catalog is not None:
            return self.game_catalog.handle(request.method, request.params, request.deadline)
        if request.method == "game.install" and self.game_install is not None:
            return self.game_install.submit(request.params, min(request.deadline, monotonic() + 5))
        if self.game_launch is not None:
            if request.method == "game.launch":
                return self.game_launch.launch(request.params, min(request.deadline, monotonic() + 5))
            if request.method == "game.running":
                return self.game_launch.running(request.params, min(request.deadline, monotonic() + 5))
            if request.method == "game.stop":
                return self.game_launch.stop(request.params, min(request.deadline, monotonic() + 5))
        raise ProtocolError("当前宿主未提供此游戏方法", "PLUGIN_API_METHOD_NOT_FOUND")

    def _java_query(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        拒绝未注入的 Java 服务，并限制其真实调用总预算。
        """
        if self.java is None:
            raise ProtocolError("当前宿主未提供 Java 查询", "PLUGIN_API_METHOD_NOT_FOUND")
        if request.method == "java.install":
            if self.operations is None:
                raise ProtocolError("当前宿主未提供任务服务", "PLUGIN_API_METHOD_NOT_FOUND")
            return self.java.install(request.params, self.operations, min(request.deadline, monotonic() + 5))
        return self.java.handle(request.method, request.params, min(request.deadline, monotonic() + 30))

    def _instance_query(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        为实例读取提供统一服务缺失、截止及交付检查。
        """
        if self._resources is None:
            raise ProtocolError("当前宿主未提供实例查询", "PLUGIN_API_METHOD_NOT_FOUND")
        deadline = min(request.deadline, monotonic() + (30 if request.method == "instances.scan" else 5))
        result = self._resources.handle(request.method, request.params, deadline)
        request.check_deadline()
        return result

    def _release_registration(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        self._keys(params, {"registration_ref"} if method == "commands.unregister" else {"subscription_ref"})
        with self._lock:
            if method == "commands.unregister":
                reference = next(
                    (ref for ref, item in self._commands.items() if item[0] == params["registration_ref"]), None
                )
                return {"released": self._commands.pop(reference, None) is not None}
            reference = params["subscription_ref"]
            if not isinstance(reference, str):
                raise ProtocolError("订阅标识无效", "PLUGIN_SCHEMA_INVALID")
            return {"released": self._subscriptions.pop(reference, None) is not None}

    def _permission_view(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        自查询只暴露当前运行引用，不交付资源目录的持久身份。
        """
        self._keys(params, set())
        view = self.session.view()
        items = [dict(asdict(item), resource_refs=list(item.resource_refs)) for item in view.items]
        for item in items:
            if item["capability"] == "accounts.read":
                item["resource_refs"] = (
                    self.public_queries.permission_refs() if self.public_queries and item["decision"] == "allow" else []
                )
            elif item["capability"] in {"java.read", "java.use"}:
                item["resource_refs"] = (
                    self.java.permission_refs(item["capability"]) if self.java and item["decision"] == "allow" else []
                )
            elif item["capability"] in PermissionDeclaration.host_selected_capabilities:
                item["resource_refs"] = (
                    self._resources.permission_refs(item["capability"])
                    if self._resources and item["decision"] == "allow"
                    else []
                )
        return {"items": items, "grant_epoch": view.grant_epoch}

    def _receipt_view(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        查询本运行已经接受的变更，不重放副作用或交付旧授权下的结果。
        """
        self._keys(params, {"mutation_key"})
        key = params["mutation_key"]
        if not isinstance(key, str) or not 0 < len(key) <= 64:
            raise ProtocolError("变更回执标识无效", "PLUGIN_SCHEMA_INVALID")
        with self._lock:
            receipt = self._receipts.get(key)
        if receipt is None:
            raise ProtocolError("本运行没有此变更回执", "PLUGIN_RESOURCE_NOT_FOUND")
        if not receipt.is_owner_control and receipt.grant_epoch != self.session.view().grant_epoch:
            raise ProtocolError("回执授权版本已失效", "PLUGIN_PERMISSION_REVOKED")
        if not receipt.ready.is_set():
            state = "pending"
        elif receipt.error_code:
            state = "failed"
        else:
            state = "succeeded"
        return {
            "state": state,
            "result": receipt.result if state == "succeeded" else None,
            "error_code": receipt.error_code,
        }

    def _register_command(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        在授权接受点原子登记本 run 的命令，实际回调在锁外执行。
        """
        lease = self.session.authorize("plugin.base")
        definition = CommandDefinition.model_validate(params)
        reference, registration = uuid4().hex, uuid4().hex

        def register() -> None:
            with self._lock:
                self._ensure_accepting()
                if len(self._commands) >= 64 or any(
                    item[1].name == definition.name for item in self._commands.values()
                ):
                    raise ProtocolError("命令已登记或超过上限", "PLUGIN_REGISTRATION_CONFLICT")
                self._commands[reference] = (registration, definition, lease)

        self.session.accept(lease, register)
        return {"command_ref": reference, "registration_ref": registration}

    def _subscribe(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        只在授权接受点登记自身设置订阅，不开放完整宿主事件总线。
        """
        self._keys(params, {"event", "callback_ref"})
        lease = self.session.authorize("plugin.base")
        callback = params["callback_ref"]
        if params["event"] != "settings.changed" or not isinstance(callback, str) or len(callback) != 32:
            raise ProtocolError("该事件或回调尚未开放", "PLUGIN_EVENT_NOT_EXPOSED")
        reference = uuid4().hex

        def register() -> None:
            with self._lock:
                self._ensure_accepting()
                if len(self._subscriptions) >= 32:
                    raise ProtocolError("订阅超过上限", "PLUGIN_RATE_LIMITED")
                self._subscriptions[reference] = (callback, lease)

        self.session.accept(lease, register)
        return {"subscription_ref": reference}

    def call_command(self, reference: str, args: dict[str, JsonValue], timeout: float = 30) -> JsonValue:
        """
        调用自身命令，等待期间不持有注册锁，迟到或失效结果不得交付。

        :param reference: 本运行命令引用或本插件命令名称
        :param args: 满足已登记 schema 的参数
        :param timeout: 回调等待预算
        :return: 已通过输出 schema 和授权检查的结果
        :raises ProtocolError: 命令、授权、schema 或回调无效时抛出
        """
        self._check_open()
        lease = self.session.authorize("plugin.base")
        with self._lock:
            item = self._commands.get(reference) or next(
                (v for v in self._commands.values() if v[1].name == reference), None
            )
        if item is None:
            raise ProtocolError("命令不属于本运行", "PLUGIN_RESOURCE_NOT_FOUND")
        definition = item[1]
        self.session.check(item[2])
        validate_fields(definition.input_fields, args)
        self.session.check(lease)
        reply = self._invoke_callback({"callback_ref": definition.callback_ref, "args": args}, timeout)
        if reply.get("ok") is not True or not isinstance(reply.get("data"), dict):
            raise ProtocolError("插件命令执行失败", "PLUGIN_COMMAND_FAILED")
        result = reply["data"].get("value")
        if not isinstance(result, dict):
            raise ProtocolError("命令结果必须是已登记对象", "PLUGIN_SCHEMA_INVALID")
        validate_fields(definition.output_fields, result)
        self._check_open()
        self.session.check(lease)
        return result

    def invoke_callback(self, reference: str, arguments: dict[str, JsonValue], timeout: float) -> dict[str, JsonValue]:
        """
        宿主主动调用本运行登记的回调，超时或授权失效时明确失败。

        :param reference: 本运行登记的回调标识
        :param arguments: 回调参数
        :param timeout: 最长等待秒数
        :return: 回调应答
        """
        return self._invoke_callback({"callback_ref": reference, "args": arguments}, timeout)

    def exported_commands(self) -> list[dict[str, JsonValue]]:
        """
        返回本运行已导出命令的公开描述，供其他插件发现。

        :return: 仅含名称、说明与字段 schema 的列表
        :raises ProtocolError: 运行已关闭或授权失效时抛出
        """
        self.session.authorize("plugin.base")
        with self._lock:
            return [
                {
                    "command": definition.name,
                    "description": definition.description,
                    "input_fields": [item.model_dump(mode="json") for item in definition.input_fields],
                    "output_fields": [item.model_dump(mode="json") for item in definition.output_fields],
                }
                for _, definition, _ in self._commands.values()
                if definition.exported
            ]

    def _hook_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        只接受启动钩子登记；阶段与回调标识在登记入口校验。

        :param request: 已通过变更检查的请求
        :return: 登记结果
        :raises ProtocolError: 未获准、通道缺失或阶段无效时抛出
        """
        permission = next(
            (
                item
                for item in self.session.view().items
                if item.capability == "instances.manage" and item.decision == "allow" and item.resource_refs
            ),
            None,
        )
        if permission is None:
            raise ProtocolError("插件未获准管理游戏", "PLUGIN_PERMISSION_DENIED")
        self.session.authorize("instances.manage", tuple(permission.resource_refs))
        if request.method != "hooks.register":
            raise ProtocolError("此钩子方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")
        if self._hook_sink is None:
            raise ProtocolError("当前宿主未提供启动钩子", "PLUGIN_API_METHOD_NOT_FOUND")
        definition = LaunchHookDefinition.model_validate(request.params)
        self._hook_sink(definition.phase, definition.callback_ref, definition.required)
        return {"registered": True}

    def _cross_plugin_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        只开放已导出命令的发现与调用；未提供通道时明确失败。

        :param request: 已通过变更检查的请求
        :return: 发现结果或目标命令返回值
        :raises ProtocolError: 通道缺失、参数无效或目标拒绝时抛出
        """
        self.session.authorize("plugin.base")
        if self._cross_plugin is None:
            raise ProtocolError("当前宿主未提供跨插件通道", "PLUGIN_API_METHOD_NOT_FOUND")
        if request.method == "plugins.list":
            self._keys(request.params, set())
            return {"plugins": self._cross_plugin.list_commands()}
        if request.method == "plugins.call":
            self._keys(request.params, {"plugin", "command", "args"})
            target, command, args = request.params["plugin"], request.params["command"], request.params["args"]
            if not isinstance(target, str) or not isinstance(command, str) or not isinstance(args, dict):
                raise ProtocolError("跨插件调用参数无效", "PLUGIN_SCHEMA_INVALID")
            timeout = min(max(0.001, request.deadline - monotonic()), 30.0)
            return {"value": self._cross_plugin.call(target, command, args, timeout)}
        raise ProtocolError("此跨插件方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")

    def command_names(self) -> list[str]:
        """
        返回当前运行的命令名称供可信管理页使用。

        :return: 不包含回调实现的名称列表
        """
        with self._lock:
            return [item[1].name for item in self._commands.values()]

    def _panel_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        只分派面板注册、列举与数据拉取，面板动作仍走命令回调通道。
        """
        if request.method == "panels.register":
            return self._register_panels(request.params)
        if request.method == "panels.list":
            self._keys(request.params, set())
            return {"panels": self.panels_view()}
        if request.method == "panels.data":
            return self._panel_data(request)
        raise ProtocolError("此面板方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")

    def _register_panels(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        在授权接受点原子登记本运行面板，设置引用与当前 schema 一并核对。
        """
        self._keys(params, {"panels"})
        lease = self.session.authorize("plugin.base")
        raw = params["panels"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
            raise ProtocolError("面板注册数量无效", "PLUGIN_SCHEMA_INVALID")
        definitions = [PanelDefinition.model_validate(item) for item in raw]
        if len({item.key for item in definitions}) != len(definitions):
            raise ProtocolError("面板标识重复", "PLUGIN_SCHEMA_INVALID")
        if any(key not in self._definitions for item in definitions for key in _panel_setting_keys(item.nodes)):
            raise ProtocolError("面板引用了未定义的设置", "PLUGIN_SETTING_NOT_DEFINED")
        references = [uuid4().hex for _ in definitions]

        def register() -> None:
            with self._lock:
                self._ensure_accepting()
                if len(self._panels) + len(definitions) > 16:
                    raise ProtocolError("面板数量超过上限", "PLUGIN_RATE_LIMITED")
                registered = {item[0].key for item in self._panels.values()}
                if any(definition.key in registered for definition in definitions):
                    raise ProtocolError("面板已登记", "PLUGIN_REGISTRATION_CONFLICT")
                for reference, definition in zip(references, definitions, strict=True):
                    self._panels[reference] = (definition, lease)

        self.session.accept(lease, register)
        return {"panel_refs": references}

    def panels_view(self) -> list[dict[str, JsonValue]]:
        """
        返回本运行已登记面板定义，供可信管理页渲染。

        :return: 不含回调标识以外实现细节的面板定义
        :raises ProtocolError: 运行已关闭或授权失效时抛出
        """
        self.session.authorize("plugin.base")
        with self._lock:
            return [
                definition.model_dump(mode="json", exclude={"data_callback_ref"})
                for definition, _ in self._panels.values()
            ]

    def _panel_data(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        按请求拉取面板列表与任务快照，未声明数据回调时返回空结构。
        """
        self._keys(request.params, {"key"})
        lease = self.session.authorize("plugin.base")
        key = request.params["key"]
        if not isinstance(key, str):
            raise ProtocolError("面板标识无效", "PLUGIN_SCHEMA_INVALID")
        with self._lock:
            item = next(((item[0], item[1]) for item in self._panels.values() if item[0].key == key), None)
        if item is None:
            raise ProtocolError("面板不属于本运行", "PLUGIN_RESOURCE_NOT_FOUND")
        definition, panel_lease = item
        self.session.check(panel_lease)
        if definition.data_callback_ref is None:
            return {"lists": {}, "tasks": {}}
        timeout = min(max(0.001, request.deadline - monotonic()), 5.0)
        reply = self._invoke_callback({"callback_ref": definition.data_callback_ref, "args": {}}, timeout)
        if reply.get("ok") is not True or not isinstance(reply.get("data"), dict):
            raise ProtocolError("面板数据回调失败", "PLUGIN_COMMAND_FAILED")
        data = PanelData.model_validate(reply["data"].get("value"))
        self.session.check(lease)
        return data.model_dump(mode="json")

    def _ui_service(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        只接受已批准界面注入的插槽内容，实际渲染与失效由可信界面负责。
        """
        self.session.authorize("ui.inject")
        if self._slot_sink is None:
            raise ProtocolError("当前宿主未提供界面注入", "PLUGIN_API_METHOD_NOT_FOUND")
        if request.method == "ui.register_html":
            definition = HtmlSlotDefinition.model_validate(request.params)
            payload: dict[str, JsonValue] = {"kind": "html", **definition.model_dump(mode="json")}
        elif request.method == "ui.register_vue":
            definition = VueSlotDefinition.model_validate(request.params)
            payload = {"kind": "vue", **definition.model_dump(mode="json")}
        else:
            raise ProtocolError("此界面方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")
        self._slot_sink(payload)
        return {"registered": True}

    def _invoke_callback(self, params: dict[str, JsonValue], timeout: float) -> dict[str, JsonValue]:
        """
        回调超时后立即拒绝新宿主调用，由运行监管确认进程树回收。
        """
        try:
            return self._invoke("callbacks.invoke", params, timeout)
        except ProtocolError as exc:
            if exc.error_code in {"PLUGIN_DEADLINE_EXCEEDED", "PLUGIN_PIPE_CLOSED"}:
                self.error_code = (
                    "PLUGIN_CALLBACK_TIMEOUT" if exc.error_code == "PLUGIN_DEADLINE_EXCEEDED" else "PLUGIN_PIPE_CLOSED"
                )
                self._closed.set()
            raise

    def settings_view(self) -> dict[str, JsonValue]:
        """
        返回自身设置的 schema、值及 revision，供可信管理页编辑。

        :return: 当前设置视图
        """
        self.session.authorize("plugin.base")
        with self._settings_lock:
            values, revision = self._read_settings()
            return {
                "schema": [item.model_dump(mode="json") for item in self._definitions.values()],
                "values": self._values(values),
                "revision": revision,
            }

    def _settings(self, request: RpcRequest) -> dict[str, JsonValue]:
        method, params = request.method, request.params
        lease = self.session.authorize("plugin.base")
        with self._settings_lock:
            if method == "settings.define":
                self._keys(params, {"definitions"})
                raw = params["definitions"]
                if not isinstance(raw, list) or len(raw) > 64:
                    raise ProtocolError("设置定义超过上限", "PLUGIN_SCHEMA_INVALID")
                definitions = [SettingDefinition.model_validate(item) for item in raw]
                if len({item.key for item in definitions}) != len(definitions):
                    raise ProtocolError("设置键重复", "PLUGIN_SCHEMA_INVALID")

                def register() -> None:
                    self._ensure_accepting()
                    self._definitions = {item.key: item for item in definitions}

                self.session.accept(lease, register)
                return {"schema_revision": 1}
            if method == "settings.get":
                self._keys(params, {"keys"})
                keys = params["keys"]
                if (
                    not isinstance(keys, list)
                    or len(keys) > 64
                    or any(not isinstance(k, str) or k not in self._definitions for k in keys)
                ):
                    raise ProtocolError("设置未定义", "PLUGIN_SETTING_NOT_DEFINED")
                values, revision = self._read_settings()
                result = self._values(values)
                self.session.check(lease)
                return {"values": {key: result[key] for key in keys}, "revision": revision}
            self._keys(params, {"values", "expected_revision"})
            patch = params["values"]
            if not isinstance(patch, dict) or type(params["expected_revision"]) is not int:
                raise ProtocolError("设置提交无效", "PLUGIN_SCHEMA_INVALID")
            if any(
                key not in self._definitions or not self._definitions[key].accepts(value)
                for key, value in patch.items()
            ):
                raise ProtocolError("设置值不符合定义", "PLUGIN_SETTING_TYPE_MISMATCH")
            with _exclusive_lock(self._settings_path.with_suffix(".lock"), wait_seconds=2):
                values, revision = self._read_settings()
                if revision != params["expected_revision"]:
                    raise ProtocolError("设置已由其他操作修改", "PLUGIN_REVISION_CONFLICT")
                candidate = values | patch
                request.check_deadline()
                self.session.accept(lease, self._ensure_accepting)
                atomic_write_text(
                    self._settings_path,
                    json.dumps(
                        {"schema_version": 1, "revision": revision + 1, "values": candidate}, ensure_ascii=False
                    ),
                )
            self._publish_settings(patch, revision + 1)
            return {"revision": revision + 1}

    def _read_settings(self) -> tuple[dict[str, JsonValue], int]:
        """
        只读取本安装 v2 设置；损坏原文件保留并明确失败，不回退旧格式。
        """
        if not self._settings_path.exists():
            return {}, 0
        try:
            if self._settings_path.is_symlink() or self._settings_path.stat().st_size > 65536:
                raise ValueError("设置文件超限")
            value = _json_value(json.loads(self._settings_path.read_bytes()))
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", "revision", "values"}
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or type(value["revision"]) is not int
                or value["revision"] < 0
                or not isinstance(value["values"], dict)
            ):
                raise ValueError("设置文件格式无效")
            return value["values"], value["revision"]
        except (OSError, ValueError) as exc:
            raise ProtocolError("插件设置文件无法读取", "PLUGIN_STORAGE_FAILED") from exc

    def _values(self, stored: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, definition in self._definitions.items():
            value = stored.get(key, definition.default)
            if not definition.accepts(value):
                raise ProtocolError("持久化设置不符合当前定义", "PLUGIN_SETTING_TYPE_MISMATCH")
            result[key] = value
        return result

    def _publish_settings(self, values: dict[str, JsonValue], revision: int) -> None:
        with self._lock:
            self._sequence += 1
            payload: dict[str, JsonValue] = {"values": values, "revision": revision, "sequence": self._sequence}
            subscriptions = tuple(self._subscriptions.items())
        for reference, (_, lease) in subscriptions:
            try:
                self._events.put_nowait((reference, payload, lease))
            except Full:
                self.error_code = "PLUGIN_RATE_LIMITED"
                self._closed.set()
                get_logger("PluginHostBroker").warning("插件可靠事件队列已满，停止本运行继续接收请求")
                return  # 设置写入已经提交，不能把事件故障误报为写入回滚。

    def _deliver_events(self) -> None:
        while not self._closed.is_set():
            try:
                reference, payload, lease = self._events.get(timeout=0.1)
            except Empty:
                continue
            try:
                with self._lock:
                    subscription = self._subscriptions.get(reference)
                if subscription is None or subscription[1] != lease:
                    continue
                self.session.check(lease)
                reply = self._invoke_callback({"callback_ref": subscription[0], "args": payload}, 5)
                if reply.get("ok") is not True:
                    self.error_code = "PLUGIN_EVENT_DELIVERY_FAILED"
                    self._closed.set()
                    get_logger("PluginHostBroker").warning("插件事件回调失败，停止本运行继续接收请求")
            except (PluginGrantError, ProtocolError, OSError):
                get_logger("PluginHostBroker").debug("插件事件未交付，订阅或运行已经失效")

    def _ensure_accepting(self) -> None:
        if self._closed.is_set():
            raise ProtocolError("插件运行已停止接收请求", "PLUGIN_SESSION_EXPIRED")

    def _check_open(self) -> None:
        self._ensure_accepting()
        view = self.session.view()
        if view.error_code:
            raise ProtocolError("插件授权状态不可用", view.error_code)
        if view.revision == 0 or not view.required_satisfied:
            raise ProtocolError("插件尚未完成授权", "PLUGIN_REQUIRED_PERMISSION_DENIED")
        if view.grant_epoch != self._observed_epoch:
            self.refresh_permissions()

    @property
    def is_closed(self) -> bool:
        """
        供运行监管确认通信或可靠事件故障后的拒绝状态。

        :return: 本门面是否已拒绝新请求
        """
        return self._closed.is_set()

    @staticmethod
    def _keys(params: dict[str, JsonValue], expected: set[str]) -> None:
        if set(params) != expected:
            raise ProtocolError("插件请求字段无效", "PLUGIN_SCHEMA_INVALID")

    def close(self) -> None:
        """
        幂等拒绝新调用并清理本运行注册；不宣称已经停止外部 Worker。
        """
        self._closed.set()
        if self.modpacks is not None:
            self.modpacks.close()
        if self.game_catalog is not None:
            self.game_catalog.close()
        if self.game_install is not None:
            self.game_install.close()
        if self.game_launch is not None:
            self.game_launch.close()
        if self.java is not None:
            self.java.close()
        if self.network is not None:
            self.network.close()
        if self.files is not None:
            self.files.close()
        if self.operations is not None:
            self.operations.close()
        if self._resources is not None:
            self._resources.clear()
        with self._lock:
            self._commands.clear()
            self._subscriptions.clear()
        if current_thread() is not self._event_thread:
            self._event_thread.join(1)

    def refresh_pending_work(self) -> None:
        """
        监管现有任务、网络和暂存的原始授权及取消信号，并重试真实资源清理。
        """
        if self.operations is not None:
            self.operations.refresh_permissions()
        if self.network is not None:
            self.network.refresh_permissions()
        if self.files is not None:
            self.files.refresh_permissions()

    def refresh_permissions(self) -> None:
        """
        授权版本变化后只保留仍获准的注册，并为自身事件重新建立有效准入。
        """
        self.refresh_pending_work()
        view = self.session.view()
        if view.error_code or not view.required_satisfied:
            self.close()
            return
        event_lease = self.session.authorize("plugin.base")
        command_lease = self.session.authorize("plugin.base")
        with self._lock:
            self._observed_epoch = view.grant_epoch
            self._commands = (
                {key: (item[0], item[1], command_lease) for key, item in self._commands.items()}
                if command_lease
                else {}
            )
            self._subscriptions = (
                {key: (value[0], event_lease) for key, value in self._subscriptions.items()} if event_lease else {}
            )
