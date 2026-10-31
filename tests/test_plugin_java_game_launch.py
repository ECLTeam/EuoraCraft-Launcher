from __future__ import annotations

import json
import sys
from threading import Event

import httpx
import pytest

from ECL.foundation import EventBus
from ECL.game import JavaRuntime
from ECL.java.manager import JavaManager
from ECL.java.models import JavaPolicy
from ECL.java.queries import JavaQueries
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.java_resources import PluginJavaResourceCatalog
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.java_api import RuntimeJava
from ECL.plugins.runtime.resources import RuntimeResources
from tests.test_plugin_game_launch import game_launch  # noqa: F401 - pytest fixture
from tests.test_plugin_host_files import call, finish

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="插件游戏启动当前要求 Windows x64")


@pytest.fixture
def selected_java(request):
    state = request.getfixturevalue("game_launch")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    manager = JavaManager(state.worker.data_path, client, state.manager, EventBus())
    executable = state.root.parent / "selected-jdk/bin/java.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"selected fixture Java")
    record = JavaPolicy.record(JavaRuntime(executable, "21.0.2", "Selected", "amd64", False), "manual")
    with manager.registry.locked():
        manager.registry.write(manager.registry.read().model_copy(update={"runtimes": (record,)}))
    queries = JavaQueries(manager)
    worker = state.worker
    worker._java_queries, worker._java_manager = queries, manager
    worker._java_catalog = PluginJavaResourceCatalog(worker.data_path, queries)
    source = state.root.parent / "selected-java-plugin"
    source.mkdir()
    metadata = {
        "name": "selected-java-plugin",
        "api_version": 2,
        "permissions": [{"capability": name} for name in ("instances.manage", "java.read", "java.use")],
    }
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("value=1", encoding="utf-8")
    worker.register(metadata, source, "directory")
    view = worker.permissions(metadata["name"])
    games = worker.permission_resources(
        metadata["name"], view["content_digest"], None, "", None, 50, "instances.manage"
    )
    runtimes = worker.permission_resources(metadata["name"], view["content_digest"], None, "", None, 50, "java.read")
    java_id = runtimes["items"][0]["resource_id"]
    worker.update_permissions(
        metadata["name"],
        view["content_digest"],
        view["revision"],
        tuple(
            PermissionDecision(
                capability=name,
                decision="allow",
                resource_refs=(state.root_id if name == "instances.manage" else java_id,),
            )
            for name in ("instances.manage", "java.read", "java.use")
        ),
        PermissionManagementCaller.main_webview,
        games["catalog_revision"],
        runtimes["catalog_revision"],
    )
    session = worker._entry(metadata["name"]).session
    broker = PluginHostBroker(
        session,
        worker.data_path,
        lambda *_: {},
        run_state=lambda: "enabled",
        operations=state.manager,
        game_operations=state.game,
        resources=RuntimeResources(session, worker._catalog, state.game, worker.refresh_resources),
        java=RuntimeJava(session, queries, worker._java_catalog, worker.refresh_java_resources, manager),
    )
    root = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
    instance = next(
        item["instance_ref"]
        for item in call(broker, "instances.scan", {"root_ref": root})["items"]
        if item["version_id"] == "one"
    )
    runtime = call(broker, "java.inventory")["items"][0]["runtime_ref"]
    try:
        yield state, broker, executable, instance, runtime
    finally:
        broker.close()
        manager.close()
        client.close()


def test_selected_java_reaches_existing_launch_service_and_creates_owned_process(selected_java, monkeypatch):
    state, broker, executable, instance, runtime = selected_java
    received = []
    original = state.game.launch_instance

    async def launch(*args, **kwargs):
        received.append(kwargs["java_path"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(state.game, "launch_instance", launch)
    receipt = call(broker, "game.launch", {"instance_ref": instance, "options": {"runtime_ref": runtime}})
    result = finish(broker, state.manager, receipt)
    assert result["state"] == "completed", result
    assert received == [executable]
    stopped = finish(
        broker, state.manager, call(broker, "game.stop", {"game_run_ref": result["result"]["game_run_ref"]})
    )
    assert stopped["state"] == "completed" and not stopped["result"]["running"]


def test_selected_java_replacement_during_preparation_prevents_process_creation(selected_java, monkeypatch):
    state, broker, executable, instance, runtime = selected_java
    entered, release = Event(), Event()
    original = state.game._command_builder

    def blocked(config):
        entered.set()
        assert release.wait(5)
        return original(config)

    monkeypatch.setattr(state.game, "_command_builder", blocked)
    receipt = call(broker, "game.launch", {"instance_ref": instance, "options": {"runtime_ref": runtime}})
    assert entered.wait(3)
    try:
        executable.write_bytes(b"replacement")
    finally:
        release.set()
    result = finish(broker, state.manager, receipt)
    assert result["state"] in {"failed", "cancelled"}, result
    assert not state.game.instances.get_instances_info()
