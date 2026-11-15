# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证界面注入内容原样转发、无长度与数量限制，以及可信接收方门禁。
#
# 公开接口：
#   - test_html_slot_forwards_valid_content(tmp_path) -> None
#   - test_html_slot_forwards_unfiltered_content(tmp_path) -> None
#   - test_vue_slot_forwards_oversized_content(tmp_path) -> None
#   - test_pages_have_no_registration_limit(tmp_path) -> None
#   - test_slot_registration_requires_host_sink(tmp_path) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugins.contracts import ProtocolError
from ECL.plugins.run_models import PluginContentBinding
from ECL.plugins.run_store import PluginRunStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.sdk._rpc import RpcRequest


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


def test_html_slot_forwards_unfiltered_content(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path)
    html = '<div onclick="alert(1)"><script>window.__injected = true</script></div>'
    try:
        assert _request(
            broker,
            "ui.register_html",
            {"slot_id": "custom-area", "html": html, "key": "k" * 100},
        ) == {"registered": True}
        assert received[0]["slot_id"] == "custom-area"
        assert received[0]["html"] == html
        assert received[0]["key"] == "k" * 100
    finally:
        broker.close()
        session.close()


def test_vue_slot_forwards_oversized_content(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path)
    template, script, style = "x" * 60001, "y" * 60001, "z" * 60001
    try:
        assert _request(
            broker,
            "ui.register_vue",
            {
                "slot_id": "plugin-slot-content-bottom",
                "component_name": "DemoCard",
                "template": template,
                "script": script,
                "style": style,
            },
        ) == {"registered": True}
        assert received[0]["template"] == template
        assert received[0]["script"] == script
        assert received[0]["style"] == style
    finally:
        broker.close()
        session.close()


def test_pages_have_no_registration_limit(tmp_path: Path) -> None:
    broker, session, received = _broker(tmp_path)
    try:
        for index in range(17):
            assert _request(
                broker,
                "ui.register_page",
                {
                    "path": f"/page-{index}",
                    "title": f"Page {index}",
                    "component_name": f"Page{index}",
                    "template": "<div>page</div>",
                },
            ) == {"registered": True}
        assert len(received) == 17
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
