# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证第三方登录与联机扩展的独立准入与返回值净化。
#
# 公开接口：
#   - test_auth_registration_requires_own_capability(request) -> None
#   - test_connector_registration_requires_own_capability(request) -> None
#   - test_account_payload_is_validated() -> None
#   - test_credentials_payload_is_validated() -> None
#   - test_status_fields_are_bounded() -> None
# ============================================================

from __future__ import annotations

from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.auth_providers import sanitize_account_payload, sanitize_credentials_payload
from ECL.plugins.connector import limit_status_fields, limit_status_text, sanitize_status_fields
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture


def send(broker, method: str, params: dict) -> dict:
    return broker.handle(RpcRequest(method, params, monotonic() + 5, uuid4().hex))


def test_auth_registration_requires_own_capability(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        send(
            broker,
            "auth.register",
            {
                "provider_id": "demo",
                "title": "示例",
                "fields": [{"key": "token", "label": "令牌"}],
                "authenticate_ref": "a" * 32,
                "resolve_ref": "b" * 32,
            },
        )
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"


def test_connector_registration_requires_own_capability(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        send(broker, "connector.register_status", {"callback_ref": "a" * 32})
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"


def test_account_payload_is_validated() -> None:
    account = sanitize_account_payload(
        {"id": "1", "alias": "玩家", "uuid": "0" * 32, "data": {"token": "t"}, "skinUrl": "https://x"}
    )
    assert account["alias"] == "玩家"
    assert account["data"] == {"token": "t"}
    with pytest.raises(ValueError):
        sanitize_account_payload({"id": "1", "alias": "玩家"})
    with pytest.raises(ValueError):
        sanitize_account_payload({"id": "1", "alias": "x" * 300, "uuid": "u"})


def test_credentials_payload_is_validated() -> None:
    credentials = sanitize_credentials_payload(
        {"player_name": "玩家", "uuid": "0" * 32, "user_type": "yggdrasil", "access_token": "t"}
    )
    assert credentials["user_type"] == "yggdrasil"
    with pytest.raises(ValueError):
        sanitize_credentials_payload(
            {"player_name": "玩家", "uuid": "0" * 32, "user_type": "unknown", "access_token": "t"}
        )


def test_status_fields_are_bounded() -> None:
    fields = sanitize_status_fields(
        {"fields": {f"k{index}": "v" * (limit_status_text + 10) for index in range(limit_status_fields + 3)}}
    )
    assert len(fields) == limit_status_fields
    assert len(next(iter(fields.values()))) == limit_status_text
    assert sanitize_status_fields({"fields": {"a": 1}}) == {}
    assert sanitize_status_fields("text") == {}
