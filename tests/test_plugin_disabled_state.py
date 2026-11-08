from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_plugin_worker_manager import make_plugin, review

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins import PluginManager


def test_session_disable_blocks_load_enable_reload_and_worker_without_persisting(tmp_path):
    source = tmp_path / "data/plugins/example"
    marker = tmp_path / "executed"
    make_plugin(source, marker)
    manager = PluginManager()
    manager.initialize(
        tmp_path / "data", tmp_path / "resources", disabled_plugin_ids=frozenset({"example"}), development_mode=True
    )
    try:
        entry = manager.list_plugins()[0]
        assert entry["status"] == "disabled"
        assert entry["error"] == "PLUGIN_DISABLED_FOR_SESSION"
        assert manager.worker_service.directory_sources() == ()
        assert manager.worker_service.development.view()["items"] == []
        assert manager.enable("example").status == "forbidden"
        assert manager.reload("example").status == "forbidden"
        with pytest.raises(ProtocolError) as error:
            manager.worker_service.enable("example")
        assert error.value.error_code == "PLUGIN_DISABLED_FOR_SESSION"
        manager._load_plugin(source, source / "plugin.json", False)
        assert not marker.exists()
        assert not (tmp_path / "data/plugin_state.json").exists()
    finally:
        manager.close()
    restarted = PluginManager()
    restarted.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        review(restarted)
        assert restarted.enable("example").success
        assert marker.exists()
    finally:
        restarted.close()


def test_session_disable_propagates_only_through_required_dependencies(tmp_path):
    for name, dependency in (
        ("example", {}),
        ("required", {"example": "*"}),
        ("optional", {"example": {"optional": True}}),
    ):
        source = tmp_path / "data/plugins" / name
        make_plugin(source, tmp_path / name)
        metadata = json.loads((source / "plugin.json").read_text())
        metadata.update(name=name, dependencies=dependency)
        (source / "plugin.json").write_text(json.dumps(metadata))
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources", disabled_plugin_ids=frozenset({"example"}))
    try:
        entries = {entry["name"]: entry for entry in manager.list_plugins()}
        assert entries["required"]["error"] == "PLUGIN_DEPENDENCY_DISABLED_FOR_SESSION"
        assert manager.enable("required").status == "forbidden"
        assert entries["optional"]["error"] != "PLUGIN_DEPENDENCY_DISABLED_FOR_SESSION"
        assert not (tmp_path / "example").exists()
        assert not (tmp_path / "required").exists()
    finally:
        manager.close()


def test_unknown_session_disabled_plugin_fails_before_any_plugin_executes(tmp_path):
    make_plugin(tmp_path / "data/plugins/example", tmp_path / "executed")
    manager = PluginManager()
    try:
        with pytest.raises(ProtocolError) as error:
            manager.initialize(tmp_path / "data", tmp_path / "resources", disabled_plugin_ids=frozenset({"missing"}))
        assert error.value.error_code == "PLUGIN_NOT_FOUND"
        assert not (tmp_path / "executed").exists()
    finally:
        manager.close()


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
