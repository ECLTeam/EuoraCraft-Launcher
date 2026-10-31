from __future__ import annotations

import json
import sys

import pytest
from plugin_java_helpers import allow, candidates, invoke, make_service, runtime

from ECL.java.models import JavaError
from ECL.plugins.grant_models import PermissionDecision, PermissionDeclaration, PermissionManagementCaller
from ECL.plugins.grant_store import PluginGrantError


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


@pytest.mark.parametrize("capability", ["java.read", "java.use", "java.catalog.read"])
def test_manifest_cannot_choose_local_java_identity(capability):
    with pytest.raises(ValueError):
        PermissionDeclaration(capability=capability, resource_refs=("a" * 32,))


def test_observed_removal_and_readding_same_object_requires_new_review(shared):
    service, manager, records, _, _ = shared
    selected = candidates(service)["items"][0]["resource_id"]
    allow(service, [selected])
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": tuple(records[1:])}))
    service.refresh_java_resources()
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": tuple(records)}))
    page = candidates(service)
    previous = next(item for item in page["items"] if item["resource_id"] == selected)
    assert not previous["available"]
    with pytest.raises(PluginGrantError):
        invoke(runtime(service), "java.inventory")


def test_unknown_java_identity_cannot_be_granted(shared):
    service = shared[0]
    candidates(service)
    with pytest.raises(PluginGrantError):
        allow(service, ["f" * 32])


def test_identity_index_rejects_rollback_and_reactivation(shared):
    service = shared[0]
    candidates(service)
    file = service.data_path / "plugin_java_resources.json"
    original = file.read_bytes()
    payload = json.loads(original)
    payload["revision"] = 0
    file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(JavaError):
        service.refresh_java_resources()
    assert json.loads(file.read_bytes())["revision"] == 0


def test_java_catalog_decision_cannot_accept_resource_scope(shared):
    service = shared[0]
    page = candidates(service)
    view = service.permissions("java-test")
    with pytest.raises(PluginGrantError):
        service.update_permissions(
            "java-test",
            view["content_digest"],
            view["revision"],
            (
                PermissionDecision(
                    capability="java.catalog.read", decision="allow", resource_refs=(page["items"][0]["resource_id"],)
                ),
            ),
            PermissionManagementCaller.main_webview,
        )


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
    allow(service, [selected])
    record.executable_path.write_bytes(b"changed actual probe")
    service.refresh_java_resources()
    assert selected not in service._java_catalog.active_refs()
