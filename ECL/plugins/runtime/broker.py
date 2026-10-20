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
from typing import ClassVar
from uuid import uuid4

from pydantic import ValidationError

from ECL.foundation.version import __version__
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.runtime.models import CommandDefinition, SettingDefinition, validate_fields
from ECL.utils import atomic_write_text, get_logger


@dataclass(slots=True)
class _Receipt:
    fingerprint: str
    grant_epoch: int
    ready: Event = field(default_factory=Event)
    result: dict[str, JsonValue] | None = None
    error_code: str | None = None


class PluginHostBroker:
    """
    只处理本连接的已实现能力；所有对象在运行结束或授权失效后不可继续使用。

    回调与磁盘操作不持有注册表锁。变更回执在本 run 内保留，容量耗尽时拒绝新提交。
    """

    implemented_capabilities: ClassVar[frozenset[str]] = frozenset(
        {"commands.register", "commands.execute", "settings.read", "settings.write", "events.subscribe"}
    )
    mutation_methods: ClassVar[frozenset[str]] = frozenset(
        {
            "commands.register",
            "commands.unregister",
            "settings.define",
            "settings.set",
            "events.subscribe",
            "events.unsubscribe",
            "commands.call",
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
    ) -> None:
        """
        绑定宿主持有的会话和回调通道，不接受客户端提供主体。

        :param session: 当前运行的唯一授权会话
        :param data_path: 宿主数据根目录
        :param invoke: 当前 Worker 的受控请求方法
        :param run_state: 唯一运行服务提供的当前状态
        """
        self.session: PluginGrantSession = session
        self.error_code: str | None = None
        self._invoke = invoke
        self._run_state = run_state
        self._settings_path = data_path / "plugin_settings_v2" / f"{session.view().installation_id}.json"
        self._lock = RLock()
        self._settings_lock = RLock()
        self._closed = Event()
        self._idle = Event()
        self._idle.set()
        self._active_requests = 0
        self._commands: dict[str, tuple[str, CommandDefinition, GrantLease]] = {}
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
                self._check_open()
                payload = _json_value(request.params)
                if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False).encode()) > 65536:
                    raise ProtocolError("插件业务载荷超限", "PLUGIN_MESSAGE_TOO_LARGE")
                if request.method not in self.mutation_methods:
                    return self._bounded_result(self._dispatch(request))
                return self._mutate(request, payload)
            except PluginGrantError as exc:
                raise ProtocolError("插件权限检查未通过", exc.error_code) from exc
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
        return self._idle.wait(timeout)

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
        with self._lock:
            receipt = self._receipts.get(request.mutation_key)
            is_new = receipt is None
            if receipt is None:
                if len(self._receipts) >= self.max_receipts:
                    raise ProtocolError("本运行变更回执已达上限", "PLUGIN_RATE_LIMITED")
                receipt = _Receipt(fingerprint, self.session.view().grant_epoch)
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
        self._check_open()
        if receipt.grant_epoch != self.session.view().grant_epoch:
            raise ProtocolError("回执授权版本已失效", "PLUGIN_PERMISSION_REVOKED")
        if receipt.error_code:
            raise ProtocolError("此前变更未完成", receipt.error_code)
        assert receipt.result is not None
        return receipt.result

    def _dispatch(self, request: RpcRequest) -> dict[str, JsonValue]:
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
            self._keys(params, set())
            view = self.session.view()
            return {
                "items": [dict(asdict(item), resource_refs=list(item.resource_refs)) for item in view.items],
                "grant_epoch": view.grant_epoch,
            }
        if method == "commands.register":
            return self._register_command(params)
        if method == "commands.unregister":
            self._keys(params, {"registration_ref"})
            with self._lock:
                reference = next(
                    (ref for ref, item in self._commands.items() if item[0] == params["registration_ref"]), None
                )
                return {"released": self._commands.pop(reference, None) is not None}
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
        if method == "events.unsubscribe":
            self._keys(params, {"subscription_ref"})
            with self._lock:
                reference = params["subscription_ref"]
                if not isinstance(reference, str):
                    raise ProtocolError("订阅标识无效", "PLUGIN_SCHEMA_INVALID")
                return {"released": self._subscriptions.pop(reference, None) is not None}
        raise ProtocolError("此 Host API 尚未接线", "PLUGIN_API_METHOD_NOT_FOUND")

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
        if receipt.grant_epoch != self.session.view().grant_epoch:
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
        复核注册授权并只把本运行回调登记到有界命令表。
        """
        lease = self.session.authorize("commands.register")
        definition = CommandDefinition.model_validate(params)
        if definition.exported:
            raise ProtocolError("跨插件命令尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")
        self.session.check(lease)
        with self._lock:
            self._check_open()
            if len(self._commands) >= 64 or any(item[1].name == definition.name for item in self._commands.values()):
                raise ProtocolError("命令已登记或超过上限", "PLUGIN_REGISTRATION_CONFLICT")
            reference = uuid4().hex
            self._commands[reference] = (uuid4().hex, definition, lease)
            return {"command_ref": reference, "registration_ref": self._commands[reference][0]}

    def _subscribe(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        只订阅本安装设置事件，拒绝完整宿主事件总线和未知回调。
        """
        self._keys(params, {"event", "callback_ref"})
        lease = self.session.authorize("events.subscribe")
        callback = params["callback_ref"]
        if params["event"] != "settings.changed" or not isinstance(callback, str) or len(callback) != 32:
            raise ProtocolError("该事件或回调尚未开放", "PLUGIN_EVENT_NOT_EXPOSED")
        self.session.check(lease)
        with self._lock:
            self._check_open()
            if len(self._subscriptions) >= 32:
                raise ProtocolError("订阅超过上限", "PLUGIN_RATE_LIMITED")
            reference = uuid4().hex
            self._subscriptions[reference] = (callback, lease)
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
        lease = self.session.authorize("commands.execute")
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

    def command_names(self) -> list[str]:
        """
        返回当前运行的命令名称供可信管理页使用。

        :return: 不包含回调实现的名称列表
        """
        with self._lock:
            return [item[1].name for item in self._commands.values()]

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
        self.session.authorize("settings.read")
        with self._settings_lock:
            values, revision = self._read_settings()
            return {
                "schema": [item.model_dump(mode="json") for item in self._definitions.values()],
                "values": self._values(values),
                "revision": revision,
            }

    def _settings(self, request: RpcRequest) -> dict[str, JsonValue]:
        method, params = request.method, request.params
        lease = self.session.authorize("settings.read" if method == "settings.get" else "settings.write")
        with self._settings_lock:
            if method == "settings.define":
                self._keys(params, {"definitions"})
                raw = params["definitions"]
                if not isinstance(raw, list) or len(raw) > 64:
                    raise ProtocolError("设置定义超过上限", "PLUGIN_SCHEMA_INVALID")
                definitions = [SettingDefinition.model_validate(item) for item in raw]
                if len({item.key for item in definitions}) != len(definitions):
                    raise ProtocolError("设置键重复", "PLUGIN_SCHEMA_INVALID")
                self.session.check(lease)
                self._definitions = {item.key: item for item in definitions}
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
                self.session.check(lease)  # 此次单目标原子写的提交接受点；后续撤销不承诺回滚。
                self._check_open()
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

    def _check_open(self) -> None:
        if self._closed.is_set():
            raise ProtocolError("插件运行已停止接收请求", "PLUGIN_SESSION_EXPIRED")
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
        with self._lock:
            self._commands.clear()
            self._subscriptions.clear()
        if current_thread() is not self._event_thread:
            self._event_thread.join(1)

    def refresh_permissions(self) -> None:
        """
        授权版本变化后只保留仍获准的注册，并为自身事件重新建立有效准入。
        """
        view = self.session.view()
        allowed = {item.capability for item in view.items if item.decision == "allow"}
        if view.error_code or not view.required_satisfied:
            self.close()
            return
        event_lease = self.session.authorize("events.subscribe") if "events.subscribe" in allowed else None
        command_lease = self.session.authorize("commands.register") if "commands.register" in allowed else None
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
