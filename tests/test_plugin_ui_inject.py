# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证界面注入权限门禁、插槽内容边界与可信接收方转发。
#
# 公开接口：
#   - test_html_slot_requires_permission(tmp_path) -> None
#   - test_html_slot_forwards_valid_content(tmp_path) -> None
#   - test_vue_slot_rejects_oversized_template(tmp_path) -> None
#   - test_slot_registration_requires_host_sink(tmp_path) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.runtime.broker import PluginHostBroker


def _broker(tmp_path: Path, *, allow: bool, with_sink: bool = True):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("value = 1", encoding="utf-8")
    declarations = (PermissionDeclaration(capability="ui.inject"),)
    binding = PluginContentBinding.capture(root, declarations, "windows-x86_64-cp311")
    store = PluginGrantStore(tmp_path, implemented_capabilities=PluginHostBroker.implemented_capabilities)
    session = store.open_session(store.new_installation_id(), binding)
    store.update(
        session,
        (PermissionDecision(capability="ui.inject", decision="allow" if allow else "deny"),),
        expected_revision=0,
        content_digest=binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    received: list[dict] = []
    broker = PluginHostBroker(
        session,
        tmp_path,
        lambda _method, _params, _timeout: {"ok": True, "data": {"value": {}}},
        run_state=lambda: "enabled",
        slot_sink=received.append if with_sink else None,
    )
    return broker, session, received


def _request(broker: PluginHostBroker, method: str, params: dict) -> dict:
    return broker.handle(RpcRequest(method, params, monotonic() + 5, uuid4().hex))


def test_html_slot_requires_permission(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path, allow=False)
    try:
        with pytest.raises(ProtocolError) as error:
            _request(
                broker,
                "ui.register_html",
                {"slot_id": "plugin-slot-content-bottom", "html": "<p>hi</p>"},
            )
        assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"
        assert received == []
    finally:
        broker.close()
        session.close()


def test_html_slot_forwards_valid_content(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path, allow=True)
    try:
        result = _request(
            broker,
            "ui.register_html",
            {"slot_id": "plugin-slot-content-bottom", "html": "<p>hi</p>", "key": "demo"},
        )
        assert result == {"registered": True}
        assert received == [
            {
                "kind": "html",
                "slot_id": "plugin-slot-content-bottom",
                "html": "<p>hi</p>",
                "key": "demo",
                "context_key": None,
            }
        ]
    finally:
        broker.close()
        session.close()


def test_vue_slot_rejects_oversized_template(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path, allow=True)
    try:
        with pytest.raises(ProtocolError) as error:
            _request(
                broker,
                "ui.register_vue",
                {
                    "slot_id": "plugin-slot-content-bottom",
                    "component_name": "DemoCard",
                    "template": "x" * 60001,
                },
            )
        assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
        assert received == []
    finally:
        broker.close()
        session.close()


def test_master_switch_blocks_slot_and_allows_clear(tmp_path: Path) -> None:
    from ECL.plugins.worker_service import PluginWorkerService

    received: list[tuple[str, dict]] = []
    service = PluginWorkerService(
        tmp_path,
        slot_sink=lambda name, payload: received.append((name, payload)),
        ui_injection_allowed=lambda: False,
    )
    with pytest.raises(ProtocolError) as error:
        service._publish_slot("demo", {"kind": "html", "slot_id": "plugin-slot-content-bottom"})
    assert error.value.error_code == "PLUGIN_UI_INJECTION_DISABLED"
    service._publish_slot("demo", {"kind": "clear"})
    assert received == [("demo", {"kind": "clear"})]


def test_slot_registration_requires_host_sink(tmp_path: Path) -> None:
    broker, session, _received = _broker(tmp_path, allow=True, with_sink=False)
    try:
        with pytest.raises(ProtocolError) as error:
            _request(
                broker,
                "ui.register_html",
                {"slot_id": "plugin-slot-content-bottom", "html": "<p>hi</p>"},
            )
        assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"
    finally:
        broker.close()
        session.close()
