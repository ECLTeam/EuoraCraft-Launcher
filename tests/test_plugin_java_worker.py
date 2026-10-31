from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from plugin_java_helpers import allow, candidates, make_service

from ECL.game import JavaScanner
from ECL.java.models import JavaPolicy
from ECL.plugins.runtime.executable import ExecutableLaunchSpec


@pytest.mark.skipif(sys.platform != "win32", reason="正式 Worker 仅支持 Windows x64")
@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_actual_java_worker_query_validate_catalog_and_revoke(tmp_path, monkeypatch, is_frozen):
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要实际冻结发行物")
    launch = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if is_frozen
        else None
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"available_releases": [25], "most_recent_lts": 25}
            if request.url.path.endswith("available_releases")
            else [],
        )
    )
    service, manager, records, client, operations = make_service(tmp_path, monkeypatch, transport, launch)
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_java_query"
    source = tmp_path / "example"
    source.mkdir()
    for filename in ("main.py", "plugin.json"):
        (source / filename).write_bytes((example / filename).read_bytes())
    metadata = json.loads((source / "plugin.json").read_bytes())
    metadata["name"] = "java-test"
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    service.register(metadata, source, "directory")
    real_java = os.environ.get("ECL_JAVA_QUERY_EXECUTABLE")
    if real_java:
        runtime = JavaScanner.probe(Path(real_java))
        assert runtime is not None
        records = [JavaPolicy.record(runtime, "manual")]
        with manager.registry.locked():
            manager.registry.write(manager.registry.read().model_copy(update={"runtimes": tuple(records)}))
    try:
        selected = candidates(service)["items"][0]["resource_id"]
        allow(service, [selected])
        before = manager.registry.registry_path.read_bytes()
        service.enable("java-test")
        broker = service.broker("java-test")
        value = json.loads(broker.call_command("inventory", {}, 30)["json"])
        reference = value["items"][0]["runtime_ref"]
        assert len(value["items"]) == 1
        assert records[0].runtime_id not in json.dumps(value)
        if real_java:
            checked = json.loads(broker.call_command("validate", {"runtime_ref": reference}, 30)["json"])
            assert checked["major_version"] == records[0].major_version
        assert json.loads(broker.call_command("catalog", {}, 30)["json"])["items"] == []
        assert manager.registry.registry_path.read_bytes() == before
        service.stop("java-test")
        service.enable("java-test")
        next_value = json.loads(service.broker("java-test").call_command("inventory", {}, 30)["json"])
        assert next_value["items"][0]["runtime_ref"] != reference
        service.stop("java-test")
    finally:
        service.close()
        manager.close()
        operations.close()
        client.close()
