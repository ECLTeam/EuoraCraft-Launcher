from __future__ import annotations

import json

import pytest
from test_plugin_worker_manager import make_plugin

from ECL.cli import LaunchOptions, apply_launch_overrides, parse_launch_options
from ECL.foundation import EventBus
from ECL.host.single_instance import SingleInstanceService, probe_running_instance
from ECL.plugins import PluginManager
from ECL.utils.config import ConfigStore
from ECL.utils.errors import ConfigValidationError


@pytest.mark.parametrize("flag, expected", [("--plugin-dev", True), ("--no-plugin-dev", False)])
def test_plugin_development_flags_override_file_without_mutation(flag, expected):
    options = parse_launch_options([flag])
    original = {"plugins": {"development_mode": not expected, "allow_ui_injection": False}}
    assert apply_launch_overrides(original, options)["plugins"] == {
        "development_mode": expected,
        "allow_ui_injection": False,
    }
    assert original["plugins"]["development_mode"] is not expected
    assert parse_launch_options([]).plugin_development is None


def test_plugin_development_conflicts_and_headless_are_rejected():
    for argv in (
        ["--plugin-dev", "--no-plugin-dev"],
        ["--headless", "--task", "runtime-check", "--plugin-dev"],
        ["--headless", "--task", "runtime-check", "--no-plugin-dev"],
    ):
        with pytest.raises(SystemExit) as error:
            parse_launch_options(argv)
        assert error.value.code == 2
    assert (
        apply_launch_overrides(
            {"plugins": {"development_mode": True}}, LaunchOptions(disable_plugins=True, plugin_development=True)
        )["plugins"]["development_mode"]
        is False
    )


@pytest.mark.parametrize("invalid", [None, "false", 0, 1, [], {}])
def test_invalid_development_config_preserves_original_file(tmp_path, invalid):
    config_file = tmp_path / "config" / "setting.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"plugins": {"development_mode": invalid}, "game": {"minecraft_paths": ["kept"]}})
    config_file.write_text(content, encoding="utf-8")
    store = ConfigStore(tmp_path)
    with pytest.raises(ConfigValidationError):
        store.get_config()
    assert config_file.read_text(encoding="utf-8") == content
    assert not config_file.with_suffix(".json.bak").exists()


def test_optional_development_config_is_not_written_and_is_preserved_on_other_updates(tmp_path):
    store = ConfigStore(tmp_path)
    assert "development_mode" not in store.get_config("plugins")
    original = store.config_path.read_bytes()
    assert "development_mode" not in store.get_config("plugins")
    assert store.config_path.read_bytes() == original
    store.save_config("plugins", {"development_mode": True, "allow_ui_injection": True})
    store.save_config("plugins", {"allow_ui_injection": False})
    assert store.get_config("plugins") == {"development_mode": True, "allow_ui_injection": False}
    before = store.config_path.read_bytes()
    with pytest.raises(ConfigValidationError):
        store.save_config("plugins", {"development_mode": "true"})
    assert store.config_path.read_bytes() == before


@pytest.mark.parametrize("flag", ["--plugin-dev", "--no-plugin-dev", "--plugin-d", "--no-plugin-d"])
@pytest.mark.parametrize("prefix", [[], ["--launch=Foo"], ["--open-page=download"]])
def test_running_instance_rejects_development_options(tmp_path, flag, prefix):
    events = EventBus()
    received = []
    events.subscribe("launcher:focus_request", received.append)
    service = SingleInstanceService(events, tmp_path, "test")
    service.start()
    try:
        with pytest.raises(ValueError, match="拒绝"):
            probe_running_instance(tmp_path, [*prefix, flag])
        assert service.take_launch_requests() == []
        assert received == []
    finally:
        service.close()


def test_startup_development_loads_only_one_stable_copy_and_does_not_restore_trust(tmp_path):
    source = tmp_path / "data/plugins/sources/example"
    marker = tmp_path / "executions"
    make_plugin(source, marker)
    code_file = source / "main.py"
    code_file.write_text(
        code_file.read_text(encoding="utf-8").replace(
            f"Path({str(marker)!r}).write_text(str(os.getpid()))",
            f"p = Path({str(marker)!r}); p.write_text((p.read_text() if p.exists() else '') + __file__ + '\\n')",
        ),
        encoding="utf-8",
    )
    manager = PluginManager()
    # 首次运行不进入开发模式也不启用，验证开发模式接管时才建立快照执行。
    manager.initialize(tmp_path / "data", tmp_path / "resources", auto_enable=False)
    try:
        assert not (tmp_path / "executions").exists()
    finally:
        manager.close()
    manager = PluginManager()
    try:
        manager.initialize(tmp_path / "data", tmp_path / "resources", development_mode=True)
        assert manager.list_plugins()[0]["status"] == "enabled"
        executions = marker.read_text().splitlines()
        assert len(executions) == 1
        assert "plugin_development_snapshots" in executions[0]
        assert manager.worker_service.development.view()["items"][0]["reload_count"] == 1
        code_file.write_text(code_file.read_text(encoding="utf-8") + "\n# source changed\n", encoding="utf-8")
    finally:
        manager.close()
    manager = PluginManager()
    try:
        # 开发模式按当前源重建快照运行；信任不跨运行持久，属用户显式开启的能力。
        manager.initialize(tmp_path / "data", tmp_path / "resources", development_mode=True)
        assert manager.list_plugins()[0]["status"] == "enabled"
        executions = marker.read_text().splitlines()
        assert len(executions) == 2
        assert all("plugin_development_snapshots" in item for item in executions)
    finally:
        manager.close()
