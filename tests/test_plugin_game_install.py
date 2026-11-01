from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ECL.foundation import EventBus
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.game_models import GameInstallRequest, GameLaunchOptions
from ECL.plugins.runtime.resources import RuntimeResources
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.game import GameService, GameServiceError
from ECL.services.operations import OperationManager
from tests.test_plugin_host_files import call, finish


@pytest.fixture
def game_install(tmp_path, monkeypatch):
    root = tmp_path / "game"
    root.mkdir()
    events = EventBus()
    manager = OperationManager(tmp_path / "data", events, max_workers=2)
    game = GameService(SimpleNamespace(), data_path=tmp_path / "data", operations=manager, enable_version_watcher=False)
    game.minecraft_versions_classified = lambda source: {"release": [{"id": "1.20.1", "releaseTime": "2023-01-01"}]}
    game.loader_versions = lambda *_: ["0.16.14"]
    writes = []

    def build(version_id, name, *, observer):
        observer.emit("check_resources")
        target = root / "versions" / name
        (target / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
        writes.append(target)
        return []

    builders = SimpleNamespace(build_minecraft_download_list=build)
    monkeypatch.setattr(game, "_context", lambda *_: SimpleNamespace(games=builders))
    worker = PluginWorkerService(
        tmp_path / "data",
        instance_queries=game,
        instance_roots=lambda: (root,),
        operations=manager,
        game_operations=game,
    )
    source = tmp_path / "source"
    source.mkdir()
    metadata = {
        "name": "game-test",
        "api_version": 2,
        "permissions": [{"capability": cap} for cap in ["instances.manage", "instances.read"]],
    }
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("value=1", encoding="utf-8")
    worker.register(metadata, source, "directory")
    view = worker.permissions("game-test")
    page = worker.permission_resources("game-test", view["content_digest"], None, "", None, 100, "instances.manage")
    root_id = page["items"][0]["resource_id"]
    worker.update_permissions(
        "game-test",
        view["content_digest"],
        view["revision"],
        tuple(
            PermissionDecision(capability=cap, decision="allow", resource_refs=(root_id,))
            for cap in ["instances.manage", "instances.read"]
        ),
        PermissionManagementCaller.main_webview,
        page["catalog_revision"],
    )
    entry = worker._entry("game-test")
    resources = RuntimeResources(entry.session, worker._catalog, game, worker.refresh_resources)
    broker = PluginHostBroker(
        entry.session,
        tmp_path / "data",
        lambda *_: {},
        run_state=lambda: "enabled",
        operations=manager,
        resources=resources,
        game_operations=game,
    )
    ref = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
    yield SimpleNamespace(
        game=game,
        worker=worker,
        broker=broker,
        manager=manager,
        root=root,
        ref=ref,
        builders=builders,
        writes=writes,
        events=events,
    )
    broker.close()
    manager.close()
    for operation in tuple(manager._operations.values()):
        if operation.future and not operation.future.cancelled():
            operation.future.result(10)
    assert broker.wait_idle(3)
    worker.close()
    game.close()


def install(state, **options):
    return call(state.broker, "game.install", {"root_ref": state.ref, "version_id": "1.20.1", **options})


@pytest.mark.parametrize("value", ["../outside", "C:escape", "CON.txt", "x.", "x ", "a/b", "a\\b", "a\0b"])
def test_plugin_names_reject_paths_ads_devices_and_normalization(value):
    with pytest.raises(ValidationError):
        GameInstallRequest(root_ref="a" * 32, version_id="1.20.1", version_name=value)


@pytest.mark.parametrize("options", [{"memory_mb": True}, {"jvm_args": []}, {"runtime_ref": "not-a-ref"}, {"env": {}}])
def test_plugin_launch_options_are_explicit_and_do_not_inherit_unimplemented_java_contract(options):
    with pytest.raises(ValidationError):
        GameLaunchOptions.model_validate(options)


def test_install_scope_is_root_only_and_declarations_cannot_supply_host_refs(game_install):
    from ECL.plugins.grant_models import PermissionDeclaration

    state = game_install
    with pytest.raises(ValidationError):
        PermissionDeclaration(capability="instances.manage", resource_refs=("a" * 32,))
    page = call(state.broker, "permissions.list")
    assert next(item for item in page["items"] if item["capability"] == "instances.manage")["resource_refs"] == [
        state.ref
    ]
    with pytest.raises(ProtocolError) as error:
        call(state.broker, "game.install", {"root_ref": "f" * 32, "version_id": "1.20.1"})
    assert error.value.error_code == "PLUGIN_RESOURCE_OUT_OF_SCOPE"


def test_shared_install_reservation_prevents_duplicate_target_and_release_is_idempotent(game_install):
    state = game_install
    reservation = state.game.reserve_install_target(state.root, "one")
    with pytest.raises(GameServiceError) as error:
        state.game.reserve_install_target(state.root, "one")
    assert error.value.error_code == "INSTALL_ALREADY_RUNNING"
    assert state.game.release_install_target(reservation)
    replacement = state.game.reserve_install_target(state.root, "one")
    assert state.game.release_install_target(reservation)
    assert replacement in state.game._install_reservations.values()
    assert state.game.release_install_target(replacement)


def test_user_install_cancelled_before_coroutine_start_releases_shared_target(game_install):
    async def exercise():
        state = game_install
        receipt = state.game.install_version({"version_id": "1.20.1"}, game_path=state.root)
        task = state.game._install_tasks[receipt["taskId"]]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert not state.game._install_reservations
        assert not state.game._install_tasks
        assert not state.writes

    asyncio.run(exercise())


def test_install_rejects_unlisted_explicit_loader_before_writing(game_install):
    result = finish(
        game_install.broker, game_install.manager, install(game_install, loader="fabric", loader_version="x")
    )
    assert result["state"] == "failed" and result["error_code"] == "PLUGIN_LOADER_VERSION_INVALID"
    assert result["effect"] == "none"
    assert not (game_install.root / "versions/1.20.1").exists()


@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_actual_worker_catalog_install_result_and_duplicate_submission(game_install, is_frozen):
    state = game_install
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要本轮真实冻结产物")
    if is_frozen:
        state.worker._executable = ExecutableLaunchSpec(
            Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "pyinstaller")
        )
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_game_install"
    metadata = json.loads((example / "plugin.json").read_bytes())
    state.worker.register(metadata, example, "directory")
    view = state.worker.permissions(metadata["name"])
    page = state.worker.permission_resources(
        metadata["name"], view["content_digest"], None, "", None, 50, "instances.manage"
    )
    root_id = page["items"][0]["resource_id"]
    state.worker.update_permissions(
        metadata["name"],
        view["content_digest"],
        view["revision"],
        tuple(
            PermissionDecision(
                capability=item["capability"],
                decision="allow",
                resource_refs=(root_id,) if item["capability"] in {"instances.manage", "instances.read"} else (),
            )
            for item in view["items"]
        ),
        PermissionManagementCaller.main_webview,
        page["catalog_revision"],
    )
    state.worker.enable(metadata["name"])
    broker = state.worker.broker(metadata["name"])

    def command(name, params):
        return json.loads(broker.call_command(name, params)["json"])

    assert not state.writes
    assert command("catalog", {})["items"][0]["version_id"] == "1.20.1"
    roots = command("discover", {"capability": "instances.manage"})
    params = {
        "root_ref": roots["items"][0]["root_ref"],
        "version_id": "1.20.1",
        "version_name": "worker-installed",
        "loader": "vanilla",
        "mutation_key": "real-worker-install",
    }
    receipt = command("install", params)
    assert command("install", params) == receipt
    deadline = monotonic() + 15
    while True:
        result = command("status", {"operation_ref": receipt["operation_ref"]})
        if result["state"] in {"completed", "failed", "cancelled"} and not result["cleanup_pending"]:
            break
        assert monotonic() < deadline
        sleep(0.025)
    assert result["state"] == "completed" and result["effect"] == "committed", result
    assert result["result"]["version_id"] == "worker-installed"
    assert len(state.writes) == 1
    state.worker.stop(metadata["name"])


def test_install_reservation_holds_busy_target_while_handle_close_is_pending(game_install):
    from ECL.utils.file_handles import FileBoundaryError

    reservation = game_install.game.reserve_install_target(game_install.root, "pending")

    class Handle:
        blocked = True

        def close(self):
            if self.blocked:
                raise FileBoundaryError("controlled close failure", "PLUGIN_FILE_BUSY")

    handle = Handle()
    reservation.handles.append(handle)
    assert not game_install.game.release_install_target(reservation)
    with pytest.raises(GameServiceError):
        game_install.game.reserve_install_target(game_install.root, "pending")
    handle.blocked = False
    assert game_install.game.release_install_target(reservation)


def test_install_receipt_result_and_stage_are_accurate_without_path_leak(game_install):
    state = game_install
    snapshots = []
    unsubscribe = state.events.subscribe("game:operation_progress", snapshots.append)
    result = finish(state.broker, state.manager, install(state))
    unsubscribe()
    assert result["state"] == "completed" and result["effect"] == "committed", result
    assert result["stage"] == "finalize_install"
    assert result["result"]["version_id"] == "1.20.1"
    assert str(state.root) not in json.dumps(result)
    assert not state.game._install_reservations
    assert len(state.writes) == 1
    assert snapshots and snapshots[-1]["status"] == "completed"
    assert len({snapshot["operationId"] for snapshot in snapshots}) == 1
    assert all(snapshot["kind"] == "plugin_game_install" and snapshot["result"] is None for snapshot in snapshots)
    assert any("installation" in snapshot for snapshot in snapshots)
    assert str(state.root) not in json.dumps(snapshots)


def test_installed_result_reads_parent_declaration_inside_custom_instance(game_install):
    state = game_install

    def build(version_id, name, *, observer):
        observer.emit("check_resources")
        target = state.root / "versions" / name
        (target / f"{name}.json").write_text(
            json.dumps(
                {
                    "id": "fabric-loader",
                    "inheritsFrom": version_id,
                    "libraries": [{"name": "net.fabricmc:fabric-loader:0.16"}],
                }
            ),
            encoding="utf-8",
        )
        (target / f"{version_id}.json").write_text(json.dumps({"id": version_id}), encoding="utf-8")
        return []

    state.builders.build_minecraft_download_list = build
    result = finish(state.broker, state.manager, install(state, version_name="custom-instance"))
    assert result["state"] == "completed" and result["result"]["version_id"] == "custom-instance", result
    assert result["result"]["game_version"] == "1.20.1"
    assert result["result"]["loader"] == "Fabric"


def test_existing_user_instance_is_preserved_with_no_effect(game_install):
    state = game_install
    target = state.root / "versions/1.20.1"
    target.mkdir(parents=True)
    (target / "sentinel.txt").write_text("keep", encoding="utf-8")
    result = finish(state.broker, state.manager, install(state))
    assert result["state"] == "failed" and result["error_code"] == "PLUGIN_FILE_EXISTS", result
    assert result["effect"] == "none"
    assert (target / "sentinel.txt").read_text() == "keep" and not state.writes


def test_failure_keeps_partial_files_and_only_same_run_can_retry(game_install):
    state = game_install
    original = state.builders.build_minecraft_download_list

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise GameServiceError("controlled failure", "GAME_DOWNLOAD_FAILED")

    state.builders.build_minecraft_download_list = fail
    result = finish(state.broker, state.manager, install(state))
    assert result["state"] == "failed" and result["effect"] == "partial", result
    assert (state.root / "versions/1.20.1/1.20.1.json").exists()
    state.builders.build_minecraft_download_list = original
    result = finish(state.broker, state.manager, install(state))
    assert result["state"] == "completed", result
    assert not state.game._install_reservations


def test_cancellation_keeps_busy_target_until_worker_really_exits(game_install):
    state = game_install
    entered, release = Event(), Event()
    original = state.builders.build_minecraft_download_list

    def build(*args, **kwargs):
        original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return []

    state.builders.build_minecraft_download_list = build
    receipt = install(state)
    assert entered.wait(3)
    try:
        assert call(state.broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})["requested"]
        with pytest.raises(ProtocolError) as error:
            install(state)
        assert error.value.error_code == "PLUGIN_INSTALL_BUSY"
    finally:
        release.set()
    result = finish(state.broker, state.manager, receipt)
    assert result["state"] == "cancelled" and result["effect"] == "partial", result
    assert not state.game._install_reservations


def test_unsupported_filesystem_fails_explicitly_without_creating_instance(game_install, monkeypatch):
    from ECL.utils.file_handles import FileBoundaryError, FileDirectory

    def unsupported(*_):
        raise FileBoundaryError("文件系统未支持", "PLUGIN_RUNTIME_UNSUPPORTED")

    monkeypatch.setattr(FileDirectory, "open", unsupported)
    state = game_install
    result = finish(state.broker, state.manager, install(state))
    assert result["error_code"] == "PLUGIN_RUNTIME_UNSUPPORTED"
    assert result["effect"] == "none" and not (state.root / "versions/1.20.1").exists()
