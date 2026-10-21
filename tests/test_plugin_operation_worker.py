from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from ECL.foundation import EventBus
from ECL.plugin_api._windows import WindowsRuntimeError
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager


@pytest.fixture(params=[False, True], ids=["source", "frozen"])
def worker(tmp_path, request):
    if sys.platform != "win32":
        pytest.skip("第三方执行仅支持 Windows x64")
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if request.param and not artifact:
        pytest.skip("真实冻结验收需要显式提供本轮发行物")
    executable = (
        ExecutableLaunchSpec(Path(artifact).resolve(), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if request.param
        else None
    )
    root = tmp_path / "code"
    root.mkdir()
    metadata = {
        "name": "task_control",
        "version": "1.0.0",
        "api_version": 2,
        "permissions": [
            {"capability": "commands.register", "required": True},
            {"capability": "commands.execute", "required": True},
            {"capability": "settings.write", "required": False},
        ],
    }
    (root / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "main.py").write_text(
        "from ECL.plugin_api import Plugin as BasePlugin, HostApiError\n"
        "class Plugin(BasePlugin):\n"
        "    def on_load(self):\n"
        "        self.define_command('task',self.task,input_fields=({'name':'mode','type':'string'},"
        "{'name':'reference','type':'string'}),output_fields=({'name':'state','type':'string'},"
        "{'name':'requested','type':'bool'}))\n"
        "    def task(self,mode,reference):\n"
        "        try:\n"
        "            if mode == 'list':\n"
        "                return {'state':str(len(self.host.call('operations.list')['items'])),'requested':False}\n"
        "            result=self.host.call('operations.'+mode,{'operation_ref':reference})\n"
        "            return {'state':result['state'],'requested':result.get('requested',False)}\n"
        "        except HostApiError as error:\n"
        "            return {'state':error.error_code,'requested':False}\n",
        encoding="utf-8",
    )
    manager = OperationManager(tmp_path, EventBus())
    service = PluginWorkerService(tmp_path, executable, operations=manager)
    service.register(metadata, root, "directory")
    view = service.permissions("task_control")
    service.update_permissions(
        "task_control",
        view["content_digest"],
        view["revision"],
        tuple(PermissionDecision(capability=item["capability"], decision="allow") for item in view["items"]),
        PermissionManagementCaller.main_webview,
    )
    service.enable("task_control")
    yield service, manager
    service.close()
    manager.close()
    for operation in tuple(manager._operations.values()):
        if operation.future and not operation.future.cancelled():
            operation.future.result(5)


def control(service, mode, reference=""):
    return service.broker("task_control").call_command("task", {"mode": mode, "reference": reference})


def test_real_worker_queries_and_cancels_only_owned_task(worker):
    service, manager = worker
    adapter = service.broker("task_control").operations
    entered, release = Event(), Event()

    def work(context):
        entered.set()
        assert release.wait(8)
        context.check_cancelled()

    reference = adapter.submit("host_fixture", work, service._entry("task_control").session.authorize("settings.write"))
    operation_id = adapter._references[reference].operation_id
    try:
        assert entered.wait(3)
        assert control(service, "list")["state"] == "1"
        assert control(service, "get", reference)["state"] == "running"
        assert control(service, "get", operation_id)["state"] == "PLUGIN_RESOURCE_NOT_FOUND"
        assert control(service, "cancel", reference)["requested"] is True
        assert not service.broker("task_control").wait_idle()
    finally:
        release.set()
    manager._operations[operation_id].future.result(5)
    assert control(service, "get", reference)["state"] == "cancelled"
    service.stop("task_control")
    service.enable("task_control")
    assert control(service, "get", reference)["state"] == "PLUGIN_RESOURCE_NOT_FOUND"
    assert control(service, "list")["state"] == "0"


def test_real_worker_stop_waits_for_host_commit_and_rejects_early_enable(worker):
    service, manager = worker
    adapter = service.broker("task_control").operations
    entered, release = Event(), Event()
    actions = []

    def work(context):
        def commit():
            actions.append(1)
            entered.set()
            assert release.wait(12)

        return context.finalize(commit)

    reference = adapter.submit("host_fixture", work, service._entry("task_control").session.authorize("settings.write"))
    operation_id = adapter._references[reference].operation_id
    try:
        assert entered.wait(3)
        assert control(service, "cancel", reference)["state"] == "PLUGIN_COMMIT_STARTED"
        view = service.permissions("task_control")
        service.update_permissions(
            "task_control",
            view["content_digest"],
            view["revision"],
            (PermissionDecision(capability="settings.write", decision="deny"),),
            PermissionManagementCaller.main_webview,
        )
        assert control(service, "get", reference)["state"] == "committing"
        with pytest.raises(WindowsRuntimeError) as error:
            service.stop("task_control")
        assert error.value.error_code == "PLUGIN_HOST_CLEANUP_PENDING"
        assert service.runtime_info("task_control")["cleanup_state"] == "pending"
        with pytest.raises(ProtocolError) as error:
            service.enable("task_control")
        assert error.value.error_code == "PLUGIN_HOST_CLEANUP_PENDING"
        assert not manager._operations[operation_id].future.done()
    finally:
        release.set()
    manager._operations[operation_id].future.result(5)
    assert actions == [1]
    deadline = monotonic() + 8
    while service.runtime_info("task_control")["cleanup_state"] != "completed" and monotonic() < deadline:
        sleep(0.05)
    assert service.runtime_info("task_control")["cleanup_state"] == "completed"
    assert manager._operations.get(operation_id) is None
