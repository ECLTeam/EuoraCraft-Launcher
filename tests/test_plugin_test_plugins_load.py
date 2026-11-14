from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.framework import PluginManager


def test_tracked_worker_example_is_discoverable_but_never_auto_executes(tmp_path: Path) -> None:
    framework = PluginManager()
    framework.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        source = Path(__file__).resolve().parents[1] / "examples/plugins/worker_echo"
        result = framework.install(str(source))
        assert result.success
        entry = framework.list_plugins()[0]
        assert entry["name"] == "worker_echo"
        assert entry["status"] == "disabled"
        assert framework.get_plugin("worker_echo") is None
        assert framework.worker_service.runtime_info("worker_echo")["pid"] is None
    finally:
        framework.close()


def test_tracked_panel_example_enables_and_runs_its_declared_action(tmp_path: Path) -> None:
    framework = PluginManager()
    framework.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        source = Path(__file__).resolve().parents[1] / "examples/plugins/worker_panel"
        assert framework.install(str(source)).success
        service = framework.worker_service
        assert framework.enable("worker_panel").success
        broker = service.broker("worker_panel")
        panel = broker.panels_view()[0]
        assert panel["key"] == "main"
        button = panel["nodes"][1]["children"][0]
        assert button["command"] == "greet"
        assert broker.call_command(button["command"], button["args"])["message"] == "你好，面板（第 1 次）"
        data = broker.handle(RpcRequest("panels.data", {"key": "main"}, monotonic() + 5))
        assert data["lists"]["history"] == [{"index": "1", "message": "第 1 次问候已完成"}]
        assert data["tasks"]["progress"]["state"] == "succeeded"
        assert framework.disable("worker_panel").success
        deadline = monotonic() + 5
        while service.runtime_info("worker_panel")["cleanup_state"] != "completed" and monotonic() < deadline:
            sleep(0.02)
        assert service.runtime_info("worker_panel")["cleanup_state"] == "completed"
        assert service.runtime_info("worker_panel")["run_state"] == "disabled"
    finally:
        framework.close()
