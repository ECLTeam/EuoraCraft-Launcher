from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_models import PluginContentBinding
from ECL.plugins.run_store import PluginRunStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.services.operations import OperationManager, OperationOrigin
from ECL.utils import EventBus
from ECL.utils.errors import GameServiceError


@pytest.fixture
def runtime(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("value = 1", encoding="utf-8")
    binding = PluginContentBinding.capture(code, "windows-x86_64-cp311")
    store = PluginRunStore()
    manager = OperationManager(EventBus(), max_workers=2)
    brokers = []

    def create(installation_id=None, content=None):
        session = store.open_session(installation_id or uuid4().hex, content or binding)
        session.view()
        broker = PluginHostBroker(session, tmp_path, lambda *args: {}, run_state=lambda: "enabled", operations=manager)
        brokers.append(broker)
        return broker

    broker = create()
    yield broker, manager, store, create
    for item in brokers:
        item.session.close()
        item.close()
    manager.close()
    for operation in tuple(manager._operations.values()):
        if operation.future is not None and not operation.future.cancelled():
            operation.future.result(5)


def call(broker, method, params=None, key=None):
    return broker.handle(RpcRequest(method, params or {}, monotonic() + 5, key or uuid4().hex))


def submit(broker, worker, *, result=False):
    assert broker.operations is not None
    return broker.operations.submit(
        "host_fixture",
        worker,
        broker.session.lease(),
        project_result=(lambda value: {"value": value}) if result else None,
        result_lease=broker.session.lease() if result else None,
    )


def finish(broker, manager, reference):
    operation_id = broker.operations._references[reference].operation_id
    manager._operations[operation_id].future.result(5)
    return call(broker, "operations.get", {"operation_ref": reference})


def revoke(broker, store):
    broker.session.close()


def test_task_views_filter_results_and_do_not_expose_raw_details(runtime, tmp_path):
    broker, manager, store, _ = runtime

    def worker(context):
        context.progress(50, "private-path-secret", owner="forged", status="completed", effect="committed")
        return "safe"

    reference = submit(broker, worker, result=True)
    view = finish(broker, manager, reference)
    assert view["state"] == "completed"
    assert view["result"] == {"value": "safe"}
    assert "private-path-secret" not in json.dumps(view)
    revoke(broker, store)
    assert call(broker, "operations.get", {"operation_ref": reference})["result"] is None
    # 任务摘要不再落盘，进程内记录之外的任何位置都不应出现该任务。
    assert not (tmp_path / "operations").exists()


@pytest.mark.parametrize("other_kind", ["other_install", "other_run", "other_content", "user"])
def test_ownership_rejects_other_sources_and_global_ids(runtime, other_kind):
    broker, manager, _, create = runtime
    reference = submit(broker, lambda context: None)
    finish(broker, manager, reference)
    global_id = broker.operations._references[reference].operation_id
    if other_kind == "user":
        other_reference = manager.submit("resource_install", lambda context: None)["operationId"]
        target = broker
    else:
        installation = broker.session.view().installation_id if other_kind != "other_install" else None
        content = None
        if other_kind == "other_content":
            binding = broker.session.binding
            content = PluginContentBinding.capture(binding.snapshot.root_path, "linux-x86_64-cp311")
        target = create(installation, content)
        other_reference = reference
    for method in ("operations.get", "operations.cancel"):
        for rejected in (other_reference, global_id):
            with pytest.raises(ProtocolError) as error:
                call(target, method, {"operation_ref": rejected})
            assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"
    if target is not broker:
        assert call(target, "operations.list")["items"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"operation_ref": "../bad"},
        {"operation_ref": "0" * 32, "owner": "me"},
        {"operation_ref": "0" * 32, "operation_id": "0" * 32},
    ],
)
def test_control_models_reject_untrusted_identity(runtime, params):
    with pytest.raises(ProtocolError) as error:
        call(runtime[0], "operations.get", params)
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"


def test_cancel_waits_for_actual_worker_and_deduplicates(runtime):
    broker, manager, _store, _ = runtime
    entered, release = Event(), Event()

    def worker(context):
        entered.set()
        assert release.wait(5)
        context.check_cancelled()

    reference = submit(broker, worker)
    try:
        assert entered.wait(3)
        result = call(broker, "operations.cancel", {"operation_ref": reference}, "same")
        assert result["requested"] is True
        assert result["state"] == "running"
        assert not broker.wait_idle()
        assert call(broker, "operations.cancel", {"operation_ref": reference}, "same") == result
        assert call(broker, "mutations.get", {"mutation_key": "same"})["state"] == "succeeded"
    finally:
        release.set()
    assert finish(broker, manager, reference)["state"] in {"failed", "cancelled"}
    operation_id = broker.operations._references[reference].operation_id
    assert manager._operations[operation_id].cancellation_reason == "plugin_requested"
    assert broker.wait_idle()


@pytest.mark.parametrize("is_failure", [False, True])
def test_accepted_commit_finishes_after_revoke_without_blocking_control(runtime, is_failure):
    broker, manager, store, create = runtime
    other = create()
    entered, release = Event(), Event()
    actions = []

    def worker(context):
        def action():
            actions.append(1)
            entered.set()
            assert release.wait(5)
            if is_failure:
                raise OSError("private-error-path")
            return "saved"

        return context.finalize(action)

    reference = submit(broker, worker, result=True)
    try:
        assert entered.wait(3)
        assert call(broker, "operations.get", {"operation_ref": reference})["state"] == "committing"
        with pytest.raises(ProtocolError) as error:
            call(broker, "operations.cancel", {"operation_ref": reference})
        assert error.value.error_code == "PLUGIN_COMMIT_STARTED"
        with ThreadPoolExecutor(1) as caller:
            caller.submit(revoke, other, store).result(1)
            caller.submit(revoke, broker, store).result(1)
    finally:
        release.set()
    view = finish(broker, manager, reference)
    assert actions == [1]
    assert view["state"] == ("failed" if is_failure else "completed")
    assert view["effect"] == ("unknown" if is_failure else "committed")
    assert view["result"] is None


def test_revoke_before_accept_prevents_action(runtime):
    broker, manager, store, _ = runtime
    entered, release = Event(), Event()
    actions = []

    def worker(context):
        entered.set()
        assert release.wait(5)
        context.finalize(lambda: actions.append(1))

    reference = submit(broker, worker)
    try:
        assert entered.wait(3)
        revoke(broker, store)
    finally:
        release.set()
    view = finish(broker, manager, reference)
    assert view["state"] == "failed"
    assert actions == []


def test_successful_commit_cannot_accept_second_action(runtime):
    broker, manager, _, _ = runtime
    actions = []

    def work(context):
        context.finalize(lambda: actions.append(1))
        with pytest.raises(GameServiceError) as error:
            context.finalize(lambda: actions.append(2))
        assert error.value.error_code == "OPERATION_COMMIT_STARTED"

    view = finish(broker, manager, submit(broker, work))
    assert view["state"] == "completed" and view["effect"] == "committed"
    assert actions == [1]


@pytest.mark.parametrize("has_effect", [False, True])
def test_plugin_tasks_never_write_operation_files(runtime, tmp_path, has_effect):
    broker, manager, _, _ = runtime
    actions = []

    def worker(context):
        if not has_effect:
            return None
        return context.finalize(lambda: actions.append(1))

    reference = submit(broker, worker)
    view = finish(broker, manager, reference)
    assert view["state"] == "completed"
    assert actions == ([1] if has_effect else [])
    assert broker.wait_idle()
    assert not (tmp_path / "operations").exists()


def test_terminal_does_not_mean_cleanup_finished(runtime):
    broker, manager, _, _ = runtime
    entered = Event()
    original_cleanup = manager._cleanup_owned

    def pending_cleanup(operation):
        entered.set()
        return False

    manager._cleanup_owned = pending_cleanup
    reference = submit(broker, lambda context: None)
    assert entered.wait(3)
    assert call(broker, "operations.get", {"operation_ref": reference})["state"] == "completed"
    assert not broker.wait_idle()
    broker.close()
    assert not broker.wait_idle()
    # 恢复真实清理后，收尾在同一标识上完成并释放引用。
    manager._cleanup_owned = original_cleanup
    assert broker.wait_idle()
    assert broker.operations._references == {}


def test_paging_uses_fixed_references_and_fresh_permission_filtered_views(runtime):
    broker, manager, store, _ = runtime
    refs = [submit(broker, lambda context: "safe", result=True) for _ in range(3)]
    for ref in refs:
        finish(broker, manager, ref)
    page = call(broker, "operations.list", {"limit": 1})
    assert page["items"][0]["operation_ref"] == refs[0]
    revoke(broker, store)
    second = call(broker, "operations.list", {"limit": 1, "cursor": page["next_cursor"]})
    assert second["items"][0]["operation_ref"] == refs[1]
    assert second["items"][0]["result"] is None
    with pytest.raises(ProtocolError):
        call(broker, "operations.list", {"limit": 2, "cursor": second["next_cursor"]})


def test_reference_capacity_does_not_evict_active_or_finished_tasks(runtime):
    broker, manager, _, _ = runtime
    for _ in range(128):
        ref = submit(broker, lambda context: None)
        finish(broker, manager, ref)
    with pytest.raises(ProtocolError) as error:
        submit(broker, lambda context: pytest.fail("over capacity"))
    assert error.value.error_code == "PLUGIN_RATE_LIMITED"
    broker.close()
    assert broker.wait_idle()
    assert not any(item.origin is not None for item in manager._operations.values())


def test_origin_snapshot_does_not_restore_unknown_task(runtime):
    broker, manager, _, _ = runtime
    reference = submit(broker, lambda context: None)
    finish(broker, manager, reference)
    item = broker.operations._references[reference]
    with pytest.raises(GameServiceError) as error:
        manager.owned_snapshot(item.operation_id, OperationOrigin(uuid4().hex, "0" * 64, uuid4().hex))
    assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"

    broker.close()
    assert broker.wait_idle()
    with pytest.raises(GameServiceError) as error:
        manager.owned_snapshot(item.operation_id, broker.operations._origin)
    assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"


def test_running_task_does_not_block_other_terminal_cleanup_and_supervision_retries(runtime):
    broker, manager, _, _ = runtime
    started, release = Event(), Event()
    may_clean, cleaned = Event(), Event()

    def active(context):
        started.set()
        assert release.wait(10)

    def cleanup():
        if not may_clean.is_set():
            return False
        cleaned.set()
        return True

    first = submit(broker, active)
    try:
        assert started.wait(3)
        second = broker.operations.submit("host_fixture", lambda context: None, broker.session.lease(), cleanup=cleanup)
        view = finish(broker, manager, second)
        assert view["cleanup_pending"]
        may_clean.set()
        broker.refresh_pending_work()
        assert cleaned.is_set()
        assert not call(broker, "operations.get", {"operation_ref": second})["cleanup_pending"]
        assert call(broker, "operations.get", {"operation_ref": first})["state"] == "running"
    finally:
        release.set()
        finish(broker, manager, first)


@pytest.mark.parametrize(
    "method,params",
    [
        ("commands.register", {"name": "test", "callback_ref": "0" * 32}),
        ("settings.define", {"definitions": [{"key": "key", "type": "bool", "default": False}]}),
        ("events.subscribe", {"event": "settings.changed", "callback_ref": "0" * 32}),
        ("settings.set", {"values": {"key": True}, "expected_revision": 0}),
    ],
)
def test_current_short_writes_recheck_at_acceptance(runtime, monkeypatch, method, params):
    broker, _, _, _ = runtime
    call(broker, "settings.define", {"definitions": [{"key": "key", "type": "bool", "default": False}]})
    original_accept = broker.session.accept

    def accept(lease, register):
        broker.session.close()
        return original_accept(lease, register)

    monkeypatch.setattr(broker.session, "accept", accept)
    with pytest.raises(ProtocolError) as error:
        call(broker, method, params)
    assert error.value.error_code == "PLUGIN_RUN_EXPIRED"
    assert broker._commands == {}
    assert broker._subscriptions == {}
    assert not broker.settings._file_path(broker.session.view().installation_id).exists()


def test_host_capacity_keeps_other_owners_tasks_tracked(runtime, monkeypatch):
    broker, manager, _, create = runtime
    monkeypatch.setattr(manager, "max_plugin_operations", 2)
    second, third = create(), create()
    entered, release = Event(), Event()

    def work(context):
        entered.set()
        assert release.wait(5)

    try:
        first_ref = submit(broker, work)
        assert entered.wait(3)
        second_ref = submit(second, work)
        with pytest.raises(ProtocolError) as error:
            submit(third, lambda context: pytest.fail("host capacity exceeded"))
        assert error.value.error_code == "PLUGIN_RATE_LIMITED"
        assert call(broker, "operations.get", {"operation_ref": first_ref})["state"] == "running"
        assert call(second, "operations.get", {"operation_ref": second_ref})["state"] in {"queued", "running"}
    finally:
        release.set()
    finish(broker, manager, first_ref)
    finish(second, manager, second_ref)


def test_task_paging_enforces_projected_byte_budget_and_snapshot_limits(runtime):
    broker, manager, _, _ = runtime
    refs = [submit(broker, lambda context: "x" * 32000, result=True) for _ in range(2)]
    for ref in refs:
        finish(broker, manager, ref)
    page = call(broker, "operations.list", {"limit": 100})
    assert len(page["items"]) == 1
    assert page["next_cursor"]
    second = call(broker, "operations.list", {"limit": 100, "cursor": page["next_cursor"]})
    assert second["items"][0]["operation_ref"] == refs[1]
    assert second["next_cursor"] is None
    for _ in range(16):
        assert call(broker, "operations.list", {"limit": 1})["next_cursor"]
    with pytest.raises(ProtocolError) as error:
        call(broker, "operations.list", {"limit": 1})
    assert error.value.error_code == "PLUGIN_RATE_LIMITED"
    with pytest.raises(ProtocolError) as error:
        call(broker, "operations.submit", {})
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"
