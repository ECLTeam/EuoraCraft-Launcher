from __future__ import annotations

import json
from threading import Event
from time import monotonic

import httpx
import pytest
from plugin_java_helpers import allow, candidates, make_service, runtime
from test_java_manager import _archive_bytes, _plan

from ECL.java.models import JavaError
from ECL.minecraft.game import JavaRuntime, JavaScanner
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_store import PluginRunError
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.services.operations import OperationOrigin
from tests.test_plugin_host_files import call, finish


@pytest.fixture
def installation(tmp_path, monkeypatch):
    service, manager, _records, client, operations = make_service(tmp_path, monkeypatch, install=True)
    archive = _archive_bytes()
    plan = _plan(manager, archive)
    actual_client = httpx.Client
    monkeypatch.setattr(
        "ECL.java.installer.httpx.Client",
        lambda **kwargs: actual_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive))),
    )

    def probe(path, **kwargs):
        return JavaRuntime(path, "21.0.2", "Fixture", "amd64", False)

    monkeypatch.setattr(JavaScanner, "probe", probe)
    page = candidates(service)
    allow(service, [page["items"][0]["resource_id"]], use=False, install=True)
    api = runtime(service)
    monkeypatch.setattr(manager.catalog, "release_snapshot", lambda *_: (21, (21,)))
    monkeypatch.setattr(manager.catalog, "package_snapshot", lambda *_: ((plan.package,), monotonic() + 60))
    broker = PluginHostBroker(
        service._entry("java-test").session,
        tmp_path / "data",
        lambda *_: {},
        run_state=lambda: "enabled",
        java=api,
        operations=operations,
    )
    package = call(broker, "java.catalog", {})["items"][0]["package_ref"]
    try:
        yield broker, api, manager, plan, package, service
    finally:
        broker.close()
        operations.close()
        service.close()
        manager.close()
        client.close()


def test_install_is_a_real_owned_task_and_result_can_be_selected_without_global_use(installation):
    broker, api, manager, _, package, _ = installation
    prepared = call(broker, "java.install_plan", {"package_ref": package})
    receipt = call(broker, "java.install", {"plan_ref": prepared["plan_ref"]})
    result = finish(broker, manager._operations, receipt)
    assert result["state"] == "completed", result
    assert result["effect"] == "committed"
    assert "download_url" not in json.dumps(result)
    record, check = api.selection(result["result"]["runtime_ref"], monotonic() + 5)
    check()
    assert record.executable_path.is_file()
    assert record.runtime_id in {item.runtime_id for item in manager.registered_snapshot()}
    assert not manager._plugin_install_owners


def test_foreign_or_extra_install_references_never_reach_installer(installation):
    broker, _, _, _, _, _ = installation
    with pytest.raises(ProtocolError):
        call(broker, "java.install", {"plan_ref": "f" * 32})
    with pytest.raises(ProtocolError):
        call(broker, "java.install_plan", {"package_ref": "f" * 32})
    with pytest.raises(ProtocolError):
        call(broker, "java.install", {"plan_ref": "a" * 32, "url": "https://example.invalid"})


def test_install_mutation_is_not_replayed_and_installed_selection_expires_on_revoke(installation):
    broker, api, manager, _, package, service = installation
    prepared = call(broker, "java.install_plan", {"package_ref": package})
    request = {"plan_ref": prepared["plan_ref"]}
    receipt = call(broker, "java.install", request, key="install-once")
    result = finish(broker, manager._operations, receipt)
    assert result["state"] == "completed", result
    assert call(broker, "java.install", request, key="install-once") == receipt
    record, check = api.selection(result["result"]["runtime_ref"], monotonic() + 5)
    service._entry("java-test").session.close()
    with pytest.raises(PluginRunError):
        check()
    with pytest.raises(PluginRunError):
        api.selection(result["result"]["runtime_ref"], monotonic() + 5)
    assert record.executable_path.is_file()


def test_unknown_plan_reference_is_rejected(installation):
    broker, _, manager, _, _, _ = installation
    with pytest.raises(ProtocolError):
        call(broker, "java.install", {"plan_ref": "f" * 32})
    assert not manager._plugin_install_owners


def test_target_reservation_blocks_user_and_other_plugin_work(installation):
    _, _, manager, plan, _, _ = installation
    owner = OperationOrigin("a" * 32, "b" * 64, "c" * 32)
    other = OperationOrigin("d" * 32, "e" * 64, "f" * 32)
    token = manager.reserve_plugin_install(plan, owner)
    try:
        with pytest.raises(JavaError, match="插件任务"):
            manager.install(plan.plan_id)
        with pytest.raises(JavaError) as error:
            manager.reserve_plugin_install(plan, other)
        assert error.value.error_code == "PLUGIN_HOST_BUSY"
        manager.release_plugin_install(plan, token, other)
        assert plan.install_path in manager._plugin_install_owners
    finally:
        manager.release_plugin_install(plan, token, owner)


def test_user_task_cannot_be_shared_by_a_plugin(installation, monkeypatch):
    _, _, manager, plan, _, _ = installation
    started, release = Event(), Event()

    def install(*_):
        started.set()
        assert release.wait(5)
        raise JavaError("fixture stopped", "JAVA_INSTALL_CANCELLED")

    monkeypatch.setattr(manager._installer, "install", install)
    manager.install(plan.plan_id)
    assert started.wait(5)
    try:
        with pytest.raises(JavaError) as error:
            manager.reserve_plugin_install(plan, OperationOrigin("a" * 32, "b" * 64, "c" * 32))
        assert error.value.error_code == "PLUGIN_HOST_BUSY"
    finally:
        release.set()
