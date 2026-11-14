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
#   - test_panel_structure_limits_are_removed(panel_setup) -> None
#   - test_panel_new_display_nodes_register_and_data_roundtrip(panel_setup) -> None
#   - test_panel_list_row_actions_require_key_column(panel_setup) -> None
#   - test_panel_refresh_seconds_bounds(panel_setup) -> None
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

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_models import PluginContentBinding
from ECL.plugins.run_store import PluginRunStore
from ECL.plugins.runtime.broker import PluginHostBroker

TEXT_NODE = {"type": "text", "text": "说明"}


@pytest.fixture
def panel_setup(tmp_path: Path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("value = 1", encoding="utf-8")
    binding = PluginContentBinding.capture(root, "windows-x86_64-cp311")
    store = PluginRunStore()
    session = store.open_session(store.new_installation_id(), binding)
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
    request(broker, "panels.register", {"panels": [panel([TEXT_NODE])]})
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.register", {"panels": [panel([TEXT_NODE])]})
    assert error.value.error_code == "PLUGIN_REGISTRATION_CONFLICT"


def test_panel_structure_limits_are_removed(panel_setup) -> None:
    """嵌套深度、节点数量与列数不再设人为上限，合法结构一律放行。"""
    broker, _payload = panel_setup
    define_setting(broker)
    deep = {"type": "group", "children": [{"type": "group", "children": [{"type": "group", "children": [TEXT_NODE]}]}]}
    wide = [{"type": "text", "text": f"n{i}"} for i in range(60)]
    many_columns = [{"key": f"col{i}", "label": f"列{i}"} for i in range(20)]
    list_node = {"type": "list", "source": "rows", "columns": many_columns}
    request(broker, "panels.register", {"panels": [panel([deep, *wide, list_node], key="wide")]})


def test_panel_new_display_nodes_register_and_data_roundtrip(panel_setup) -> None:
    """heading/divider/metric/badge 四种新展示节点随注册放行并出现在面板清单。"""
    broker, _payload = panel_setup
    nodes = [
        {"type": "heading", "level": 2, "text": "概览"},
        {"type": "divider"},
        {"type": "metric", "label": "实例数", "value": "12", "tone": "primary", "hint": "本机"},
        {"type": "badge", "text": "运行中", "tone": "success"},
    ]
    request(broker, "panels.register", {"panels": [panel(nodes, key="display")]})
    listed = request(broker, "panels.list", {})
    registered = next(item for item in listed["panels"] if item["key"] == "display")["nodes"]
    assert {item["type"] for item in registered} == {"heading", "divider", "metric", "badge"}
    assert registered[2]["value"] == "12"


def test_panel_list_row_actions_require_key_column(panel_setup) -> None:
    """行操作依赖宿主注入的 row_key，缺少 key 列即校验失败；合法声明随数据回流。"""
    broker, _payload = panel_setup
    action = {"label": "删除", "command": "demo.run", "tone": "danger", "confirm": "确认？"}
    with pytest.raises(ProtocolError) as error:
        request(
            broker,
            "panels.register",
            {
                "panels": [
                    panel(
                        [
                            {
                                "type": "list",
                                "source": "rows",
                                "columns": [{"key": "name", "label": "名"}],
                                "row_actions": [action],
                            }
                        ],
                        key="bad",
                    )
                ]
            },
        )
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    request(
        broker,
        "panels.register",
        {
            "panels": [
                panel(
                    [
                        {
                            "type": "list",
                            "source": "rows",
                            "columns": [{"key": "key", "label": "键"}, {"key": "name", "label": "名"}],
                            "row_actions": [action],
                        }
                    ],
                    key="good",
                )
            ]
        },
    )


def test_panel_refresh_seconds_bounds(panel_setup) -> None:
    """refresh_seconds 0-3600 合法，越界与负数拒绝，0 表示仅手动刷新。"""
    broker, _payload = panel_setup
    request(broker, "panels.register", {"panels": [panel([TEXT_NODE], key="auto", refresh_seconds=30)]})
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.register", {"panels": [panel([TEXT_NODE], key="fast", refresh_seconds=3601)]})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    with pytest.raises(ProtocolError) as error:
        request(broker, "panels.register", {"panels": [panel([TEXT_NODE], key="neg", refresh_seconds=-1)]})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"


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
