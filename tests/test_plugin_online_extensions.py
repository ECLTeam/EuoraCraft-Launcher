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
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.auth_providers import sanitize_account_payload, sanitize_credentials_payload
from ECL.plugins.connector import limit_status_fields, limit_status_text, sanitize_status_fields
from ECL.plugins.runtime.models import AuthProviderDefinition
from ECL.plugins.worker_service import PluginWorkerService
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


def test_auth_private_data_is_not_silently_truncated() -> None:
    token = "t" * 4096
    account = sanitize_account_payload({"id": "1", "alias": "Player", "uuid": "0" * 32, "data": {"token": token}})
    assert account["data"] == {"token": token}
    credentials = sanitize_credentials_payload(
        {"player_name": "Player", "uuid": "0" * 32, "user_type": "msa", "access_token": token}
    )
    assert credentials["access_token"] == token


@pytest.mark.parametrize("data", [{"token": "t" * 16385}, {"token": 1}, {"": "token"}, {"x" * 65: "token"}])
def test_invalid_private_data_rejects_whole_account(data) -> None:
    with pytest.raises(ValueError):
        sanitize_account_payload({"id": "1", "alias": "Player", "uuid": "0" * 32, "data": data})


def test_auth_registration_is_scoped_to_installation(tmp_path, monkeypatch) -> None:
    registrations = []
    service = PluginWorkerService(tmp_path, auth_sink=lambda *args: registrations.append(args))
    installation_id = "first-installation"
    monkeypatch.setattr(
        service,
        "_entry",
        lambda _name: SimpleNamespace(
            installation_id=installation_id, session=SimpleNamespace(authorize=lambda _: None)
        ),
    )
    definition = AuthProviderDefinition(
        provider_id="demo",
        title="Demo",
        fields=[{"key": "token", "label": "Token"}],
        authenticate_ref="a" * 32,
        resolve_ref="b" * 32,
    )
    service._publish_auth("first", "auth", definition)
    installation_id = "second-installation"
    service._publish_auth("second", "auth", definition)
    service._publish_auth("first", "auth", definition)
    assert registrations[0][1] != registrations[1][1]
    assert registrations[0][1] != registrations[2][1]
    assert all(item[1] != "demo" for item in registrations)
    service._publish_auth("first", "auth", None)
    assert registrations[-1] == ("first", None, None, None)
