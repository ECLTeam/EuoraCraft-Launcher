from __future__ import annotations

import json
import sys
from time import monotonic

import httpx

from ECL.foundation import EventBus
from ECL.game import JavaRuntime
from ECL.java.manager import JavaManager
from ECL.java.models import JavaPolicy
from ECL.java.queries import JavaQueries
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.java_api import RuntimeJava
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager


def make_service(tmp_path, monkeypatch, transport=None, launch_spec=None, install=False):
    events = EventBus()
    operations = OperationManager(tmp_path, events)
    client = httpx.Client(transport=transport or httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    manager = JavaManager(tmp_path, client, operations, events)
    records = []
    for index in range(2):
        home = tmp_path / f"jdk-{index}"
        (home / "bin").mkdir(parents=True)
        executable = home / "bin" / ("java.exe" if sys.platform == "win32" else "java")
        executable.write_bytes(b"fixture executable")
        (home / "release").write_text("JAVA_VERSION=21.0.2", encoding="utf-8")
        record = JavaPolicy.record(JavaRuntime(executable, "21.0.2", "Fixture", "amd64", False), "manual")
        records.append(record)
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": tuple(records)}))
    capabilities = {"java.read", "java.use", "java.catalog.read"}
    if install:
        capabilities.add("java.install")
    queries = JavaQueries(manager)
    service = PluginWorkerService(
        tmp_path / "data", launch_spec, java_queries=queries, java_manager=manager, operations=operations
    )
    source = tmp_path / "plugin"
    source.mkdir()
    metadata = {
        "name": "java-test",
        "api_version": 2,
        "permissions": [{"capability": name, "required": False} for name in sorted(capabilities)],
    }
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("from ECL.plugin_api import Plugin\n", encoding="utf-8")
    service.register(metadata, source, "directory")
    return service, manager, records, client, operations


def candidates(service, capability="java.read"):
    view = service.permissions("java-test")
    return service.permission_resources("java-test", view["content_digest"], None, "", None, 100, capability)


def allow(service, ids, *, use=True, catalog=True, revision=None, install=False):
    view = service.permissions("java-test")
    decisions = [PermissionDecision(capability="java.read", decision="allow", resource_refs=tuple(ids))]
    if use:
        decisions.append(PermissionDecision(capability="java.use", decision="allow", resource_refs=tuple(ids)))
    if catalog:
        decisions.append(PermissionDecision(capability="java.catalog.read", decision="allow"))
    if install:
        decisions.append(PermissionDecision(capability="java.install", decision="allow"))
    return service.update_permissions(
        "java-test",
        view["content_digest"],
        view["revision"],
        tuple(decisions),
        PermissionManagementCaller.main_webview,
        java_resource_revision=revision if revision is not None else service.refresh_java_resources(),
    )


def runtime(service):
    return RuntimeJava(
        service._entry("java-test").session,
        service._java_queries,
        service._java_catalog,
        service.refresh_java_resources,
        service._java_manager,
    )


def invoke(api, method, **params):
    return api.handle(method, params, monotonic() + 5)
