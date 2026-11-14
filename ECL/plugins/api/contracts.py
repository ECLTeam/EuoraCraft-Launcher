# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验插件与宿主之间的有界 JSON 信封，不依赖宿主或第三方库。
#
# 公开接口：
#   - JsonValue — 可跨进程传输的 JSON 值类型。
#   - class ProtocolError — 协议校验或版本协商失败。
#   - class ProtocolPolicy — 固定版本、消息大小及结构限制。
#   - class WireMessage — 校验后的协议消息及编码接口。
# ============================================================

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import ClassVar, TypeAlias

JsonValue: TypeAlias = str | int | float | bool | list["JsonValue"] | dict[str, "JsonValue"] | None


class ProtocolError(ValueError):
    """
    表示协议输入不合法或双方版本不兼容。

    错误文本仅描述规则，不拼接请求体、路径或凭据。
    """

    def __init__(self, message: str, error_code: str = "PLUGIN_PROTOCOL_ERROR") -> None:
        """
        保存适合返回调用方的稳定错误码。

        :param message: 不包含输入值的规则说明
        :param error_code: 协议定义的失败类别
        """
        super().__init__(message)
        self.error_code: str = error_code


class ProtocolPolicy:
    """
    定义 API v2 引导和 RPC 使用的不可变协议限制。
    """

    api_version: ClassVar[int] = 2
    protocol_version: ClassVar[int] = 1
    max_frame_bytes: ClassVar[int] = 1024 * 1024
    max_depth: ClassVar[int] = 16
    max_identifier_length: ClassVar[int] = 64
    max_timeout_ms: ClassVar[int] = 120000
    kinds: ClassVar[frozenset[str]] = frozenset(
        {"hello", "load_authorized", "loaded", "request", "response", "shutdown", "stopped", "error"}
    )
    envelope_fields: ClassVar[frozenset[str]] = frozenset(
        {"protocol_version", "kind", "request_id", "method", "params", "timeout_ms", "mutation_key"}
    )


def _json_value(value: object, depth: int = 0) -> JsonValue:
    """
    将 JSON 边界的未知值转换为有限类型，拒绝非有限数和过深结构。
    """
    if depth > ProtocolPolicy.max_depth:
        raise ProtocolError("消息嵌套层数超过上限")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProtocolError("消息包含无效的 Unicode 字符") from exc
        return value
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ProtocolError("整数超出可精确传输范围")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError("消息不能包含非有限数")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key in value:
            _json_value(key, depth + 1)
        return {key: _json_value(item, depth + 1) for key, item in value.items()}
    raise ProtocolError("消息包含不可序列化的值")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("JSON 对象包含重复字段")
        result[key] = value
    return result


def _identifier(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProtocolError("协议标识必须是字符串")
    if not value and allow_empty:
        return value
    if not value or len(value) > ProtocolPolicy.max_identifier_length or any(ord(char) < 32 for char in value):
        raise ProtocolError("协议标识格式无效")
    return value


@dataclass(frozen=True, slots=True)
class WireMessage:
    """
    保存已校验的信封，不让客户端字段决定调用主体。

    params 属于 JSON 边界，具体业务方法还须转换为其请求模型。
    """

    kind: str
    params: dict[str, JsonValue]
    request_id: str = ""
    method: str = ""
    timeout_ms: int = 5000
    mutation_key: str = ""

    def encode(self) -> bytes:
        """
        编码并复核消息大小及字段，拒绝任意 Python 对象。

        :return: UTF-8 JSON 字节
        :raises ProtocolError: 类型、结构或大小不满足协议时抛出
        """
        payload = {
            "protocol_version": ProtocolPolicy.protocol_version,
            "kind": self.kind,
            "params": self.params,
            "request_id": self.request_id,
            "method": self.method,
            "timeout_ms": self.timeout_ms,
            "mutation_key": self.mutation_key,
        }
        checked = _json_value(payload)
        raw = json.dumps(checked, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.decode(raw)
        return raw

    @classmethod
    def decode(cls, raw: bytes) -> WireMessage:
        """
        校验消息信封并拒绝重复字段、未知字段及不兼容版本。

        :param raw: 未受信的 UTF-8 消息，不接受 Pickle
        :return: 类型明确的消息
        :raises ProtocolError: 消息无法安全解析时抛出
        """
        if len(raw) > ProtocolPolicy.max_frame_bytes:
            raise ProtocolError("消息大小超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        try:
            decoded: object = json.loads(raw, object_pairs_hook=_unique_object)
            payload = _json_value(decoded)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ProtocolError("消息不是合法的 UTF-8 JSON") from exc
        if not isinstance(payload, dict) or set(payload) - ProtocolPolicy.envelope_fields:
            raise ProtocolError("消息信封包含未知字段")
        version = payload.get("protocol_version")
        if type(version) is not int or version != ProtocolPolicy.protocol_version:
            raise ProtocolError("协议版本不兼容", "PLUGIN_PROTOCOL_VERSION_UNSUPPORTED")
        kind = payload.get("kind")
        params = payload.get("params")
        if not isinstance(kind, str) or kind not in ProtocolPolicy.kinds or not isinstance(params, dict):
            raise ProtocolError("消息类型或参数结构无效")
        timeout_ms = payload.get("timeout_ms", 5000)
        if type(timeout_ms) is not int or not 0 < timeout_ms <= ProtocolPolicy.max_timeout_ms:
            raise ProtocolError("消息超时范围无效")
        request_id = _identifier(payload.get("request_id", ""), allow_empty=kind != "request")
        method = _identifier(payload.get("method", ""), allow_empty=kind != "request")
        mutation_key = _identifier(payload.get("mutation_key", ""), allow_empty=True)
        return cls(kind, params, request_id, method, timeout_ms, mutation_key)
