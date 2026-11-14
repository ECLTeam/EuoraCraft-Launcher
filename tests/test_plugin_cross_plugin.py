# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证跨插件命令的发现、调用与未导出拒绝。
#
# 公开接口：
#   - test_broker_exposes_only_exported_commands(panel_setup) -> None
#   - test_broker_cross_plugin_requires_channel(panel_setup) -> None
#   - test_broker_forwards_discovery_and_call(panel_setup) -> None
# ============================================================

from __future__ import annotations

from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.runtime.broker import CrossPluginChannel
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture


def send(broker, method: str, params: dict) -> dict:
    return broker.handle(RpcRequest(method, params, monotonic() + 5, uuid4().hex))


def test_broker_exposes_only_exported_commands(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    common = {
        "description": "",
        "input_fields": [],
        "output_fields": [],
        "callback_ref": "a" * 32,
    }
    send(broker, "commands.register", {**common, "name": "hidden", "exported": False})
    send(broker, "commands.register", {**common, "name": "shared", "exported": True})
    assert [item["command"] for item in broker.exported_commands()] == ["shared"]
    with pytest.raises(ProtocolError) as error:
        send(broker, "plugins.list", {})
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"


def test_broker_cross_plugin_requires_channel(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        send(broker, "plugins.call", {"plugin": "other", "command": "echo", "args": {}})
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"


def test_broker_forwards_discovery_and_call(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    seen: list[tuple] = []
    broker._cross_plugin = CrossPluginChannel(
        list_commands=lambda: [{"plugin": "other", "command": "echo"}],
        call=lambda target, command, args, timeout: seen.append((target, command, args)) or {"text": "ok"},
    )
    assert send(broker, "plugins.list", {}) == {"plugins": [{"plugin": "other", "command": "echo"}]}
    assert send(broker, "plugins.call", {"plugin": "other", "command": "echo", "args": {"text": "hi"}}) == {
        "value": {"text": "ok"}
    }
    assert seen and seen[0][0] == "other" and seen[0][1] == "echo"
