from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from ECL.services.game.launch_settings import InstanceLaunchOverrides, LaunchSettingsResolver


def test_instance_and_single_use_overrides_preserve_false_and_empty() -> None:
    global_settings = {
        "java_auto": False,
        "java_path": "global-java",
        "memory_auto": False,
        "memory_size": 4096,
        "fullscreen": True,
        "lock_memory": True,
        "wrapper_command": "global-wrapper",
        "window_title_template": "global-title",
        "jvm_args": ["-Dglobal=1"],
        "game_args_tail": '--name "a b"',
    }
    raw = {
        "javaMode": "auto",
        "memoryMode": "manual",
        "memory": 2048,
        "fullscreen": False,
        "lockMemory": False,
        "commandOverrides": ["wrapperCommand"],
        "wrapperCommand": "",
        "jvmArgs": "-Dinstance=1",
        "preLaunchCommand": "",
        "schemaVersion": 2,
    }
    original = deepcopy(raw)
    effective = LaunchSettingsResolver.resolve(
        global_settings,
        InstanceLaunchOverrides.model_validate(raw),
        {
            "memory": 3072,
            "jvm_args": ["-Dcli=1"],
            "game_args": ["--cli"],
        },
    )
    assert effective.java_path is None
    assert effective.memory == 3072
    assert effective.fullscreen is False
    assert effective.lock_memory is False
    assert effective.wrapper_command == ""
    assert effective.window_title == "global-title"
    assert effective.jvm_args == ["-Dglobal=1", "-Dinstance=1", "-Dcli=1"]
    assert effective.game_args == ["--name", "a b", "--cli"]
    assert raw == original


def test_legacy_settings_migrate_without_overwriting_explicit_modes() -> None:
    legacy = InstanceLaunchOverrides.model_validate(
        {
            "customJava": True,
            "javaPath": "legacy-java",
            "customMemory": True,
            "memory": 8192,
            "wrapperCommand": "legacy-wrapper",
            "isolated": False,
        }
    )
    effective = LaunchSettingsResolver.resolve({}, legacy)
    assert effective.java_path == "legacy-java"
    assert effective.memory == 8192
    assert effective.wrapper_command == "legacy-wrapper"
    assert effective.version_isolation is False
    independent = InstanceLaunchOverrides.model_validate(
        {
            "schemaVersion": 2,
            "javaMode": "inherit",
            "customJava": True,
            "wrapperCommand": "kept-in-editor",
            "commandOverrides": [],
        }
    )
    assert LaunchSettingsResolver.resolve({"wrapper_command": "global"}, independent).wrapper_command == "global"


def test_inherited_settings_track_global_changes() -> None:
    instance = InstanceLaunchOverrides()
    first = LaunchSettingsResolver.resolve(
        {"java_auto": False, "java_path": "first", "memory_auto": False, "memory_size": 2048}, instance
    )
    second = LaunchSettingsResolver.resolve(
        {"java_auto": False, "java_path": "second", "memory_auto": False, "memory_size": 4096}, instance
    )
    assert (first.java_path, first.memory) == ("first", 2048)
    assert (second.java_path, second.memory) == ("second", 4096)


@pytest.mark.parametrize("raw", [{"schemaVersion": 3}, {"memory": 100}, {"width": 10}, {"renderer": "invalid"}])
def test_settings_reject_unsupported_schema_and_invalid_values(raw) -> None:
    with pytest.raises(ValidationError):
        InstanceLaunchOverrides.model_validate(raw)


def test_manual_java_requires_path_and_extra_file_fields_survive() -> None:
    instance = InstanceLaunchOverrides.model_validate({"javaMode": "manual", "unrelated": {"keep": True}})
    assert instance.model_dump(by_alias=True)["unrelated"] == {"keep": True}
    with pytest.raises(ValueError, match="Java"):
        LaunchSettingsResolver.resolve({}, instance)


def test_argument_parser_preserves_windows_paths_quotes_and_explicit_empty_args() -> None:
    assert LaunchSettingsResolver.parse_arguments(
        r'-Dname="ECL Player" -Djava=C:\Java\bin --server local\ host ""'
    ) == ["-Dname=ECL Player", r"-Djava=C:\Java\bin", "--server", "local host", ""]
    with pytest.raises(ValueError, match="引号"):
        LaunchSettingsResolver.parse_arguments('-Dname="unclosed')


def test_instance_settings_reset_preserves_unknown_fields_and_future_schema_is_read_only(tmp_path) -> None:
    from ECL.services.game import GameService
    from ECL.utils.errors import GameServiceError

    service = GameService(object(), data_path=tmp_path / "data", enable_version_watcher=False)
    root = tmp_path / "minecraft"
    settings_path = root / "versions" / "Foo" / ".ecl" / "settings.json"
    try:
        service.write_version_settings(
            root, "Foo", {"customMemory": True, "memory": 6144, "extensionField": {"value": 1}}
        )
        assert service.write_version_settings(root, "Foo", {}) == {"extensionField": {"value": 1}}
        settings_path.write_text('{"schemaVersion": 3, "futureField": true}', encoding="utf-8")
        original = settings_path.read_bytes()
        with pytest.raises(GameServiceError) as error:
            service.write_version_settings(root, "Foo", {})
        assert error.value.error_code == "SETTINGS_SCHEMA_UNSUPPORTED"
        assert settings_path.read_bytes() == original
    finally:
        service.close()
