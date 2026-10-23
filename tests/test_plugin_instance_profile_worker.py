from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Event, RLock
from time import monotonic, sleep

import pytest

from ECL.foundation import EventBus
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.game import GameService
from ECL.services.game.instance_profiles import InstanceProfileStore
from ECL.services.game.version_stats import VersionStatsStore
from ECL.services.operations import OperationManager


@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_actual_profile_worker_updates_and_cancels_without_automatic_writes(tmp_path, is_frozen):
    if sys.platform != "win32":
        pytest.skip("第三方Worker只支持Windows x64")
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要最终冻结发行物")
    root = tmp_path / "game"
    instance = root / "versions/one"
    instance.mkdir(parents=True)
    (instance / "one.json").write_text('{"id":"one"}', encoding="utf-8")
    data_path = tmp_path / "data"
    store = InstanceProfileStore(data_path, VersionStatsStore())
    game = GameService.__new__(GameService)
    game._lock = RLock()
    game._running_games = {}
    game._instance_profiles = store
    launch = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if is_frozen
        else None
    )
    manager = OperationManager(data_path, EventBus(), max_workers=1)
    service = PluginWorkerService(
        data_path,
        launch,
        instance_queries=game,
        instance_profiles=store.transactions,
        instance_roots=lambda: (root,),
        operations=manager,
    )
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_instance_profile"
    metadata = json.loads((example / "plugin.json").read_bytes())
    source = tmp_path / "source"
    source.mkdir()
    for name in ("main.py", "plugin.json"):
        (source / name).write_bytes((example / name).read_bytes())
    gate = Event()
    try:
        service.register(metadata, source, "directory")
        review = service.permissions(metadata["name"])
        roots = service.permission_resources(metadata["name"], review["content_digest"], None, "", None, 50)
        root_id = roots["items"][0]["resource_id"]
        children = service.permission_resources(
            metadata["name"], review["content_digest"], root_id, "", None, 50, "instances.write"
        )
        selected = children["items"][0]["resource_id"]
        service.update_permissions(
            metadata["name"],
            review["content_digest"],
            review["revision"],
            tuple(
                PermissionDecision(
                    capability=item["capability"],
                    decision="allow",
                    resource_refs=(root_id,)
                    if item["capability"] == "instances.read"
                    else (selected,)
                    if item["capability"] == "instances.write"
                    else (),
                )
                for item in review["items"]
            ),
            PermissionManagementCaller.main_webview,
            children["catalog_revision"],
        )
        service.enable(metadata["name"])
        broker = service.broker(metadata["name"])
        resources = json.loads(broker.call_command("discover", {})["json"])
        targets = json.loads(broker.call_command("scan", {"root_ref": resources["items"][0]["root_ref"]})["json"])
        before = targets["items"][0]
        params = {
            "instance_ref": before["instance_ref"],
            "expected_profile_etag": before["profile_etag"],
            "mutation_key": "worker-profile",
            "alias": "Worker name",
            "favorite": False,
        }

        def wait(receipt):
            end = monotonic() + 15
            while True:
                view = json.loads(broker.call_command("status", {"operation_ref": receipt["operation_ref"]})["json"])
                if view["state"] in {"completed", "failed", "cancelled"} and not view["cleanup_pending"]:
                    return view
                assert monotonic() < end
                sleep(0.025)

        result = wait(broker.call_command("update", params))
        assert result["state"] == "completed" and result["effect"] == "committed", result
        profile_path = instance / ".ecl/instance.json"
        original = profile_path.read_bytes()
        assert json.loads(original)["alias"] == "Worker name"
        params.update(
            expected_profile_etag=result["result"]["profile_etag"], alias="Cancelled", mutation_key="cancel-profile"
        )
        blocker = manager._executor.submit(gate.wait, 5)
        receipt = broker.call_command("update", params)
        assert broker.call_command("cancel", {"operation_ref": receipt["operation_ref"]})["requested"]
        gate.set()
        blocker.result(5)
        assert wait(receipt)["state"] == "cancelled"
        service.stop(metadata["name"])
        service.enable(metadata["name"])
        assert profile_path.read_bytes() == original
    finally:
        gate.set()
        service.close()
        manager.close()
        assert store.transactions.close()
