from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

from ECL.foundation import EventBus
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager
from tests.test_plugin_file_delete import published
from tests.test_plugin_host_files import root


@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_real_worker_delete_version_cancel_and_no_automatic_cleanup(tmp_path, is_frozen):
    if sys.platform != "win32":
        pytest.skip("第三方 Worker 仅支持 Windows x64")
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要本轮最终冻结发行物")
    executable = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if is_frozen
        else None
    )
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_file_cleanup"
    metadata = json.loads((example / "plugin.json").read_bytes())
    metadata["permissions"] += [{"capability": c} for c in ()]
    code_path = tmp_path / "code"
    code_path.mkdir()
    (code_path / "main.py").write_bytes((example / "main.py").read_bytes())
    (code_path / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    manager = OperationManager(tmp_path, EventBus(), max_workers=1)
    service = PluginWorkerService(tmp_path, executable, operations=manager)
    gate = Event()

    def wait(receipt):
        deadline = monotonic() + 10
        while True:
            view = broker.call_command("status", {"operation_ref": receipt["operation_ref"]})
            if view["state"] in {"completed", "failed", "cancelled"} and not view["cleanup_pending"]:
                return view
            assert monotonic() < deadline
            sleep(0.02)

    try:
        service.register(metadata, code_path, "directory")
        view = service.permissions(metadata["name"])
        service.update_permissions(
            metadata["name"],
            view["content_digest"],
            view["revision"],
            tuple(PermissionDecision(capability=item["capability"], decision="allow") for item in view["items"]),
            PermissionManagementCaller.main_webview,
        )
        service.enable(metadata["name"])
        broker = service.broker(metadata["name"])
        target, etag = published(broker, manager)
        params = {
            "root_ref": target["root_ref"],
            "name": target["relative_path"],
            "expected_etag": etag,
            "mutation_key": "selected-delete",
        }
        receipt = broker.call_command("delete", params)
        assert wait(receipt) == {
            "state": "completed",
            "effect": "committed",
            "cleanup_pending": False,
            "deleted": True,
            "error_code": "",
        }
        assert broker.call_command("delete", params) == receipt
        target, etag = published(broker, manager)
        blocker = manager._executor.submit(gate.wait, 10)
        try:
            params.update(expected_etag=etag, mutation_key="cancel-delete")
            receipt = broker.call_command("delete", params)
            assert broker.call_command("cancel", {"operation_ref": receipt["operation_ref"]}) == {"requested": True}
        finally:
            gate.set()
            blocker.result(10)
        assert wait(receipt)["state"] == "cancelled"
        saved = next(tmp_path.glob("plugin_data/*/files/target.bin"))
        service.stop(metadata["name"])
        service.enable(metadata["name"])
        broker = service.broker(metadata["name"])
        assert saved.read_bytes() == b"hello"
        assert root(broker, "plugin.base") != target["root_ref"]
    finally:
        gate.set()
        service.close()
        manager.close()
