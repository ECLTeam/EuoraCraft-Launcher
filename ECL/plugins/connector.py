# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件可注册的 Scaffolding 联机扩展协议。
#
# 公开接口：
#   - class ConnectorProtocolResponse — 一个 Scaffolding 扩展协议响应。
#       - json(value, status) -> ConnectorProtocolResponse — 把 JSON 值编码为 UTF-8 协议响应。
#   - class ConnectorProtocolRequest — 保存房主收到的扩展协议请求，供插件只读访问。
#       - json(default=…) -> Any — 将请求体解析为 JSON；空请求体返回 ``default``。
#       - remove_player(machine_id) -> None — 从房主玩家列表中移除指定机器，供优雅退出协议使用。
#   - class ConnectorSessionContext — 向插件提供当前房间信息，以及有限的请求发送和头像读取方法。
#       - request(protocol, body=…) -> tuple[int, bytes] — 向当前房主发送原始扩展协议请求。
#       - request_json(protocol, payload=…) -> Any — 发送 JSON 扩展请求并解析成功响应。
#       - local_player_icon() -> str | None — 返回本机玩家完整皮肤的 base64（PNG），供联机头像交换使用。
#   - class ConnectorExtensionRegistry — 保存插件的联机扩展；单个扩展出错时，其他扩展仍可运行。
#       - register(owner, name, protocols, on_guest_joined, enrich_status, before_leave, on_reset) -> None — 注册或原位更新一个插件拥有的联机扩展。
#       - unregister_owner(owner) -> None — 移除指定插件拥有的全部联机扩展。
#       - protocol_names() -> list[str] — 返回当前需要参与 Scaffolding 协商的扩展协议名。
#       - dispatch(request) -> tuple[int, bytes] — 调用单个协议处理器并规范化响应。
#       - guest_joined(context) -> None — 通知扩展房客已完成基础协议握手。
#       - enrich_status(context, status) -> dict[str, Any] — 依次让扩展补充当前联机状态。
#       - before_leave(context) -> None — 在连接关闭前通知扩展。
#       - reset(context) -> None — 在会话状态清空后通知扩展释放缓存。
# ============================================================

"""插件可注册的 Scaffolding 联机扩展协议。"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from ECL.utils import get_logger


class ConnectorProtocolPolicy:
    """
    保存联机扩展协议的校验规则与哨兵值。
    """

    extension_pattern = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
    protocol_pattern = re.compile(r"^[a-z0-9_]+:[a-z0-9_]+$")
    missing = object()


@dataclass(frozen=True, slots=True)
class ConnectorProtocolResponse:
    """
    一个 Scaffolding 扩展协议响应。
    """

    status: int = 0
    body: bytes = b""

    @classmethod
    def json(cls, value: Any, *, status: int = 0) -> ConnectorProtocolResponse:
        """
        把 JSON 值编码为 UTF-8 协议响应。
        """
        return cls(status=status, body=_encode_json(value))


@dataclass(frozen=True, slots=True)
class ConnectorProtocolRequest:
    """
    保存房主收到的扩展协议请求，供插件只读访问。
    """

    protocol: str
    body: bytes
    peer_machine_id: str | None = None
    game_info: Mapping[str, Any] | None = None
    _remove_player: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)

    def json(self, default: Any = None) -> Any:
        """
        将请求体解析为 JSON；空请求体返回 ``default``。
        """
        if not self.body:
            return default
        return json.loads(self.body.decode("utf-8"))

    async def remove_player(self, machine_id: str) -> None:
        """
        从房主玩家列表中移除指定机器，供优雅退出协议使用。
        """
        if self._remove_player is not None:
            await self._remove_player(machine_id)


@dataclass(frozen=True, slots=True)
class ConnectorSessionContext:
    """
    向插件提供当前房间信息，以及有限的请求发送和头像读取方法。
    """

    mode: str
    room_code: str | None
    machine_id: str | None
    game_info: Mapping[str, Any] | None
    _request: Callable[[str, bytes], tuple[int, bytes]] | None = field(default=None, repr=False)
    _local_icon_provider: Callable[[], str | None] | None = field(default=None, repr=False)

    def request(self, protocol: str, body: bytes = b"") -> tuple[int, bytes]:
        """
        向当前房主发送原始扩展协议请求。
        """
        if self._request is None:
            raise RuntimeError("当前联机会话不支持发送扩展协议")
        return self._request(protocol, body)

    def request_json(self, protocol: str, payload: Any = ConnectorProtocolPolicy.missing) -> Any:
        """
        发送 JSON 扩展请求并解析成功响应。
        """
        body = b"" if payload is ConnectorProtocolPolicy.missing else _encode_json(payload)
        status, response_body = self.request(protocol, body)
        if status != 0:
            detail = response_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"扩展协议 {protocol} 返回状态 {status}: {detail}")
        if not response_body:
            return None
        return json.loads(response_body.decode("utf-8"))

    def local_player_icon(self) -> str | None:
        """
        返回本机玩家完整皮肤的 base64（PNG），供联机头像交换使用。

        离线默认皮肤返回本地 data URL 的载荷；在线账户返回一次性下载并缓存的
        皮肤字节。解析失败或无皮肤时返回 None。
        """
        if self._local_icon_provider is None:
            return None
        try:
            value = self._local_icon_provider()
        except Exception:
            return None
        return value or None


ConnectorProtocolResult = ConnectorProtocolResponse | tuple[int, bytes] | bytes | str | Any
ConnectorProtocolHandler = Callable[
    [ConnectorProtocolRequest], ConnectorProtocolResult | Awaitable[ConnectorProtocolResult]
]
ConnectorSessionHook = Callable[[ConnectorSessionContext], Any]
ConnectorStatusEnricher = Callable[[ConnectorSessionContext, dict[str, Any]], Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class _RegisteredExtension:
    owner: str
    name: str
    protocols: Mapping[str, ConnectorProtocolHandler]
    on_guest_joined: ConnectorSessionHook | None
    enrich_status: ConnectorStatusEnricher | None
    before_leave: ConnectorSessionHook | None
    on_reset: ConnectorSessionHook | None


class ConnectorExtensionRegistry:
    """
    保存插件的联机扩展；单个扩展出错时，其他扩展仍可运行。
    """

    def __init__(self) -> None:
        self._extensions: dict[str, _RegisteredExtension] = {}
        self._protocol_owners: dict[str, str] = {}
        self._lock = RLock()
        self._logger = get_logger("ConnectorExtensionRegistry")

    def register(
        self,
        *,
        owner: str,
        name: str,
        protocols: Mapping[str, ConnectorProtocolHandler],
        on_guest_joined: ConnectorSessionHook | None = None,
        enrich_status: ConnectorStatusEnricher | None = None,
        before_leave: ConnectorSessionHook | None = None,
        on_reset: ConnectorSessionHook | None = None,
    ) -> None:
        """
        注册或原位更新一个插件拥有的联机扩展。
        """
        normalized_name = str(name).strip().casefold()
        if not ConnectorProtocolPolicy.extension_pattern.fullmatch(normalized_name):
            raise ValueError(f"联机扩展标识无效: {name}")
        normalized_protocols: dict[str, ConnectorProtocolHandler] = {}
        for protocol, handler in protocols.items():
            normalized_protocol = str(protocol).strip().casefold()
            if len(
                normalized_protocol.encode("ascii", errors="ignore")
            ) > 255 or not ConnectorProtocolPolicy.protocol_pattern.fullmatch(normalized_protocol):
                raise ValueError(f"联机扩展协议名无效: {protocol}")
            if not callable(handler):
                raise TypeError(f"联机扩展协议处理器必须可调用: {protocol}")
            normalized_protocols[normalized_protocol] = handler

        extension = _RegisteredExtension(
            owner=owner,
            name=normalized_name,
            protocols=normalized_protocols,
            on_guest_joined=on_guest_joined,
            enrich_status=enrich_status,
            before_leave=before_leave,
            on_reset=on_reset,
        )
        with self._lock:
            current = self._extensions.get(normalized_name)
            if current is not None and current.owner != owner:
                raise ValueError(f"联机扩展已由插件 {current.owner} 注册: {normalized_name}")
            for protocol in normalized_protocols:
                protocol_owner = self._protocol_owners.get(protocol)
                if protocol_owner is not None and protocol_owner != normalized_name:
                    raise ValueError(f"联机扩展协议已由 {protocol_owner} 注册: {protocol}")
            if current is not None:
                for protocol in current.protocols:
                    self._protocol_owners.pop(protocol, None)
            self._extensions[normalized_name] = extension
            for protocol in normalized_protocols:
                self._protocol_owners[protocol] = normalized_name

    def unregister_owner(self, owner: str) -> None:
        """
        移除指定插件拥有的全部联机扩展。
        """
        with self._lock:
            names = [name for name, extension in self._extensions.items() if extension.owner == owner]
            for name in names:
                extension = self._extensions.pop(name)
                for protocol in extension.protocols:
                    self._protocol_owners.pop(protocol, None)

    def protocol_names(self) -> list[str]:
        """
        返回当前需要参与 Scaffolding 协商的扩展协议名。
        """
        with self._lock:
            return sorted(self._protocol_owners)

    async def dispatch(self, request: ConnectorProtocolRequest) -> tuple[int, bytes]:
        """
        调用单个协议处理器并规范化响应。
        """
        with self._lock:
            extension_name = self._protocol_owners.get(request.protocol)
            extension = self._extensions.get(extension_name or "")
            handler = extension.protocols.get(request.protocol) if extension is not None else None
        if handler is None:
            return 255, f"Unsupported protocol: {request.protocol}".encode()
        try:
            result = handler(request)
            if inspect.isawaitable(result):
                result = await result
            return _normalize_response(result)
        except Exception as exc:
            self._logger.exception("插件联机扩展协议执行失败：协议：%s", request.protocol)
            return 255, str(exc).encode("utf-8", errors="replace")[:4096]

    def guest_joined(self, context: ConnectorSessionContext) -> None:
        """
        通知扩展房客已完成基础协议握手。
        """
        self._call_hooks("on_guest_joined", context)

    def enrich_status(self, context: ConnectorSessionContext, status: dict[str, Any]) -> dict[str, Any]:
        """
        依次让扩展补充当前联机状态。
        """
        with self._lock:
            extensions = tuple(self._extensions.values())
        for extension in extensions:
            if extension.enrich_status is None:
                continue
            try:
                patch = extension.enrich_status(context, status)
                if patch:
                    status.update(patch)
            except Exception:
                self._logger.debug("插件联机状态扩展失败：扩展：%s", extension.name, exc_info=True)
        return status

    def before_leave(self, context: ConnectorSessionContext) -> None:
        """
        在连接关闭前通知扩展。
        """
        self._call_hooks("before_leave", context)

    def reset(self, context: ConnectorSessionContext) -> None:
        """
        在会话状态清空后通知扩展释放缓存。
        """
        self._call_hooks("on_reset", context)

    def _call_hooks(self, hook_name: str, context: ConnectorSessionContext) -> None:
        with self._lock:
            extensions = tuple(self._extensions.values())
        for extension in extensions:
            hook = getattr(extension, hook_name)
            if hook is None:
                continue
            try:
                hook(context)
            except Exception:
                self._logger.debug(
                    "插件联机会话钩子失败：扩展：%s；钩子：%s",
                    extension.name,
                    hook_name,
                    exc_info=True,
                )


def _encode_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _normalize_response(result: Any) -> tuple[int, bytes]:
    if isinstance(result, ConnectorProtocolResponse):
        return result.status, result.body
    if isinstance(result, tuple) and len(result) == 2:
        status, body = result
        if not isinstance(status, int) or not isinstance(body, bytes):
            raise TypeError("联机协议元组响应必须为 tuple[int, bytes]")
        return status, body
    if isinstance(result, bytes):
        return 0, result
    if isinstance(result, str):
        return 0, result.encode("utf-8")
    if result is None:
        return 0, b""
    return 0, _encode_json(result)


__all__ = [
    "ConnectorExtensionRegistry",
    "ConnectorProtocolHandler",
    "ConnectorProtocolRequest",
    "ConnectorProtocolResponse",
    "ConnectorSessionContext",
]
