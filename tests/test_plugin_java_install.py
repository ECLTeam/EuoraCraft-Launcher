from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import httpx
import pytest
from plugin_java_helpers import allow, candidates, make_service, runtime
from test_java_manager import _archive_bytes, _plan

from ECL.game import JavaRuntime, JavaScanner
from ECL.java.models import JavaError
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.grant_store import PluginGrantError
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
    view = service.permissions("java-test")
    service.update_permissions(
        "java-test",
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="java.install", decision="deny"),),
        PermissionManagementCaller.main_webview,
    )
    with pytest.raises(PluginGrantError):
        check()
    with pytest.raises(PluginGrantError):
        api.selection(result["result"]["runtime_ref"], monotonic() + 5)
    assert record.executable_path.is_file()


def test_regrant_cannot_revive_an_old_plan_or_catalog_reference(installation):
    broker, _, manager, _, package, service = installation
    prepared = call(broker, "java.install_plan", {"package_ref": package})
    view = service.permissions("java-test")
    service.update_permissions(
        "java-test",
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="java.install", decision="deny"),),
        PermissionManagementCaller.main_webview,
    )
    selected = candidates(service)["items"][0]["resource_id"]
    allow(service, [selected], use=False, install=True)
    with pytest.raises(ProtocolError):
        call(broker, "java.install", {"plan_ref": prepared["plan_ref"]})
    with pytest.raises(ProtocolError):
        call(broker, "java.install_plan", {"package_ref": package})
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


@pytest.mark.skipif(sys.platform != "win32", reason="实际第三方 Worker 当前要求 Windows x64")
def test_actual_worker_example_prepares_installs_and_receives_owned_runtime(installation):
    _, _, manager, _, _, service = installation
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_java_query"
    metadata = json.loads((example / "plugin.json").read_bytes())
    metadata["name"] = "java-test"
    service.register(metadata, example, "directory")
    selected = candidates(service)["items"][0]["resource_id"]
    allow(service, [selected], use=False, install=True)
    service.enable("java-test")
    broker = service.broker("java-test")
    try:

        def command(name, params=None):
            return json.loads(broker.call_command(name, params or {}, 30)["json"])

        package = command("catalog")["items"][0]["package_ref"]
        plan = command("prepare_install", {"package_ref": package, "mutation_key": "worker-plan"})
        receipt = command("install", {"plan_ref": plan["plan_ref"], "mutation_key": "worker-install"})
        deadline = monotonic() + 10
        while True:
            result = command("status", {"operation_ref": receipt["operation_ref"]})
            if result["state"] in {"completed", "failed", "cancelled"} and not result["cleanup_pending"]:
                assert result["state"] == "completed", result
                break
            assert monotonic() < deadline
            sleep(0.02)
        assert command("install", {"plan_ref": plan["plan_ref"], "mutation_key": "worker-install"}) == receipt
        record, check = broker.java.selection(result["result"]["runtime_ref"], monotonic() + 5)
        check()
        assert record.executable_path.is_file() and record in manager.registered_snapshot()
    finally:
        service.stop("java-test")
