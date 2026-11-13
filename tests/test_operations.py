from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ECL.foundation import EventBus
from ECL.services.operations import OperationManager
from ECL.utils.errors import GameServiceError


def test_commit_does_not_block_unrelated_cancel(tmp_path):
    manager = OperationManager(EventBus(), max_workers=2)
    entered, release, running = Event(), Event(), Event()

    def commit(context):
        def action():
            entered.set()
            assert release.wait(5)

        context.finalize(action)

    def other(context):
        running.set()
        assert release.wait(5)
        context.check_cancelled()

    with ThreadPoolExecutor(1) as caller:
        first = manager.submit("resource_install", other)["operationId"]
        assert running.wait(3)
        # submit emits a snapshot; a separate caller lets the old global-lock bug surface.
        submitted = caller.submit(manager.submit, "resource_install", commit)
        assert entered.wait(3)
        with ThreadPoolExecutor(1) as canceller:
            cancelled = canceller.submit(manager.cancel, first)
            try:
                assert cancelled.result(0.5)
            finally:
                release.set()
        second = submitted.result(3)["operationId"]
    manager.close()
    manager._operations[first].future.result(3)
    manager._operations[second].future.result(3)


def test_real_failure_after_cancel_is_failed(tmp_path):
    manager = OperationManager(EventBus())
    entered, release = Event(), Event()

    def worker(context):
        entered.set()
        assert release.wait(3)
        raise GameServiceError("独立执行失败", "GAME_OPERATION_FAILED")

    operation_id = manager.submit("resource_install", worker)["operationId"]
    try:
        assert entered.wait(3)
        assert manager.cancel(operation_id)
    finally:
        release.set()
    manager._operations[operation_id].future.result(3)
    manager.close()
    assert manager.get(operation_id)["status"] == "failed"
    assert manager.get(operation_id)["errorCode"] == "GAME_OPERATION_FAILED"


def test_cancel_queued_task_never_calls_worker(tmp_path):
    manager = OperationManager(EventBus(), max_workers=1)
    entered, release = Event(), Event()
    called = []

    def blocked(context):
        entered.set()
        assert release.wait(5)

    first = manager.submit("resource_install", blocked)["operationId"]
    try:
        assert entered.wait(3)
        queued = manager.submit("resource_install", lambda context: called.append(1))["operationId"]
        assert manager.cancel(queued)
    finally:
        release.set()
    manager._operations[first].future.result(3)
    manager._operations[queued].future.result(3)
    manager.close()
    assert called == []
    assert manager.get(queued)["status"] == "cancelled"


def test_cancel_before_commit_keeps_staged_effect_and_never_runs_action(tmp_path):
    manager = OperationManager(EventBus())
    entered, release = Event(), Event()
    actions = []

    def worker(context):
        context.report_effect("staged")
        entered.set()
        assert release.wait(5)
        context.finalize(lambda: actions.append(1))

    operation_id = manager.submit("resource_install", worker)["operationId"]
    try:
        assert entered.wait(3)
        assert manager.cancel(operation_id)
    finally:
        release.set()
    manager._operations[operation_id].future.result(3)
    manager.close()
    assert manager.get(operation_id)["status"] == "cancelled"
    assert manager._operations[operation_id].effect == "staged"
    assert actions == []


def test_finished_context_cannot_commit_or_report_late_progress(tmp_path):
    manager = OperationManager(EventBus())
    contexts, actions = [], []
    operation_id = manager.submit("resource_install", lambda context: contexts.append(context))["operationId"]
    manager._operations[operation_id].future.result(3)
    try:
        with pytest.raises(GameServiceError) as error:
            contexts[0].finalize(lambda: actions.append(1))
        assert error.value.error_code == "OPERATION_FINISHED"
        with pytest.raises(GameServiceError):
            contexts[0].progress(10, "late")
        with pytest.raises(GameServiceError):
            contexts[0].report_effect("partial")
    finally:
        manager.close()
    assert actions == []
    assert manager.get(operation_id)["status"] == "completed"


def test_parallel_commit_is_rejected_while_first_action_is_running(tmp_path):
    manager = OperationManager(EventBus())
    entered, release = Event(), Event()
    contexts, actions = [], []

    def worker(context):
        contexts.append(context)

        def commit():
            entered.set()
            assert release.wait(5)

        context.finalize(commit)

    operation_id = manager.submit("resource_install", worker)["operationId"]
    try:
        assert entered.wait(3)
        with pytest.raises(GameServiceError) as error:
            contexts[0].finalize(lambda: actions.append(1))
        assert error.value.error_code == "OPERATION_COMMIT_STARTED"
    finally:
        release.set()
        manager._operations[operation_id].future.result(3)
        manager.close()
    assert actions == []


def test_terminal_operations_are_capped_and_oldest_evicted():
    manager = OperationManager(EventBus(), max_workers=2)
    submitted: list[str] = []
    try:
        for _ in range(OperationManager.max_terminal_operations + 5):
            operation_id = manager.submit("resource_install", lambda context: None)["operationId"]
            manager._operations[operation_id].future.result(5)
            submitted.append(operation_id)

        remaining = [item for item in submitted if item in manager._operations]
        assert len(remaining) == OperationManager.max_terminal_operations
        assert remaining == submitted[5:]
        with pytest.raises(GameServiceError):
            manager.get(submitted[0])
        assert manager.get(submitted[-1])["status"] == "completed"
    finally:
        manager.close()
