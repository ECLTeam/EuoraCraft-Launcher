from __future__ import annotations

from uuid import uuid4

import pytest

from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_models import PluginContentBinding
from ECL.plugins.run_store import PluginRunStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.custom_ui import _Window
from ECL.plugins.runtime.custom_ui_models import CustomUiDeclaration, CustomUiRequest
from tests.test_plugin_host_files import call


@pytest.mark.parametrize(
    "entry",
    [
        "../index.html",
        "C:/index.html",
        "/index.html",
        "a\\index.html",
        "https://site/index.html",
        "file:///index.html",
        "index.js",
    ],
)
def test_custom_ui_entry_never_accepts_host_path_or_url(entry):
    with pytest.raises(ValueError):
        CustomUiDeclaration(entry_file=entry, title="Demo")


@pytest.fixture
def ui(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("pass", encoding="utf-8")
    (code / "index.html").write_text("<!doctype html><title>Approved</title>", encoding="utf-8")
    binding = PluginContentBinding.capture(code, "windows-x86_64-cp311")
    store = PluginRunStore()
    session = store.open_session(store.new_installation_id(), binding)
    broker = PluginHostBroker(
        session,
        tmp_path / "data",
        lambda *_: {"ok": True, "data": {"value": {}}},
        run_state=lambda: "enabled",
        window_app=lambda: None,
        custom_ui=CustomUiDeclaration(entry_file="index.html", title="Demo"),
    )
    yield broker, code
    broker.close()
    session.close()


def test_custom_ui_approved_file_bytes_are_rechecked_before_use(ui):
    broker, code = ui
    assert "Approved" in broker.custom_ui._html()
    (code / "index.html").write_text("<!doctype html><title>Changed</title>", encoding="utf-8")
    with pytest.raises(ProtocolError) as exc:
        broker.custom_ui._html()
    assert exc.value.error_code == "PLUGIN_CONTENT_CHANGED"


def test_custom_ui_wrong_credential_closes_window_before_any_host_call(ui):
    broker, _ = ui
    window = _Window(broker.session.lease())
    broker.custom_ui._message(window, "ecl-plugin-message://request/wrong?payload=%7B%7D")
    assert window.destroyed.is_set() and window.error_code == "PLUGIN_UI_MESSAGE_REJECTED"
    assert not window.requests


def test_custom_ui_replay_returns_same_result_and_changed_identity_cannot_write_again(ui):
    broker, _ = ui
    call(broker, "settings.define", {"definitions": [{"key": "text", "type": "string", "default": ""}]})
    window = _Window(broker.session.lease())
    responses = []
    broker.custom_ui._reply = lambda _, response: responses.append(response)
    request = CustomUiRequest(
        id=uuid4().hex,
        method="settings.set",
        mutation_key=uuid4().hex,
        params={"values": {"text": "first"}, "expected_revision": 0},
    )
    for query in (
        request,
        request,
        request.model_copy(update={"params": {"values": {"text": "other"}, "expected_revision": 1}}),
    ):
        assert broker.custom_ui._slots.acquire(blocking=False)
        broker.custom_ui._execute(window, query)
    assert responses[0] == responses[1]
    assert responses[2]["error_code"] == "PLUGIN_MUTATION_CONFLICT"
    assert call(broker, "settings.get", {"keys": ["text"]}) == {"values": {"text": "first"}, "revision": 1}


def test_custom_ui_open_fails_closed_without_native_isolation(ui):
    """
    pytaurix 尚未提供顶层导航锁定与消息 URI 拦截原语；创建请求必须显式拒绝，
    不退回无隔离的窗口实现，也不允许失败后残留窗口引用。
    """
    broker, _ = ui
    with pytest.raises(ProtocolError) as exc:
        broker.custom_ui.handle("ui.windows.open", {})
    assert exc.value.error_code == "PLUGIN_UI_ISOLATION_UNAVAILABLE"
    assert broker.custom_ui._windows == {}
