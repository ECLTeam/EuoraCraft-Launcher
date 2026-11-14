from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import httpx
import pytest
from plugin_java_helpers import allow, candidates, invoke, make_service, runtime

from ECL.game import JavaRuntime, JavaScanner
from ECL.java.models import JavaError, JavaPolicy
from ECL.java.packages import JavaCatalog
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_store import PluginRunError
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.models import JavaCatalogRequest, JavaValidateRequest


@pytest.fixture
def shared(tmp_path, monkeypatch):
    fixture = make_service(tmp_path, monkeypatch)
    yield fixture
    service, manager, _, client, operations = fixture
    service.close()
    manager.close()
    operations.close()
    client.close()


def approved(shared, *, use=True):
    service = shared[0]
    page = candidates(service)
    selected = page["items"][0]["resource_id"]
    allow(service, [selected], use=use)
    api = runtime(service)
    reference = invoke(api, "java.inventory")["items"][0]["runtime_ref"]
    return api, reference, selected


def test_inventory_is_read_only_and_does_not_probe_or_scan(shared, monkeypatch):
    service, manager, records, _, _ = shared
    candidates(service)
    before = manager.registry.registry_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("query must not scan, probe, write registry or submit work")

    monkeypatch.setattr(JavaScanner, "probe", forbidden)
    monkeypatch.setattr(JavaScanner, "scan", forbidden)
    monkeypatch.setattr(manager, "inventory", forbidden)
    monkeypatch.setattr(manager.registry, "write", forbidden)
    monkeypatch.setattr(manager._operations, "submit", forbidden)
    api = runtime(service)
    result = invoke(api, "java.inventory")
    assert len(result["items"]) == len(records)
    encoded = json.dumps(result)
    assert str(records[0].executable_path) not in encoded
    assert records[0].runtime_id not in encoded
    assert "executable" not in encoded and "installed_files" not in encoded
    assert before == manager.registry.registry_path.read_bytes()
    listed = invoke(api, "resources.list", capability="java.read")
    assert listed["items"][0]["runtime_ref"] == result["items"][0]["runtime_ref"]


def test_validation_keeps_business_files(shared, monkeypatch):
    api, reference, _ = approved(shared)
    _, manager, records, _, _ = shared
    before = manager.registry.registry_path.read_bytes()
    calls = []

    def probe(path, **kwargs):
        calls.append((path, kwargs))
        kwargs["check"]()
        return JavaRuntime(path, "21.0.3", "Fixture", "amd64", False)

    monkeypatch.setattr(JavaScanner, "probe", probe)
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "private-agent")
    value = invoke(api, "java.validate", runtime_ref=reference, required_major=21)
    assert value["full_version"] == "21.0.3"
    assert calls[0][0] in {record.executable_path for record in records}
    assert "JAVA_TOOL_OPTIONS" not in calls[0][1]["environment"]
    assert before == manager.registry.registry_path.read_bytes()
    with pytest.raises(ProtocolError) as error:
        invoke(api, "java.validate", runtime_ref=reference, required_major=25)
    assert error.value.error_code == "PLUGIN_JAVA_VERSION_MISMATCH"


def test_reference_cannot_cross_run_or_survive_target_replacement(shared):
    api, reference, selected = approved(shared)
    with pytest.raises(ProtocolError):
        invoke(runtime(shared[0]), "java.validate", runtime_ref=reference)
    record = shared[1].registered_snapshot()[0]
    record.executable_path.unlink()
    record.executable_path.write_bytes(b"new executable")
    with pytest.raises(ProtocolError):
        invoke(api, "java.validate", runtime_ref=reference)
    assert selected not in shared[0]._java_catalog.active_refs()
    new_page = candidates(shared[0])
    assert any(item["resource_id"] != selected for item in new_page["items"])
    assert all(item["resource_id"] != selected for item in new_page["items"])


def test_revoke_while_probe_is_running_rejects_late_result_and_retains_activity(shared, monkeypatch):
    api, reference, _ = approved(shared)
    entered, release = Event(), Event()

    def probe(path, **kwargs):
        entered.set()
        assert release.wait(5)
        return JavaRuntime(path, "21.0.2", "Fixture", "amd64", False)

    monkeypatch.setattr(JavaScanner, "probe", probe)
    session = shared[0]._entry("java-test").session
    broker = PluginHostBroker(session, shared[0].data_path, lambda *args: {}, java=api, run_state=lambda: "enabled")
    from ECL.plugins.api._rpc import RpcRequest

    request = RpcRequest("java.validate", {"runtime_ref": reference}, monotonic() + 5)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(broker.handle, request)
        try:
            assert entered.wait(5)
            assert not broker.wait_idle()
            session.close()
            assert not broker.wait_idle()
        finally:
            release.set()
        with pytest.raises(ProtocolError):
            work.result(5)
    assert broker.wait_idle()
    broker.close()


@pytest.mark.parametrize(
    "model,body",
    [
        (JavaValidateRequest, {"runtime_ref": "a" * 32, "required_major": True}),
        (JavaValidateRequest, {"runtime_ref": "a" * 32, "required_major": None}),
        (JavaValidateRequest, {"runtime_ref": "a" * 32, "path": "outside"}),
        (JavaCatalogRequest, {"major_version": None}),
        (JavaCatalogRequest, {"major_version": 0}),
        (JavaCatalogRequest, {"major_version": 100}),
        (JavaCatalogRequest, {"runtime_kind": "jdk"}),
        (JavaCatalogRequest, {"url": "https://outside.test"}),
    ],
)
def test_strict_java_request_models(model, body):
    with pytest.raises(ValueError):
        model.model_validate(body)


def test_catalog_uses_fixed_host_and_never_inherits_client_credentials(shared):
    service, manager, _, client, _ = shared
    calls = []

    def respond(request):
        calls.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.url.host == "api.adoptium.net"
        if request.url.path.endswith("available_releases"):
            return httpx.Response(200, json={"available_releases": [21, 25], "most_recent_lts": 25})
        return httpx.Response(
            200,
            json=[
                {
                    "release_name": "jdk-25.0.1+1",
                    "binary": {
                        "os": JavaPolicy.host()[0],
                        "architecture": JavaPolicy.host()[1],
                        "image_type": "jre",
                        "package": {
                            "link": "https://github.com/adoptium/a/releases/java.zip",
                            "name": "java.zip",
                            "checksum": "a" * 64,
                            "size": 1024,
                        },
                    },
                }
            ],
        )

    manager.catalog._client = httpx.Client(
        transport=httpx.MockTransport(respond), auth=("secret", "secret"), cookies={"secret": "secret"}
    )
    try:
        page = candidates(service)
        allow(service, [page["items"][0]["resource_id"]])
        api = runtime(service)
        result = invoke(api, "java.catalog")
        assert result["recommended_major"] == 25
        assert len(calls) == 2 and "/25/" in calls[1].url.path
        assert calls[1].url.params["image_type"] == "jre"
        assert "download_url" not in json.dumps(result)
        assert result["items"][0]["package_ref"] != "a" * 24
        assert invoke(api, "java.catalog")["items"] == result["items"]
        assert len(calls) == 2
    finally:
        manager.catalog._client.close()
        manager.catalog._client = client


def test_corrupt_index_is_preserved_and_does_not_remain_authorized(shared):
    api, _, _ = approved(shared)
    index = shared[0].data_path / "plugins" / "java_resources.json"
    index.write_bytes(b"broken")
    with pytest.raises(ProtocolError) as error:
        invoke(api, "java.inventory")
    assert error.value.error_code == "PLUGIN_RESOURCE_CATALOG_INVALID"
    assert index.read_bytes() == b"broken"


def test_catalog_errors_do_not_turn_into_empty_results(shared):
    service = shared[0]
    page = candidates(service)
    allow(service, [page["items"][0]["resource_id"]])
    with pytest.raises(ProtocolError) as error:
        invoke(runtime(service), "java.catalog")
    assert error.value.error_code == "PLUGIN_JAVA_CATALOG_FAILED"


@pytest.mark.parametrize("method", ["java.delete", "java.unknown"])
def test_unimplemented_java_methods_cannot_return_inventory_success(shared, method):
    with pytest.raises(ProtocolError) as error:
        invoke(runtime(shared[0]), method)
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"


def test_catalog_requests_share_deadline_instead_of_resetting_each_request(monkeypatch):
    from ECL.java import packages as package_module

    clock = [100.0]
    requests = []
    monkeypatch.setattr(package_module, "monotonic", lambda: clock[0])

    def respond(request):
        requests.append(request)
        clock[0] += 4
        return httpx.Response(
            200, json={"available_releases": [25], "most_recent_lts": 25} if len(requests) == 1 else []
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        catalog = JavaCatalog(client)
        catalog.releases(deadline=105)
        with pytest.raises(JavaError) as error:
            catalog.packages(25, "JRE", deadline=105)
    assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
    assert len(requests) == 2
    assert requests[1].extensions["timeout"]["read"] == 1


def test_catalog_revocation_error_is_not_rewritten_as_source_failure(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"available_releases": [25], "most_recent_lts": 25})

    def check():
        if calls:
            raise PluginRunError("revoked", "PLUGIN_RUN_EXPIRED")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client, pytest.raises(PluginRunError):
        JavaCatalog(client).releases(deadline=monotonic() + 5, check=check)


def test_inventory_cursor_cannot_change_page_limit(shared):
    service = shared[0]
    page = candidates(service)
    allow(service, [item["resource_id"] for item in page["items"]])
    api = runtime(service)
    first = invoke(api, "java.inventory", limit=1)
    assert first["next_cursor"]
    with pytest.raises(ProtocolError) as error:
        invoke(api, "java.inventory", limit=2, cursor=first["next_cursor"])
    assert error.value.error_code == "PLUGIN_CURSOR_INVALID"
