from __future__ import annotations

import json
import sys

import httpx

from ECL.foundation import EventBus
from ECL.game import JavaRuntime
from ECL.java.manager import JavaManager
from ECL.java.models import JavaPolicy
from ECL.java.queries import JavaQueries
from ECL.plugins.runtime.java_api import RuntimeJava
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager


def make_service(tmp_path, monkeypatch, transport=None, launch_spec=None, install=False):
    events = EventBus()
    operations = OperationManager(events)
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
    queries = JavaQueries(manager)
    service = PluginWorkerService(
        tmp_path / "data", launch_spec, java_queries=queries, java_manager=manager, operations=operations
    )
    source = tmp_path / "plugin"
    source.mkdir()
    metadata = {"name": "java-test", "api_version": 2}
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("from ECL.plugin_api import Plugin\n", encoding="utf-8")
    service.register(metadata, source, "directory")
    return service, manager, records, client, operations


def candidates(service):
    """返回宿主目录中全部活动 Java 资源身份；权限决定流程已移除。"""
    service.refresh_java_resources(discover=True)
    return {
        "items": [{"resource_id": item.resource_id} for item in service._java_catalog.resources() if item.is_active]
    }


def allow(service, ids, *, use=True, catalog=True, revision=None, install=False):
    """权限决定已移除；保留签名使调用点无需变动。"""
    return 0


def runtime(service):
    return RuntimeJava(
        service._entry("java-test").session,
        service._java_queries,
        service._java_catalog,
        service.refresh_java_resources,
        service._java_manager,
    )


def invoke(api, method, **params):
    from time import monotonic

    return api.handle(method, params, monotonic() + 5)
