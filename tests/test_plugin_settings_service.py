from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.runtime.models import SettingDefinition
from ECL.plugins.settings import PluginSettingsService


def define(service, *, owner="a" * 32, digest="b" * 64, run="c" * 32, kind="string", default="hello", notify=None):
    return service.define(
        owner,
        digest,
        run,
        [SettingDefinition(key="greeting", type=kind, default=default)],
        notify or (lambda values, revision: None),
        lambda register: register(),
    )


def test_worker_and_disabled_management_share_values_and_events(tmp_path):
    service = PluginSettingsService(tmp_path)
    changes = []
    schema = define(service, notify=lambda values, revision: changes.append((values, revision)))
    service.publish("a" * 32, "c" * 32)
    assert service.update("a" * 32, "b" * 64, {"greeting": "saved"}, 0, lambda: None, schema_revision=schema) == 1
    assert changes == [({"greeting": "saved"}, 1)]
    assert service.view("a" * 32, "b" * 64, "c" * 32)["values"] == {"greeting": "saved"}
    service.release("a" * 32, "c" * 32)
    restarted = PluginSettingsService(tmp_path)
    assert restarted.view("a" * 32, "b" * 64)["editable"] is True
    assert (
        restarted.update("a" * 32, "b" * 64, {"greeting": "disabled edit"}, 1, lambda: None, schema_revision=schema)
        == 2
    )


def test_stale_content_or_schema_cannot_overwrite_after_reload(tmp_path):
    service = PluginSettingsService(tmp_path)
    old = define(service)
    service.publish("a" * 32, "c" * 32)
    service.release("a" * 32, "c" * 32)
    new = define(service, digest="d" * 64, run="e" * 32)
    service.publish("a" * 32, "e" * 32)
    assert old != new
    for digest, revision in (("b" * 64, old), ("d" * 64, old)):
        with pytest.raises(ProtocolError, match="定义") as error:
            service.update("a" * 32, digest, {"greeting": "stale"}, 0, lambda: None, schema_revision=revision)
        assert error.value.error_code == "PLUGIN_SETTING_SCHEMA_CHANGED"
    assert service.view("a" * 32, "d" * 64)["revision"] == 0


def test_removed_values_survive_and_type_change_preserves_original_file(tmp_path):
    service = PluginSettingsService(tmp_path)
    define(service)
    service.update("a" * 32, "b" * 64, {"greeting": "user"}, 0, lambda: None, run_id="c" * 32)
    target = tmp_path / "plugin_settings_v2" / f"{'a' * 32}.json"
    original = target.read_bytes()
    define(service, run="e" * 32, kind="bool", default=False)
    with pytest.raises(ProtocolError) as error:
        service.view("a" * 32, "b" * 64, "e" * 32)
    assert error.value.error_code == "PLUGIN_SETTING_TYPE_MISMATCH"
    assert target.read_bytes() == original
    service.define("a" * 32, "b" * 64, "f" * 32, [], lambda values, revision: None, lambda register: register())
    assert service.view("a" * 32, "b" * 64, "f" * 32)["values"] == {}
    assert json.loads(target.read_bytes())["values"] == {"greeting": "user"}


def test_corrupt_storage_is_not_an_empty_success(tmp_path):
    service = PluginSettingsService(tmp_path)
    define(service)
    target = tmp_path / "plugin_settings_v2" / f"{'a' * 32}.json"
    target.parent.mkdir()
    target.write_bytes(b"{broken")
    with pytest.raises(ProtocolError) as error:
        service.view("a" * 32, "b" * 64, "c" * 32)
    assert error.value.error_code == "PLUGIN_STORAGE_FAILED"
    assert target.read_bytes() == b"{broken"


def test_concurrent_writers_only_one_revision_commits(tmp_path):
    service = PluginSettingsService(tmp_path)
    define(service)

    def update(value):
        try:
            return service.update("a" * 32, "b" * 64, {"greeting": value}, 0, lambda: None, run_id="c" * 32)
        except ProtocolError as error:
            return error.error_code

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(update, ("first", "second")))
    assert sorted(map(str, results)) == ["1", "PLUGIN_REVISION_CONFLICT"]
    assert service.view("a" * 32, "b" * 64, "c" * 32)["revision"] == 1


def test_unknown_schema_and_other_installations_do_not_leak(tmp_path):
    service = PluginSettingsService(tmp_path)
    define(service)
    assert service.view("d" * 32, "b" * 64)["editable"] is False
    assert service.view("a" * 32, "f" * 64)["schema"] == []
    assert service.view("a" * 32, "b" * 64, "0" * 32)["editable"] is False


def test_corrupt_schema_does_not_replace_values_with_defaults(tmp_path):
    service = PluginSettingsService(tmp_path)
    define(service)
    service.publish("a" * 32, "c" * 32)
    service.update("a" * 32, "b" * 64, {"greeting": "user value"}, 0, lambda: None, run_id="c" * 32)
    schema_file = tmp_path / "plugin_settings_schemas" / f"{'a' * 32}.json"
    payload = json.loads(schema_file.read_bytes())
    payload["definitions"][0]["default"] = "tampered"
    schema_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProtocolError) as error:
        service.view("a" * 32, "b" * 64)
    assert error.value.error_code == "PLUGIN_STORAGE_FAILED"
    values_file = tmp_path / "plugin_settings_v2" / f"{'a' * 32}.json"
    assert json.loads(values_file.read_bytes())["values"] == {"greeting": "user value"}
