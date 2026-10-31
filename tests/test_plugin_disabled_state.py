from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_plugin_worker_manager import make_plugin, review

from ECL.plugins import PluginManager


@pytest.fixture
def framework(tmp_path: Path):
    make_plugin(tmp_path / "data/plugins/example", tmp_path / "marker")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources", auto_enable=False)
    yield manager
    manager.close()


def test_safe_mode_discovers_metadata_without_executing(framework, tmp_path):
    entry = framework.list_plugins()[0]
    assert entry["name"] == "example"
    assert entry["version"] == "1.0.0"
    assert entry["review_required"]
    assert not (tmp_path / "marker").exists()


def test_disabling_unstarted_plugin_persists_user_choice(framework, tmp_path):
    assert framework.disable("example").success
    assert json.loads((tmp_path / "data/plugin_state.json").read_text())["disabled"] == ["example"]
    assert framework.get_plugin("example") is None


def test_restart_preserves_disabled_choice(framework, tmp_path):
    framework.disable("example")
    restarted = PluginManager()
    restarted.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert restarted.list_plugins()[0]["status"] == "disabled"
        assert not (tmp_path / "marker").exists()
    finally:
        restarted.close()


def test_uninstall_disabled_plugin_removes_files_and_eligibility(framework, tmp_path):
    identity = framework.list_plugins()[0]["installation_id"]
    assert framework.disable("example").success
    assert framework.uninstall("example").success
    assert framework.list_plugins() == []
    assert not (tmp_path / "data/plugins/example").exists()
    source = tmp_path / "source"
    make_plugin(source, tmp_path / "marker")
    assert framework.install(str(source)).success
    assert framework.list_plugins()[0]["installation_id"] != identity


def test_close_does_not_mark_installed_plugins_disabled(framework, tmp_path):
    framework.close()
    state_file = tmp_path / "data/plugin_state.json"
    assert not state_file.exists() or "example" not in json.loads(state_file.read_text())["disabled"]


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Worker")
def test_enable_clears_disabled_choice_only_after_review(framework, tmp_path):
    framework.disable("example")
    assert not framework.enable("example").success
    review(framework)
    assert framework.enable("example").success
    assert "example" not in json.loads((tmp_path / "data/plugin_state.json").read_text())["disabled"]


def test_third_party_injection_requires_known_plugin(framework):
    # 未登记的来源仍被拒绝，避免伪造名称写入插槽。
    framework._on_html_injected("unknown", "slot", "<div />", None)
    framework._on_vue_slot_registered("unknown", "slot", "bad", "<div />", "", "")
    assert framework.get_slots() == {}
    assert framework.get_vue_slots() == {}
    # 已登记插件的内容进入收集路径；是否获准由 Broker 按 ui.inject 判定，
    # 第三方注入已按用户决定开放，不再在注册表层一律拒绝。
    framework._on_html_injected("example", "slot", "<div>ok</div>", None)
    assert framework.get_slots()["slot"][0]["plugin"] == "example"


def test_system_route_registration_remains_idempotent(tmp_path):
    root = tmp_path / "resources/resources/system_plugins/system"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text('{"name":"system"}')
    (root / "main.py").write_text("from ECL.plugins import Plugin as Base\nclass Plugin(Base):\n    pass\n")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        plugin = manager.get_plugin("system")
        manager._register_route(plugin, "/system", "System", "")
        manager._register_route(plugin, "/system", "System", "")
        assert len(manager.get_routes()) == 1
        assert manager.disable("system").status == "forbidden"
    finally:
        manager.close()
