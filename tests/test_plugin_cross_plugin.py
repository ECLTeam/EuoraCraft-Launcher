# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证跨插件命令的发现、调用、未导出拒绝与目标禁用失效。
#
# 公开接口：
#   - test_broker_exposes_only_exported_commands(panel_setup) -> None
#   - test_broker_cross_plugin_requires_channel(panel_setup) -> None
#   - test_real_worker_calls_exported_command(tmp_path) -> None
#   - test_real_worker_rejects_disabled_target(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionManagementCaller
from ECL.plugins.runtime.broker import CrossPluginChannel
from ECL.plugins.worker_service import PluginWorkerService
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/plugins"


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


def _prepare(tmp_path: Path) -> PluginWorkerService:
    service = PluginWorkerService(tmp_path / "data")
    for name in ("worker_provider", "worker_caller"):
        example = EXAMPLES / name
        code = tmp_path / name
        code.mkdir()
        metadata = json.loads((example / "plugin.json").read_bytes())
        (code / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
        (code / "main.py").write_text((example / "main.py").read_text(encoding="utf-8"), encoding="utf-8")
        service.register(metadata, code, "directory")
        view = service.permissions(name)
        service.update_permissions(
            name,
            view["content_digest"],
            view["revision"],
            (),
            PermissionManagementCaller.main_webview,
        )
    return service


def test_real_worker_calls_exported_command(tmp_path: Path) -> None:
    service = _prepare(tmp_path)
    try:
        service.enable("worker_provider")
        service.enable("worker_caller")
        result = service.broker("worker_caller").call_command("call_target", {"text": "跨插件"})
        assert result == {"text": "跨插件"}
    finally:
        service.close()


def test_real_worker_rejects_disabled_target(tmp_path: Path) -> None:
    service = _prepare(tmp_path)
    try:
        service.enable("worker_provider")
        service.enable("worker_caller")
        service.stop("worker_provider")
        with pytest.raises(ProtocolError) as error:
            service.broker("worker_caller").call_command("call_target", {"text": "x"})
        assert error.value.error_code in {"PLUGIN_TARGET_DISABLED", "PLUGIN_COMMAND_FAILED"}
    finally:
        service.close()
