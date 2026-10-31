# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证受管工具的声明校验、未声明拒绝与通道缺失时的明确失败。
#
# 公开接口：
#   - test_tools_require_host_channel(request) -> None
#   - test_tool_run_requires_declaration(request) -> None
#   - test_tool_executable_cannot_escape_package(request) -> None
#   - test_tool_args_are_bounded(request) -> None
#   - test_tool_run_and_stop_forward_to_channel(request) -> None
# ============================================================

from __future__ import annotations

from time import monotonic
from uuid import uuid4

import pytest

from ECL.foundation.event_bus import EventBus
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.runtime.broker import ToolChannel
from ECL.services.processes import ProcessService
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture


def send(broker, method: str, params: dict) -> dict:
    return broker.handle(RpcRequest(method, params, monotonic() + 5, uuid4().hex))


def attach(broker, seen: list[tuple]) -> None:
    broker._tool_channel = ToolChannel(
        run=lambda name, definition, args: seen.append(("run", name, args)) or "process-1",
        stop=lambda reference: seen.append(("stop", reference)) or True,
    )


def test_tools_require_host_channel(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        send(broker, "tools.run", {"tool": "demo", "args": []})
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"


def test_tool_run_requires_declaration(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    attach(broker, [])
    with pytest.raises(ProtocolError) as error:
        send(broker, "tools.run", {"tool": "demo", "args": []})
    assert error.value.error_code == "PLUGIN_TOOL_NOT_DECLARED"


@pytest.mark.parametrize("executable", ["../escape.exe", "/usr/bin/sh", "tools/../../x.exe"])
def test_tool_executable_cannot_escape_package(request, executable: str) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    attach(broker, [])
    with pytest.raises(ProtocolError):
        send(
            broker,
            "tools.register",
            {"name": "demo", "title": "示例", "executable": executable, "args": []},
        )


def test_tool_args_are_bounded(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    attach(broker, [])
    send(broker, "tools.register", {"name": "demo", "title": "示例", "executable": "tool.exe", "args": []})
    with pytest.raises(ProtocolError) as error:
        send(broker, "tools.run", {"tool": "demo", "args": ["x"] * 17})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"


def test_process_ownership_separates_plugins_and_game() -> None:
    """只有创建者能操作自己的进程，游戏实例与其他插件进程都不属于它。"""
    service = ProcessService(EventBus())
    service._meta["tool-1"] = {"name": "示例", "type": "plugin:demo", "stdin": False, "owner": "demo"}
    service._meta["game-1"] = {"name": "1.21.1", "type": "minecraft", "stdin": True}
    assert service.owned_by("tool-1", "demo") is True
    assert service.owned_by("tool-1", "other") is False
    assert service.owned_by("game-1", "demo") is False
    assert service.owned_by("missing", "demo") is False


def test_tool_run_and_stop_forward_to_channel(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    seen: list[tuple] = []
    attach(broker, seen)
    send(
        broker,
        "tools.register",
        {"name": "demo", "title": "示例", "executable": "bin/tool.exe", "args": ["--fixed"]},
    )
    assert send(broker, "tools.run", {"tool": "demo", "args": ["--extra"]}) == {"process_ref": "process-1"}
    assert send(broker, "tools.stop", {"process_ref": "process-1"}) == {"stopped": True}
    assert seen == [("run", "demo", ("--extra",)), ("stop", "process-1")]
