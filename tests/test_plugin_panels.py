# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证声明式面板的注册边界、数据拉取与命令动作通道。
#
# 公开接口：
#   - test_register_and_list_panels(panel_setup) -> None
#   - test_register_rejects_invalid_panels(panel_setup) -> None
#   - test_panel_data_without_callback_is_empty(panel_setup) -> None
#   - test_panel_data_reads_callback_payload(panel_setup) -> None
#   - test_panel_data_rejects_invalid_payload(panel_setup) -> None
#   - test_panel_action_runs_registered_command(panel_setup) -> None
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

TEXT_NODE = {"type": "text", "text": "说明"}


@pytest.fixture
def panel_setup(tmp_path: Path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("value = 1", encoding="utf-8")
    declarations = (PermissionDeclaration(capability="instances.read"),)
    binding = PluginContentBinding.capture(root, declarations, "windows-x86_64-cp311")
    store = PluginGrantStore(tmp_path, implemented_capabilities=PluginHostBroker.implemented_capabilities)
    session = store.open_session(store.new_installation_id(), binding)
    store.update(
        session,
        (PermissionDecision(capability="instances.read", decision="allow"),),
        expected_revision=0,
        content_digest=binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    payload: dict[str, object] = {}

    def invoke(_method, params, _timeout):
        payload.update(params["args"])
        return {"ok": True, "data": {"value": payload}}

    broker = PluginHostBroker(session, tmp_path, invoke, run_state=lambda: "enabled")
    yield broker, payload
    broker.close()
    session.close()


def request(broker: PluginHostBroker, method: str, params: dict, key: str | None = None) -> dict:
    return broker.handle(RpcRequest(method, params, monotonic() + 5, key or uuid4().hex))


def define_setting(broker: PluginHostBroker) -> None:
    request(
        broker,
        "settings.define",
        {"definitions": [{"key": "greeting", "type": "string", "default": "hello"}]},
    )


def panel(nodes: list[dict], key: str = "main", **extra) -> dict:
    return {"key": key, "title": "演示面板", "nodes": nodes, **extra}


def test_register_and_list_panels(panel_setup) -> None:
    broker, _payload = panel_setup
    define_setting(broker)
    result = request(
        broker,
        "panels.register",
        {"panels": [panel([TEXT_NODE, {"type": "setting", "key": "greeting"}])]},
    )
    assert len(result["panel_refs"]) == 1
    listed = request(broker, "panels.list", {})["panels"]
    assert [item["key"] for item in listed] == ["main"]
    assert "data_callback_ref" not in listed[0]
    assert broker.panels_view()[0]["nodes"][0] == {"type": "text", "text": "说明", "tone": "default"}


def test_register_rejects_invalid_panels(panel_setup) -> None:
    broker, _payload = panel_setup
    define_setting(broker)
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.register", {"panels": [panel([{"type": "setting", "key": "missing"}])]})
    assert error.value.error_code == "PLUGIN_SETTING_NOT_DEFINED"
    with pytest.raises(ProtocolError) as error:
        request(
            broker,
            "panels.register",
            {"panels": [panel([{"type": "unknown"}])]},
        )
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    deep = {"type": "group", "children": [{"type": "group", "children": [{"type": "group", "children": [TEXT_NODE]}]}]}
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.register", {"panels": [panel([deep])]})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    request(broker, "panels.register", {"panels": [panel([TEXT_NODE])]})
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.register", {"panels": [panel([TEXT_NODE])]})
    assert error.value.error_code == "PLUGIN_REGISTRATION_CONFLICT"


def test_panel_data_without_callback_is_empty(panel_setup) -> None:
    broker, _payload = panel_setup
    define_setting(broker)
    request(broker, "panels.register", {"panels": [panel([TEXT_NODE])]})
    assert request(broker, "panels.data", {"key": "main"}) == {"lists": {}, "tasks": {}}
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.data", {"key": "other"})
    assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"


def test_panel_data_reads_callback_payload(panel_setup) -> None:
    broker, payload = panel_setup
    define_setting(broker)
    payload.update(
        {
            "lists": {"rows": [{"name": "a"}]},
            "tasks": {"job": {"state": "running", "percent": 40, "detail": "进行中"}},
        }
    )
    request(broker, "panels.register", {"panels": [panel([TEXT_NODE], data_callback_ref="0" * 32)]})
    data = request(broker, "panels.data", {"key": "main"})
    assert data["lists"] == {"rows": [{"name": "a"}]}
    assert data["tasks"]["job"] == {"state": "running", "percent": 40, "detail": "进行中"}


def test_panel_data_rejects_invalid_payload(panel_setup) -> None:
    broker, payload = panel_setup
    define_setting(broker)
    payload.update({"lists": {"rows": [{"name": "a" * 201}]}})
    request(broker, "panels.register", {"panels": [panel([TEXT_NODE], data_callback_ref="1" * 32)]})
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.data", {"key": "main"})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"


def test_panel_action_runs_registered_command(panel_setup) -> None:
    broker, _payload = panel_setup
    request(
        broker,
        "commands.register",
        {
            "name": "greet",
            "description": "",
            "input_fields": [{"name": "name", "type": "string", "max_length": 120}],
            "output_fields": [{"name": "name", "type": "string"}],
            "callback_ref": "a" * 32,
            "exported": False,
        },
    )
    assert broker.call_command("greet", {"name": "ECL"}) == {"name": "ECL"}
    with pytest.raises(ProtocolError) as error:
        broker.call_command("missing", {})
    assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"
    with pytest.raises(ProtocolError) as error:
        broker.call_command("greet", {"name": 1})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
