from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest

from ECL.foundation import EventBus
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.services.operations import OperationManager, OperationOrigin
from ECL.utils.errors import GameServiceError


@pytest.fixture
def runtime(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("value = 1", encoding="utf-8")
    capabilities = frozenset(set())
    binding = PluginContentBinding.capture(
        code, tuple(PermissionDeclaration(capability=item) for item in capabilities), "windows-x86_64-cp311"
    )
    store = PluginGrantStore(tmp_path, implemented_capabilities=capabilities)
    manager = OperationManager(tmp_path, EventBus(), max_workers=2)
    brokers = []

    def create(installation_id=None, content=None):
        session = store.open_session(installation_id or uuid4().hex, content or binding)
        view = session.view()
        store.update(
            session,
            tuple(PermissionDecision(capability=item, decision="allow") for item in capabilities),
            expected_revision=view.revision,
            content_digest=session.binding.content_digest,
            caller=PermissionManagementCaller.main_webview,
        )
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
        broker.session.authorize("plugin.base"),
        project_result=(lambda value: {"value": value}) if result else None,
        result_lease=broker.session.authorize("plugin.base") if result else None,
    )


def finish(broker, manager, reference):
    operation_id = broker.operations._references[reference].operation_id
    manager._operations[operation_id].future.result(5)
    return call(broker, "operations.get", {"operation_ref": reference})


def revoke(broker, store):
    store.update(
        broker.session,
        (),
        expected_revision=broker.session.view().revision,
        content_digest=broker.session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


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
    journal = json.loads(next((tmp_path / "operations/plugin").glob("*.json")).read_bytes())
    assert journal["schema_version"] == 1
    assert journal["phase"] == "terminal"
    assert "private-path-secret" not in json.dumps(journal)
    assert "safe" not in json.dumps(journal)


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
            content = PluginContentBinding.capture(
                binding.snapshot.root_path, binding.declarations, "linux-x86_64-cp311"
            )
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


def test_cancel_waits_for_actual_worker_and_deduplicates_across_revoke(runtime):
    broker, manager, store, _ = runtime
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
        revoke(broker, store)
        assert call(broker, "operations.cancel", {"operation_ref": reference}, "same") == result
        assert call(broker, "mutations.get", {"mutation_key": "same"})["state"] == "succeeded"
    finally:
        release.set()
    # 失权检查可能先报告明确授权失败，不能把其错误伪装成取消。
    assert finish(broker, manager, reference)["state"] in {"failed", "cancelled"}
    operation_id = broker.operations._references[reference].operation_id
    journal = json.loads((manager._data_path / "plugin" / f"{operation_id}.json").read_bytes())
    assert journal["cancellation_reason"] == "plugin_requested"
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


@pytest.mark.parametrize("failed_phase", ["queued", "executing", "committing", "terminal"])
def test_journal_failures_never_replay_business_actions(runtime, monkeypatch, failed_phase):
    import ECL.services.operations as module

    broker, manager, _, _ = runtime
    original_write = module.atomic_write_text
    actions = []

    def write(target, payload):
        if json.loads(payload)["phase"] == failed_phase:
            raise OSError("test journal error")
        return original_write(target, payload)

    monkeypatch.setattr(module, "atomic_write_text", write)
    reference = submit(broker, lambda context: context.finalize(lambda: actions.append(1)))
    view = finish(broker, manager, reference)
    if failed_phase == "terminal":
        assert view["state"] == "completed"
        assert actions == [1]
        assert not broker.wait_idle()
    else:
        assert view["state"] == "failed"
        assert view["error_code"] == "PLUGIN_OPERATION_PERSIST_FAILED"
        assert actions == []
    monkeypatch.setattr(module, "atomic_write_text", original_write)
    assert broker.wait_idle()
    assert actions == ([1] if failed_phase == "terminal" else [])


def test_terminal_does_not_mean_cleanup_finished(runtime, monkeypatch):
    import ECL.services.operations as module

    broker, manager, _, _ = runtime
    entered, release = Event(), Event()
    original_write = module.atomic_write_text

    def write(target, payload):
        if json.loads(payload)["phase"] == "terminal":
            entered.set()
            assert release.wait(5)
        return original_write(target, payload)

    monkeypatch.setattr(module, "atomic_write_text", write)
    reference = submit(broker, lambda context: None)
    try:
        assert entered.wait(3)
        assert call(broker, "operations.get", {"operation_ref": reference})["state"] == "completed"
        assert not broker.wait_idle()
        broker.close()
        assert not broker.wait_idle()
    finally:
        release.set()
    # closed run 的引用仍保留至实际收尾结束。
    operation_id = broker.operations._references[reference].operation_id
    manager._operations[operation_id].future.result(5)
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


def test_origin_snapshot_does_not_restore_from_journal(runtime):
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
        second = broker.operations.submit(
            "host_fixture", lambda context: None, broker.session.authorize("plugin.base"), cleanup=cleanup
        )
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
    broker, _, store, _ = runtime
    call(broker, "settings.define", {"definitions": [{"key": "key", "type": "bool", "default": False}]})
    original_accept = broker.session.accept

    def accept(lease, register):
        store.update(
            broker.session,
            (),
            expected_revision=broker.session.view().revision,
            content_digest=broker.session.binding.content_digest,
            caller=PermissionManagementCaller.main_webview,
        )
        return original_accept(lease, register)

    monkeypatch.setattr(broker.session, "accept", accept)
    with pytest.raises(ProtocolError) as error:
        call(broker, method, params)
    assert error.value.error_code == "PLUGIN_GRANT_EXPIRED"
    assert broker._commands == {}
    assert broker._subscriptions == {}
    assert not broker._settings_path.exists()


def test_grant_disk_write_does_not_hold_global_authorization_lock(runtime, monkeypatch):
    import ECL.plugins.grant_store as module

    broker, _, store, create = runtime
    other = create()
    entered, release = Event(), Event()
    original_write = module.atomic_write_text

    installation_id = broker.session.view().installation_id

    def delayed_write(target, payload):
        if installation_id in str(target):
            entered.set()
            assert release.wait(5)
        return original_write(target, payload)

    monkeypatch.setattr(module, "atomic_write_text", delayed_write)
    with ThreadPoolExecutor(2) as callers:
        pending = callers.submit(revoke, broker, store)
        try:
            assert entered.wait(3)
            callers.submit(revoke, other, store).result(1)
            broker.session.close()
        finally:
            release.set()
        pending.result(3)
    assert broker.session.view().error_code == "PLUGIN_SESSION_EXPIRED"


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
