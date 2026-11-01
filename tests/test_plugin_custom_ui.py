from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.installation_store import WorkerManifest
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


def test_custom_ui_manifest_requires_independent_permission():
    with pytest.raises(ValueError):
        WorkerManifest.model_validate(
            {"api_version": 2, "name": "demo", "custom_ui": {"entry_file": "index.html", "title": "Demo"}}
        )


@pytest.fixture
def ui(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("pass", encoding="utf-8")
    (code / "index.html").write_text("<!doctype html><title>Approved</title>", encoding="utf-8")
    binding = PluginContentBinding.capture(
        code, (PermissionDeclaration(capability="ui.custom.execute"),), "windows-x86_64-cp311"
    )
    store = PluginGrantStore(tmp_path / "data", implemented_capabilities=PluginHostBroker.implemented_capabilities)
    session = store.open_session(store.new_installation_id(), binding)
    store.update(
        session,
        (PermissionDecision(capability="ui.custom.execute", decision="allow"),),
        expected_revision=0,
        content_digest=binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
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
    window = _Window(broker.session.authorize("ui.custom.execute"))
    broker.custom_ui._message(window, "ecl-plugin-message://request/wrong?payload=%7B%7D")
    assert window.destroyed.is_set() and window.error_code == "PLUGIN_UI_MESSAGE_REJECTED"
    assert not window.requests


def test_custom_ui_replay_returns_same_result_and_changed_identity_cannot_write_again(ui):
    broker, _ = ui
    call(broker, "settings.define", {"definitions": [{"key": "text", "type": "string", "default": ""}]})
    window = _Window(broker.session.authorize("ui.custom.execute"))
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


@pytest.mark.skipif(
    not os.environ.get("ECL_NATIVE_UI_GATE"),
    reason="需要固定原生 Wheel 的真实系统 WebView 验收",
)
def test_real_native_custom_ui_worker_round_trip_revocation_and_reopen(tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONUTF8="1", _PYTAURI_DIST="pytauri-wheel")
    environment.pop("WEBVIEW2_USER_DATA_FOLDER", None)
    environment.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    runtime = os.environ.get("ECL_NATIVE_RUNTIME_PATH")
    environment["PYTHONPATH"] = os.pathsep.join([str(root), *([runtime] if runtime else [])])
    output = tmp_path / "native-ui"
    result = subprocess.run(
        [sys.executable, str(root / "tests/plugin_custom_ui_gate_host.py"), str(output)],
        cwd=root,
        env=environment,
        capture_output=True,
        timeout=60,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    report = output / "result.json"
    assert report.exists(), result.stderr.decode("utf-8", errors="replace")[-2000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert result.returncode == 0 and data["ok"] is True, (
        data,
        result.stderr.decode("utf-8", errors="replace")[-2000:],
    )
    assert data["revocation_closed"] and data["reopened_and_closed"]
