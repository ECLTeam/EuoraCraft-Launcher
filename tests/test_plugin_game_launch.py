from __future__ import annotations

import json
import os
import sys
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from anyio.from_thread import start_blocking_portal

from ECL.foundation import EventBus
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.resources import RuntimeResources
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.game import GameService, GameServiceError
from ECL.services.operations import OperationManager
from ECL.utils.errors import AccountError
from ECL.utils.windows_file_handles import FileBoundaryError
from tests.test_game_service import EmptySearchMinecraft, FakeAccounts
from tests.test_plugin_host_files import call, finish

native_launch = pytest.mark.skipif(sys.platform != "win32", reason="第三方游戏执行只开放Windows本地NTFS")


@pytest.fixture
def game_launch(tmp_path, monkeypatch):
    root = tmp_path / "game"
    for name in ("one", "two"):
        directory = root / "versions" / name
        directory.mkdir(parents=True)
        (directory / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
    java = tmp_path / "java.exe"
    java.write_bytes(b"")
    events = EventBus()
    manager = OperationManager(tmp_path / "data", events, max_workers=3)
    game = GameService(
        FakeAccounts(),
        data_path=tmp_path / "data",
        operations=manager,
        event_bus=events,
        search_factory=EmptySearchMinecraft,
        enable_version_watcher=False,
        authlib_injector=SimpleNamespace(needs_download=lambda: False, ensure=lambda: None, close=lambda: None),
        command_builder=lambda _config: [sys.executable, "-c", "import time; time.sleep(120)"],
    )
    monkeypatch.setattr(game, "_resolve_java_path", lambda *_: str(java))
    monkeypatch.setattr(game, "_validate_launch_environment", lambda *_: None)
    monkeypatch.setattr(
        game, "_context", lambda *_: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_: []))
    )
    worker = PluginWorkerService(
        tmp_path / "data",
        instance_queries=game,
        instance_roots=lambda: (root,),
        operations=manager,
        game_operations=game,
    )
    entries = []

    def create():
        name = f"launcher{len(entries)}"
        source = tmp_path / name
        source.mkdir()
        metadata = {
            "name": name,
            "api_version": 2,
            "permissions": [{"capability": cap} for cap in ["instances.read", "instances.launch", "instances.stop"]],
        }
        (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
        (source / "main.py").write_text("value=1", encoding="utf-8")
        worker.register(metadata, source, "directory")
        view = worker.permissions(name)
        roots = worker.permission_resources(name, view["content_digest"], None, "", None, 100, "instances.read")
        root_id = roots["items"][0]["resource_id"]
        children = worker.permission_resources(name, view["content_digest"], root_id, "", None, 100, "instances.launch")
        instance_id = next(item["resource_id"] for item in children["items"] if item["version_id"] == "one")
        worker.update_permissions(
            name,
            view["content_digest"],
            view["revision"],
            (
                PermissionDecision(capability="instances.read", decision="allow", resource_refs=(root_id,)),
                PermissionDecision(capability="instances.launch", decision="allow", resource_refs=(instance_id,)),
                PermissionDecision(capability="instances.stop", decision="allow", resource_refs=(instance_id,)),
            ),
            PermissionManagementCaller.main_webview,
            children["catalog_revision"],
        )
        entry = worker._entry(name)
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
        ref = call(broker, "resources.list", {"capability": "instances.launch"})["items"][0]["instance_ref"]
        state = SimpleNamespace(
            game=game,
            manager=manager,
            worker=worker,
            broker=broker,
            ref=ref,
            root=root,
            root_id=root_id,
            instance_id=instance_id,
            name=name,
            entry=entry,
        )
        entries.append(state)
        return state

    with start_blocking_portal("asyncio") as portal:
        portal.call(game.bind_plugin_launch_loop)
        first = create()
        first.create = create
        first.portal = portal
        yield first
        for state in entries:
            state.broker.close()
        manager.close()
        for operation in tuple(manager._operations.values()):
            if operation.future and not operation.future.cancelled():
                operation.future.result(10)
        for state in entries:
            assert state.broker.wait_idle(3)
        game.instances.shutdown_all(force=True, wait_timeout=3)
        worker.close()
        game.close()


def launch(state, key=None):
    return call(state.broker, "game.launch", {"instance_ref": state.ref, "options": {"memory_mb": 1024}}, key)


def completed_launch(state):
    result = finish(state.broker, state.manager, launch(state))
    assert result["state"] == "completed" and result["effect"] == "committed", result
    return result["result"]


@native_launch
@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_actual_worker_launch_query_stop_and_close_preserves_process(game_launch, is_frozen):
    state = game_launch
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要本轮真实冻结产物")
    if is_frozen:
        state.worker._executable = ExecutableLaunchSpec(
            Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "pyinstaller")
        )
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_game_launch"
    metadata = json.loads((example / "plugin.json").read_bytes())
    state.worker.register(metadata, example, "directory")
    view = state.worker.permissions(metadata["name"])
    page = state.worker.permission_resources(
        metadata["name"], view["content_digest"], state.root_id, "", None, 50, "instances.launch"
    )
    state.worker.update_permissions(
        metadata["name"],
        view["content_digest"],
        view["revision"],
        tuple(
            PermissionDecision(
                capability=item["capability"],
                decision="allow",
                resource_refs=(state.root_id,)
                if item["capability"] == "instances.read"
                else (state.instance_id,)
                if item["capability"] in {"instances.launch", "instances.stop"}
                else (),
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

    def wait(receipt):
        deadline = monotonic() + 15
        while True:
            view = command("status", {"operation_ref": receipt["operation_ref"]})
            if view["state"] in {"completed", "failed", "cancelled"} and not view["cleanup_pending"]:
                assert view["state"] == "completed", view
                return view["result"]
            assert monotonic() < deadline
            sleep(0.025)

    assert not state.game._plugin_game_runs
    instance_ref = command("discover", {"capability": "instances.launch"})["items"][0]["instance_ref"]
    params = {"instance_ref": instance_ref, "mutation_key": "worker-launch"}
    receipt = command("launch", params)
    assert command("launch", params) == receipt
    run = wait(receipt)
    assert run["running"] and command("running", {"instance_ref": instance_ref})["items"] == [run]
    result = wait(command("stop", {"game_run_ref": run["game_run_ref"], "mutation_key": "worker-stop"}))
    assert not result["running"] and result["exit_code"] is not None
    survivor = wait(command("launch", {"instance_ref": instance_ref, "mutation_key": "worker-survivor"}))
    process = next(run.process for run in state.game._plugin_game_runs.values() if run.process.pid == survivor["pid"])
    state.worker.stop(metadata["name"])
    assert process.poll() is None


def revoke_launch(state):
    session = state.broker.session
    state.worker.grants.update(
        session,
        (PermissionDecision(capability="instances.launch", decision="deny"),),
        expected_revision=session.view().revision,
        content_digest=session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    state.broker.refresh_pending_work()


@native_launch
def test_real_child_launch_running_stop_and_retained_exit_code(game_launch):
    state = game_launch
    result = completed_launch(state)
    assert result["pid"] > 0 and result["running"]
    assert set(result) == {"game_run_ref", "instance_ref", "pid", "running", "exit_code"}
    assert str(state.root) not in json.dumps(result)
    assert call(state.broker, "instances.get", {"instance_ref": state.ref})["version_id"] == "one"
    running = call(state.broker, "game.running", {"instance_ref": state.ref})["items"]
    assert running == [result]
    stopped = finish(
        state.broker, state.manager, call(state.broker, "game.stop", {"game_run_ref": result["game_run_ref"]})
    )
    assert stopped["state"] == "completed" and stopped["result"]["running"] is False, stopped
    assert stopped["result"]["exit_code"] is not None
    assert call(state.broker, "game.running", {"instance_ref": state.ref})["items"][0]["running"] is False
    repeated = finish(
        state.broker, state.manager, call(state.broker, "game.stop", {"game_run_ref": result["game_run_ref"]})
    )
    assert repeated["state"] == "completed" and not repeated["result"]["running"]


@native_launch
def test_another_run_cannot_query_or_stop_foreign_game(game_launch):
    state = game_launch
    result = completed_launch(state)
    other = state.create()
    assert call(other.broker, "game.running", {"instance_ref": other.ref})["items"] == []
    with pytest.raises(ProtocolError) as error:
        call(other.broker, "game.stop", {"game_run_ref": result["game_run_ref"]})
    assert error.value.error_code == "PLUGIN_OWNER_MISMATCH"
    assert call(state.broker, "game.running", {"instance_ref": state.ref})["items"][0]["running"]


def test_plugin_and_user_share_single_launch_reservation(game_launch):
    state = game_launch
    state.game._launch_cancel_event = Event()
    try:
        with pytest.raises(ProtocolError) as error:
            launch(state)
        assert error.value.error_code == "PLUGIN_LAUNCH_BUSY"
    finally:
        state.game._launch_cancel_event = None
    reservation = state.game.reserve_plugin_launch(state.broker.game_launch._origin)

    async def user_launch():
        return await state.game.launch_instance({"version_id": "one"}, game_path=state.root)

    try:
        with pytest.raises(GameServiceError) as error:
            state.portal.call(user_launch)
        assert error.value.error_code == "LAUNCH_ALREADY_RUNNING"
    finally:
        state.game.release_plugin_launch(reservation)
    replacement = state.game.reserve_plugin_launch(state.broker.game_launch._origin)
    assert state.game.release_plugin_launch(reservation)
    assert state.game._launch_cancel_event is replacement.cancel_event
    state.game.release_plugin_launch(replacement)


def test_launch_reservation_keeps_busy_state_until_actual_handle_cleanup(game_launch):
    state = game_launch
    reservation = state.game.reserve_plugin_launch(state.broker.game_launch._origin)

    class Handle:
        blocked = True

        def close(self):
            if self.blocked:
                raise FileBoundaryError("controlled close failure", "PLUGIN_FILE_BUSY")

    handle = Handle()
    reservation.handles.append(handle)
    assert not state.game.release_plugin_launch(reservation)
    assert state.game._launch_cancel_event is reservation.cancel_event
    handle.blocked = False
    assert state.game.release_plugin_launch(reservation)
    assert state.game._launch_cancel_event is None


def test_completed_future_timeout_is_not_mistaken_for_still_running_work(game_launch):
    future = Future()
    future.set_exception(TimeoutError("controlled execution timeout"))
    reservation = game_launch.game.reserve_plugin_launch(game_launch.broker.game_launch._origin)
    try:
        with pytest.raises(GameServiceError) as error:
            game_launch.game._wait_plugin_launch(future, None, game_launch.broker.game_launch._origin, reservation)
        assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
    finally:
        game_launch.game.release_plugin_launch(reservation)


@native_launch
def test_duplicate_mutation_does_not_create_two_processes(game_launch):
    state = game_launch
    receipt = launch(state, "same-start")
    assert launch(state, "same-start") == receipt
    result = finish(state.broker, state.manager, receipt)
    assert result["state"] == "completed", result
    assert len(state.game.instances.get_instances_info()) == 1


@native_launch
@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_cancellation_or_revocation_before_commit_never_creates_process(game_launch, action):
    state = game_launch
    entered, release = Event(), Event()
    original = state.game._command_builder

    def blocked(config):
        entered.set()
        assert release.wait(5)
        return original(config)

    state.game._command_builder = blocked
    receipt = launch(state)
    assert entered.wait(3)
    try:
        if action == "cancel":
            call(state.broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
        else:
            revoke_launch(state)
    finally:
        release.set()
    result = finish(state.broker, state.manager, receipt)
    assert result["state"] in {"cancelled", "failed"}, result
    assert result["effect"] == "partial"
    assert not state.game.instances.get_instances_info()
    assert state.game._launch_cancel_event is None


@native_launch
def test_revocation_after_commit_acceptance_does_not_kill_created_game(game_launch, monkeypatch):
    state = game_launch
    entered, release = Event(), Event()
    original = state.game.instances.create_instance

    def created(**options):
        result = original(**options)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(state.game.instances, "create_instance", created)
    receipt = launch(state)
    assert entered.wait(3)
    try:
        revoke_launch(state)
    finally:
        release.set()
    result = finish(state.broker, state.manager, receipt)
    assert result["state"] == "completed" and result["effect"] == "committed", result
    assert result["result"] is None
    processes = state.game.instances.get_instances_info()
    assert len(processes) == 1 and processes[0]["Instance"].poll() is None


@native_launch
def test_instance_id_reuse_cannot_stop_another_process_object(game_launch):
    state = game_launch
    result = completed_launch(state)
    item = state.game.instances.get_instances_info()[0]
    original = item["Instance"]
    item["Instance"] = SimpleNamespace(pid=original.pid)
    try:
        stopped = finish(
            state.broker, state.manager, call(state.broker, "game.stop", {"game_run_ref": result["game_run_ref"]})
        )
        assert stopped["state"] == "failed" and stopped["error_code"] == "PLUGIN_OWNER_MISMATCH", stopped
        assert stopped["effect"] == "none" and original.poll() is None
    finally:
        item["Instance"] = original


@native_launch
def test_missing_account_is_explicit_without_creating_process(game_launch, monkeypatch):
    async def unavailable():
        raise AccountError("No account", "ACCOUNT_REQUIRED")

    monkeypatch.setattr(game_launch.game.accounts, "get_launch_credentials", unavailable)
    result = finish(game_launch.broker, game_launch.manager, launch(game_launch))
    assert result["state"] == "failed" and result["error_code"] == "PLUGIN_ACCOUNT_REQUIRED", result
    assert not game_launch.game.instances.get_instances_info()


@native_launch
def test_account_change_before_process_commit_is_rejected(game_launch, monkeypatch):
    original = game_launch.game._command_builder

    def changed(config):
        monkeypatch.setattr(game_launch.game.accounts, "current_account", lambda: {"id": "changed", "type": "offline"})
        return original(config)

    game_launch.game._command_builder = changed
    result = finish(game_launch.broker, game_launch.manager, launch(game_launch))
    assert result["state"] == "failed" and result["error_code"] == "PLUGIN_ACCOUNT_CHANGED", result
    assert not game_launch.game.instances.get_instances_info()


@native_launch
def test_post_spawn_failure_preserves_ownership_and_user_visibility(game_launch, monkeypatch):
    def fail(*_):
        raise OSError("controlled stats failure")

    monkeypatch.setattr(game_launch.game._version_stats, "record_launch", fail)
    result = finish(game_launch.broker, game_launch.manager, launch(game_launch))
    assert result["state"] == "failed" and result["effect"] == "committed", result
    processes = call(game_launch.broker, "game.running", {"instance_ref": game_launch.ref})["items"]
    assert len(processes) == 1 and processes[0]["running"]
    assert game_launch.game.list_instances()[0]["pid"] == processes[0]["pid"]


@native_launch
def test_immediately_exited_game_remains_queryable_with_actual_exit_code(game_launch):
    game_launch.game._command_builder = lambda _: [sys.executable, "-c", "raise SystemExit(0)"]
    result = completed_launch(game_launch)
    process = game_launch.game._plugin_game_runs[
        game_launch.broker.game_launch._references[result["game_run_ref"]].token
    ].process
    process.wait(timeout=3)
    observed = call(game_launch.broker, "game.running", {"instance_ref": game_launch.ref})["items"][0]
    assert not observed["running"] and observed["exit_code"] == 0


@native_launch
def test_close_releases_worker_control_but_preserves_game_for_user(game_launch):
    state = game_launch
    result = completed_launch(state)
    state.broker.close()
    assert state.broker.wait_idle(3)
    assert not state.game._plugin_game_runs
    assert state.game.list_instances()[0]["pid"] == result["pid"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX拒绝第三方游戏写边界")
def test_posix_plugin_launch_is_explicitly_unsupported(game_launch):
    result = finish(game_launch.broker, game_launch.manager, launch(game_launch))
    assert result["error_code"] == "PLUGIN_RUNTIME_UNSUPPORTED" and result["effect"] == "none", result
    assert not game_launch.game.instances.get_instances_info()
