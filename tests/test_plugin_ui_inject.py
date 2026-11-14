# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证界面注入权限门禁、插槽内容边界与可信接收方转发。
#
# 公开接口：
#   - test_html_slot_forwards_valid_content(tmp_path) -> None
#   - test_vue_slot_rejects_oversized_template(tmp_path) -> None
#   - test_slot_registration_requires_host_sink(tmp_path) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_models import PluginContentBinding
from ECL.plugins.run_store import PluginRunStore
from ECL.plugins.runtime.broker import PluginHostBroker


def _broker(tmp_path: Path, *, with_sink: bool = True):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("value = 1", encoding="utf-8")
    binding = PluginContentBinding.capture(root, "windows-x86_64-cp311")
    store = PluginRunStore()
    session = store.open_session(store.new_installation_id(), binding)
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


def test_html_slot_forwards_valid_content(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path)
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
    broker, session, received = _broker(tmp_path)
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


def test_slot_registration_requires_host_sink(tmp_path: Path) -> None:
    broker, session, _received = _broker(tmp_path, with_sink=False)
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
