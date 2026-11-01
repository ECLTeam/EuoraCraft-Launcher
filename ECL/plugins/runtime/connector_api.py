# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：把 Worker 联机能力限定到自己创建或加入的房间，实际资源仍归联机服务。
#
# 公开接口：
#   - class RuntimeConnector — 原始许可、会话代次及有界私有协议适配。
# ============================================================

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Event, RLock
from time import sleep
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import Field, model_validator
from pydantic import JsonValue as PydanticJsonValue

from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.connector import ConnectorProtocolRequest, ConnectorProtocolResponse, ConnectorSessionContext
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.runtime.models import RequestModel
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.services.operations import OperationContext, OperationOrigin
from ECL.utils import ConnectorError

if TYPE_CHECKING:
    from ECL.services.connector import ConnectorService, PluginConnectorRoom


class _Open(RequestModel):
    port: int | None = Field(default=None, strict=True, ge=1, le=65535)
    code: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[^\x00-\x20]+$")

    @model_validator(mode="after")
    def _choice(self) -> _Open:
        if (self.port is None) == (self.code is None):
            raise ValueError("建房端口和加入房间码必须二选一")
        return self


class _RoomRequest(RequestModel):
    session_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class _Protocol(RequestModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    callback_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class _Send(_RoomRequest):
    protocol_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    payload: PydanticJsonValue


class _StatusText(_RoomRequest):
    text: str = Field(max_length=500)


@dataclass(slots=True)
class _Room:
    token: PluginConnectorRoom
    lease: GrantLease
    operation_ref: str = ""
    status_text: str = ""


@dataclass(frozen=True, slots=True)
class _ProtocolBinding:
    protocol: str
    callback: str
    lease: GrantLease


class RuntimeConnector:
    """
    房间任务保持活动至实际退出；取消、撤权和 Worker 退出由同一任务清理资源。

    不读取用户房间，不交付机器标识或底层网络对象。通知只入队，不在联机锁内等待 Worker。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        service: ConnectorService,
        operations: RuntimeOperations,
        plugin_name: str,
        invoke: Callable[[dict[str, JsonValue], float], dict[str, JsonValue]],
        publish: Callable[[str, dict[str, JsonValue]], None],
    ) -> None:
        """
        使用应用唯一联机和任务服务，不增加第二套网络生命周期。

        :param session: 原始运行授权
        :param service: 应用唯一联机服务
        :param operations: 同一运行任务接口
        :param plugin_name: 宿主解析的清单名称
        :param invoke: 有界逆向回调
        :param publish: 仅入队的宿主事件发布
        """
        self._session = session
        self._service = service
        self._operations = operations
        self._invoke = invoke
        self._publish = publish
        self._plugin_name = plugin_name
        self._origin = OperationOrigin(session.view().installation_id, session.binding.content_digest, session.run_id)
        self._owner = f"worker-connector-{session.run_id}"
        self._namespace = "ecl_" + hashlib.sha256(plugin_name.encode()).hexdigest()[:24]
        self._lock = RLock()
        self._rooms: dict[str, _Room] = {}
        self._protocols: dict[str, _ProtocolBinding] = {}
        self._closed = False

    def _check(self, lease: GrantLease) -> None:
        with self._lock:
            if self._closed:
                raise ProtocolError("联机运行已经结束", "PLUGIN_SESSION_EXPIRED")
        self._session.check(lease)

    def _room(self, reference: str) -> _Room:
        with self._lock:
            room = self._rooms.get(reference)
        if room is None:
            raise ProtocolError("未找到自身联机会话", "PLUGIN_RESOURCE_NOT_FOUND")
        self._check(room.lease)
        return room

    def _status(self, reference: str, room: _Room) -> dict[str, JsonValue]:
        status = self._service.plugin_room_status(room.token)
        self._check(room.lease)
        return {
            "session_ref": reference,
            "operation_ref": room.operation_ref,
            "mode": status.mode,
            "room_code": status.room_code,
            "local_port": status.local_port,
            "cleanup_pending": status.cleanup_pending,
        }

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        分派自身会话及私有协议，所有变更仍使用 Broker 的提交去重。

        :param method: 明确登记的联机方法
        :param params: 不可信 JSON 参数
        :param deadline: RPC 接纳截止，不代表房间执行结束
        :return: 最小会话、任务或协议引用
        :raises ProtocolError: 许可、代次、输入或方法无效时抛出
        """
        lease = self._session.authorize("connector.manage")
        self._check(lease)
        try:
            return self._dispatch(method, params, deadline, lease)
        except ConnectorError as exc:
            raise ProtocolError("联机会话操作未完成", getattr(exc, "error_code", "PLUGIN_CONNECTOR_FAILED")) from None

    def _dispatch(
        self, method: str, params: dict[str, JsonValue], deadline: float, lease: GrantLease
    ) -> dict[str, JsonValue]:
        if method == "connector.open":
            return self._open(_Open.model_validate(params), lease, deadline)
        if method == "connector.protocols.register":
            return self._register(_Protocol.model_validate(params), lease)
        if method == "connector.request":
            return self._send(_Send.model_validate(params))
        if method == "connector.status.set":
            query = _StatusText.model_validate(params)
            room = self._room(query.session_ref)
            self._service.plugin_room_status(room.token)
            with self._lock:
                room.status_text = query.text
            return {"updated": True}
        if method in {"connector.get", "connector.leave"}:
            query = _RoomRequest.model_validate(params)
            room = self._room(query.session_ref)
            if method == "connector.get":
                return self._status(query.session_ref, room)
            return self._operations.handle("operations.cancel", {"operation_ref": room.operation_ref})
        raise ProtocolError("联机方法尚未定义", "PLUGIN_API_METHOD_NOT_FOUND")

    def _open(self, query: _Open, lease: GrantLease, deadline: float) -> dict[str, JsonValue]:
        """
        保留代次后提交长任务；失败或取消只清理原代次，成功建房仍不结束房间任务。
        """
        with self._lock:
            if len(self._rooms) >= 32:
                raise ProtocolError("会话引用已达上限", "PLUGIN_RATE_LIMITED")
            self._check(lease)
        token = self._service.reserve_plugin_room(self._origin)
        reference = uuid4().hex
        room = _Room(token, lease)
        admitted = Event()
        with self._lock:
            self._rooms[reference] = room

        def execute(context: OperationContext) -> object:
            while not admitted.wait(0.05):
                context.check_cancelled()
            context.check_cancelled()
            context.report_effect("staged")
            self._service.start_plugin_room(token, port=query.port, code=query.code)
            context.check_cancelled()
            self._publish("connector.changed", {"session_ref": reference, "event": "ready"})
            context.progress(100, "联机会话运行中")
            while self._service.owns_plugin_room(token):
                context.check_cancelled()
                sleep(0.1)
            return None

        def cleanup() -> bool:
            result = self._service.leave_plugin_room(token)
            self._publish("connector.changed", {"session_ref": reference, "event": "closed"})
            return result

        try:
            room.operation_ref = self._operations.submit(
                "plugin_connector_room",
                execute,
                lease,
                cleanup=cleanup,
                initial_effect="staged",
                submission_deadline=deadline,
            )
            admitted.set()
        except Exception:
            # 提交前即已持有代次，失败必须交回；实际清理失败仍保留服务句柄供用户重试。
            self._service.leave_plugin_room(token)
            raise
        return {"session_ref": reference, "operation_ref": room.operation_ref, "state": "queued"}

    @staticmethod
    def _encode(value: JsonValue) -> bytes:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > 16384:
            raise ProtocolError("联机载荷超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        return encoded

    def _register(self, query: _Protocol, lease: GrantLease) -> dict[str, JsonValue]:
        """
        固定清单名称生成跨设备一致命名空间，仅为本来源会话协商协议。
        """
        with self._lock:
            self._check(lease)
            if len(self._protocols) >= 8:
                raise ProtocolError("联机协议已达上限", "PLUGIN_RATE_LIMITED")
            name = f"{self._namespace}:{query.name}"
            if any(item.protocol == name for item in self._protocols.values()):
                raise ProtocolError("联机协议已经登记", "PLUGIN_REGISTRATION_CONFLICT")
            reference = uuid4().hex
            self._protocols[reference] = _ProtocolBinding(name, query.callback_ref, lease)
        self._register_extension()
        return {"protocol_ref": reference, "protocol": name}

    def _register_extension(self) -> None:
        """
        状态补充读取本地缓存，所有通知仅排队；不在房间锁内执行远程回调。
        """
        with self._lock:
            bindings = tuple(self._protocols.values())
            if self._closed or not bindings:
                return
            self._session.accept_all(
                tuple(item.lease for item in bindings),
                lambda: self._service.extensions.register(
                    owner=self._owner,
                    name=self._namespace,
                    protocols={item.protocol: self._handler(item) for item in bindings},
                    on_guest_joined=lambda _: self._notify("joined"),
                    before_leave=lambda _: self._notify("leaving"),
                    on_reset=lambda _: self._notify("reset"),
                    enrich_status=self._enrich,
                    applies_to_session=lambda: self._service.is_plugin_room_origin(self._origin),
                ),
            )

    def _handler(
        self, binding: _ProtocolBinding
    ) -> Callable[[ConnectorProtocolRequest], Awaitable[ConnectorProtocolResponse]]:
        async def handle(request: ConnectorProtocolRequest) -> ConnectorProtocolResponse:
            try:
                return await asyncio.to_thread(self._receive, binding, request.body)
            except (ProtocolError, ValueError, UnicodeError):
                return ConnectorProtocolResponse(status=255, body=b"PLUGIN_CONNECTOR_REQUEST_REJECTED")

        return handle

    def _receive(self, binding: _ProtocolBinding, body: bytes) -> ConnectorProtocolResponse:
        """
        只交付有界 JSON，不转发请求方机器标识和游戏信息；回调前后复核原授权。
        """
        self._check(binding.lease)
        with self._lock:
            room = next((item for item in self._rooms.values() if self._service.owns_plugin_room(item.token)), None)
        if room is None or len(body) > 16384:
            raise ProtocolError("协议不属于当前会话", "PLUGIN_RESOURCE_DENIED")
        if self._service.plugin_room_status(room.token).cleanup_pending:
            raise ProtocolError("协议会话正在退出", "PLUGIN_RESOURCE_DENIED")
        value = _json_value(json.loads(body))
        reply = self._invoke({"callback_ref": binding.callback, "args": {"payload": value}}, 5)
        self._check(binding.lease)
        if (
            not self._service.owns_plugin_room(room.token)
            or self._service.plugin_room_status(room.token).cleanup_pending
        ):
            raise ProtocolError("协议会话已经结束", "PLUGIN_RESOURCE_DENIED")
        if reply.get("ok") is not True or not isinstance(reply.get("data"), dict):
            raise ProtocolError("协议回调失败", "PLUGIN_CONNECTOR_FAILED")
        return ConnectorProtocolResponse(body=self._encode(_json_value(reply["data"].get("value"))))

    def _send(self, query: _Send) -> dict[str, JsonValue]:
        room = self._room(query.session_ref)
        with self._lock:
            binding = self._protocols.get(query.protocol_ref)
        if binding is None:
            raise ProtocolError("未找到自身协议引用", "PLUGIN_RESOURCE_NOT_FOUND")
        self._check(binding.lease)
        status, body = self._service.request_plugin_room(room.token, binding.protocol, self._encode(query.payload))
        self._check(binding.lease)
        self._check(room.lease)
        if status != 0:
            raise ProtocolError("房主未接受协议请求", "PLUGIN_CONNECTOR_FAILED")
        if len(body) > 16384:
            raise ProtocolError("协议响应超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        try:
            value = _json_value(json.loads(body))
        except (ValueError, UnicodeError):
            raise ProtocolError("房主协议响应无效", "PLUGIN_SCHEMA_INVALID") from None
        return {"value": value}

    def _notify(self, event: str) -> None:
        with self._lock:
            if self._closed:
                return
        self._publish("connector.changed", {"event": event})

    def _enrich(self, context: ConnectorSessionContext, status: dict[str, object]) -> dict[str, JsonValue]:
        with self._lock:
            text = next(
                (room.status_text for room in self._rooms.values() if self._service.owns_plugin_room(room.token)), ""
            )
        return {"pluginExtension": {"name": self._plugin_name, "text": text}} if text else {}

    def invalidate(self) -> None:
        """
        撤销旧协议及状态，不替换房间原始许可；任务服务负责实际取消和清理。
        """
        with self._lock:
            self._protocols.clear()
            for room in self._rooms.values():
                room.status_text = ""
        self._service.extensions.unregister_owner(self._owner)

    def close(self) -> None:
        """
        拒绝新调用并撤销协议；Broker 随后取消本运行的房间任务并等待实际退出。
        """
        with self._lock:
            self._closed = True
        self.invalidate()
