from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest

from ECL.foundation import EventBus
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager


@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_snapshot_example_uses_real_worker_upload_commit_read_and_restart(tmp_path, is_frozen):
    if sys.platform != "win32":
        pytest.skip("第三方执行仅支持 Windows x64")
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要本轮真实冻结发行物")
    executable = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if is_frozen
        else None
    )
    code_path = Path(__file__).resolve().parents[1] / "examples/plugins/worker_snapshot"
    metadata = json.loads((code_path / "plugin.json").read_bytes())
    manager = OperationManager(tmp_path, EventBus())
    service = PluginWorkerService(tmp_path, executable, operations=manager)
    try:
        service.register(metadata, code_path, "directory")
        permissions = service.permissions("worker_snapshot")
        service.update_permissions(
            "worker_snapshot",
            permissions["content_digest"],
            permissions["revision"],
            tuple(PermissionDecision(capability=item["capability"], decision="allow") for item in permissions["items"]),
            PermissionManagementCaller.main_webview,
        )
        service.enable("worker_snapshot")
        broker = service.broker("worker_snapshot")
        receipt = broker.call_command("export", {})
        deadline = monotonic() + 10
        while True:
            status = broker.call_command("status", {"operation_ref": receipt["operation_ref"]})
            if status["state"] in {"completed", "failed", "cancelled"}:
                break
            assert monotonic() < deadline
            sleep(0.02)
        assert status == {"state": "completed", "effect": "committed"}
        target = {"root_ref": receipt["root_ref"], "relative_path": receipt["name"]}
        view = broker.handle(RpcRequest("files.stat", {"target": target}, monotonic() + 5))
        assert view["size"] > 0
        installation_id = broker.session.view().installation_id
        exported = tmp_path / "plugin_data" / installation_id / "files" / receipt["name"]
        assert json.loads(exported.read_bytes())["api_version"] == 2
        service.stop("worker_snapshot")
        service.enable("worker_snapshot")
        broker = service.broker("worker_snapshot")
        roots = broker.handle(RpcRequest("resources.list", {"capability": "plugin.base"}, monotonic() + 5))
        assert roots["items"][0]["root_ref"] != receipt["root_ref"]
        assert exported.exists()
    finally:
        service.close()
        manager.close()
