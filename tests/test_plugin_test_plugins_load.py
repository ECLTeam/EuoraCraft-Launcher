from __future__ import annotations

from pathlib import Path

from ECL.plugins import PluginManager


def test_tracked_worker_example_is_discoverable_but_never_auto_executes(tmp_path: Path) -> None:
    framework = PluginManager()
    framework.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        source = Path(__file__).resolve().parents[1] / "examples/plugins/worker_echo"
        result = framework.install(str(source))
        assert result.success
        entry = framework.list_plugins()[0]
        assert entry["name"] == "worker_echo"
        assert entry["review_required"] is True
        assert framework.get_plugin("worker_echo") is None
        assert framework.worker_service.runtime_info("worker_echo")["pid"] is None
    finally:
        framework.close()
