from __future__ import annotations

from pathlib import Path

from test_plugin_worker_manager import make_plugin

from ECL.plugins import PluginManager


def test_reinstall_updates_code_without_enabling_or_inheriting_review(tmp_path: Path) -> None:
    source = tmp_path / "source"
    marker = tmp_path / "marker"
    make_plugin(source, marker)
    framework = PluginManager()
    framework.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert framework.install(str(source)).success
        identity = framework.list_plugins()[0]["installation_id"]
        (source / "main.py").write_text("raise RuntimeError('not executed during install')", encoding="utf-8")
        assert framework.install(str(source)).success
        entry = framework.list_plugins()[0]
        assert entry["installation_id"] == identity
        assert entry["review_required"] is True
        assert not marker.exists()
        assert framework.get_plugin("example") is None
    finally:
        framework.close()


def test_reload_does_not_bypass_initial_review(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    make_plugin(tmp_path / "data/plugins/example", marker)
    framework = PluginManager()
    framework.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert not framework.reload("example").success
        assert not marker.exists()
        assert framework.get_plugin("example") is None
    finally:
        framework.close()
