from __future__ import annotations

import json
import sys

import pytest
from plugin_java_helpers import candidates, make_service

from ECL.java.models import JavaError


@pytest.fixture
def shared(tmp_path, monkeypatch):
    fixture = make_service(tmp_path, monkeypatch)
    yield fixture
    service, manager, _, client, operations = fixture
    service.close()
    manager.close()
    operations.close()
    client.close()


def test_missing_registry_is_empty_without_creating_registry(shared):
    manager = shared[1]
    manager.registry.registry_path.unlink()
    assert manager.registered_snapshot() == ()
    assert not manager.registry.registry_path.exists()


def test_corrupt_registry_is_preserved(shared):
    manager = shared[1]
    manager.registry.registry_path.write_bytes(b"broken")
    with pytest.raises(JavaError):
        manager.registered_snapshot()
    assert manager.registry.registry_path.read_bytes() == b"broken"


def test_observed_removal_and_readding_same_object_does_not_reactivate_identity(shared):
    service, manager, records, _, _ = shared
    selected = candidates(service)["items"][0]["resource_id"]
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": tuple(records[1:])}))
    service.refresh_java_resources()
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": tuple(records)}))
    assert selected not in service._java_catalog.active_refs()


def test_identity_index_rejects_rollback_and_reactivation(shared):
    service = shared[0]
    candidates(service)
    file = service.data_path / "plugins" / "java_resources.json"
    original = file.read_bytes()
    payload = json.loads(original)
    payload["revision"] = 0
    file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(JavaError):
        service.refresh_java_resources()
    assert json.loads(file.read_bytes())["revision"] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows javaw identity")
def test_javaw_permission_pins_actual_java_executable(shared):
    service, manager, records, _, _ = shared
    record = records[0]
    javaw = record.executable_path.with_name("javaw.exe")
    javaw.write_bytes(b"fixture javaw")
    updated = record.model_copy(update={"executable_path": javaw})
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": (updated,)}))
    selected = candidates(service)["items"][0]["resource_id"]
    record.executable_path.write_bytes(b"changed actual probe")
    service.refresh_java_resources()
    assert selected not in service._java_catalog.active_refs()
