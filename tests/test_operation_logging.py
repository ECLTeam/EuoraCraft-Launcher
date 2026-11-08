from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from ECL.api.bridge import _ipc_handler, guard_ipc_handler
from ECL.api.contracts import failure, success
from ECL.api.operation_logging import IpcLogPolicy, IpcLogRuntime
from ECL.api.registry import IpcCommandRegistry
from ECL.foundation import EventBus
from ECL.services.operations import OperationManager
from ECL.utils.operation_logging import (
    OperationLogFilter,
    ReadableLogFormatter,
    current_operation,
    operation_scope,
    safe_log_text,
)


@pytest.fixture
def records():
    collected = []

    class Handler(logging.Handler):
        def emit(self, record):
            collected.append(record)

    logger = logging.getLogger("ecl-operation-test")
    handler = Handler()
    handler.addFilter(OperationLogFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, collected
    finally:
        logger.removeHandler(handler)


def test_all_registered_commands_have_explicit_log_policies() -> None:
    assert set(IpcLogPolicy.commands) == set(IpcCommandRegistry.command_names)
    assert all(
        spec.title and spec.kind in {"action", "query", "poll", "channel"} for spec in IpcLogPolicy.commands.values()
    )


def test_offline_uuid_preview_uses_quiet_logging_without_account_input(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success({"uuid": "private-uuid"})

    asyncio.run(guard_ipc_handler(state, "accounts_preview_offline_uuid", handler)({"username": "private-name"}))
    assert logs == []
    assert all(
        "private-name" not in record.getMessage() and "private-uuid" not in record.getMessage() for record in logs
    )


def test_sidebar_notification_success_uses_debug(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success()

    asyncio.run(guard_ipc_handler(state, "plugin_notify_sidebar_state", handler)({"collapsed": False}))
    assert logs == []


def test_sidebar_notification_failure_remains_visible(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return failure("通知失败", "SIDEBAR_NOTIFY_FAILED")

    asyncio.run(guard_ipc_handler(state, "plugin_notify_sidebar_state", handler)({"collapsed": True}))
    assert logs[-1].levelno == logging.WARNING
    assert "SIDEBAR_NOTIFY_FAILED" in logs[-1].getMessage()


def test_settings_save_only_logs_actual_write(tmp_path, records) -> None:
    from ECL.utils.config import ConfigStore

    logger, logs = records
    store = ConfigStore(tmp_path / "data", EventBus())
    store.logger = logger
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        await asyncio.to_thread(store.save_config, "ui", body)
        return success()

    guarded = guard_ipc_handler(state, "settings_set", handler)
    current = store.get_config("ui")
    logs.clear()
    asyncio.run(guarded(current))
    assert logs == []
    asyncio.run(guarded({**current, "locale": "de-DE"}))
    assert len(logs) == 1
    assert logs[0].levelno == logging.INFO
    assert "已保存" in logs[0].getMessage()
    assert "de-DE" not in logs[0].getMessage()
    asyncio.run(guarded({**current, "locale": "de-DE"}))
    assert len(logs) == 1


def test_ipc_records_returned_failure_without_request_body(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return failure("错误输入", "INVALID_REQUEST")

    response = asyncio.run(guard_ipc_handler(state, "settings_set", handler)({"password": "never-log-this"}))
    assert response["success"] is False
    assert len(logs) == 1
    assert logs[0].operation_action == "保存设置"
    assert all("操作编号" not in record.getMessage() for record in logs)
    assert "错误码：INVALID_REQUEST" in logs[0].getMessage()
    assert "never-log-this" not in str([record.getMessage() for record in logs])
    assert current_operation() is None


def test_async_task_submission_is_not_reported_as_completion(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success({"taskId": "install-123"})

    asyncio.run(guard_ipc_handler(state, "game_install", handler)({}))
    # 实际执行方负责记录执行阶段，IPC 受理不能再造一条成功或完成。
    assert logs == []


def test_poll_failure_sampling_and_recovery(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())
    replies = [failure("失败", "A"), failure("失败", "A"), failure("失败", "B"), success({}), success({})]

    async def handler(body):
        return replies.pop(0)

    guarded = guard_ipc_handler(state, "system_ping", handler)
    for _ in range(5):
        asyncio.run(guarded({}))
    assert len(logs) == 3
    assert "已恢复" in logs[-1].getMessage()


def test_poll_exception_has_one_stack_and_no_repeated_failure(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    @_ipc_handler()
    async def system_ping(self, body):
        raise RuntimeError("boom")

    guarded = guard_ipc_handler(state, "system_ping", lambda body: system_ping(state, body))
    for _ in range(2):
        assert asyncio.run(guarded({}))["errorCode"] == "INTERNAL_ERROR"
    assert len(logs) == 1
    assert logs[0].exc_info is not None


def test_poll_failure_interval_expiry(monkeypatch) -> None:
    runtime = IpcLogRuntime()
    monkeypatch.setattr("ECL.api.operation_logging.monotonic", lambda: 1.0)
    assert runtime.record_failure("poll", "A")
    assert not runtime.record_failure("poll", "A")
    monkeypatch.setattr("ECL.api.operation_logging.monotonic", lambda: 32.0)
    assert runtime.record_failure("poll", "A")


@pytest.mark.parametrize("command", ["frontend_log", "logs_get_history"])
def test_log_channels_do_not_log_their_own_invocation(records, command) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success({})

    asyncio.run(guard_ipc_handler(state, command, handler)({}))
    assert logs == []


def test_timeout_does_not_claim_background_thread_stopped(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        await asyncio.Event().wait()

    result = asyncio.run(guard_ipc_handler(state, "connector_join", handler, timeout=0.01)({}))
    assert result["errorCode"] == "OPERATION_TIMEOUT"
    assert any("后台执行是否停止由执行方确认" in record.getMessage() for record in logs)
    assert not any("超时，已取消" in record.getMessage() for record in logs)


def test_request_cancellation_restores_context(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(guard_ipc_handler(state, "connector_join", handler)({}))
    assert "请求已取消" in logs[-1].getMessage()
    assert current_operation() is None


@pytest.mark.parametrize(
    "directory",
    [
        r"C:/Users/Alice/private",
        r"C:/Users/Alice Smith/private",
        r"C:\Users\Alice\private",
        "/home/alice/private",
        "/Users/alice/private",
    ],
)
def test_redaction_preserves_protocol_and_external_equal_signs(directory) -> None:
    message = safe_log_text(
        directory
        + ' password="secret with spaces" access_token=abc https://user:pass@host KEY=VALUE tcp://relay.test:1 U/AAAA-BBBB-CCCC-DDDD'
    )
    assert "Alice" not in message and "alice" not in message
    assert "secret" not in message and "abc" not in message and "user:pass" not in message
    assert "AAAA" not in message
    assert "KEY=VALUE" in message and "tcp://relay.test:1" in message


def test_exception_formatter_redacts_stack(records) -> None:
    logger, logs = records
    try:
        raise ValueError('password="private value"')
    except ValueError:
        logger.exception("执行失败")
    rendered = ReadableLogFormatter().format(logs[0])
    assert "ValueError" in rendered and "private value" not in rendered


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_background_task_lifecycle_sampling_and_thread_context(tmp_path, records, outcome) -> None:
    logger, logs = records
    manager = OperationManager(tmp_path, EventBus(), max_workers=1)
    manager._logger = logger
    entered, release = Event(), Event()
    worker_actions = []

    def worker(context):
        worker_actions.append(current_operation().action)
        context.progress(1, "下载")
        context.progress(2, "已下载 2 个文件")
        entered.set()
        assert release.wait(3)
        context.check_cancelled()
        if outcome == "failed":
            raise ValueError("failure")
        return "done"

    try:
        with operation_scope("父操作"):
            submitted = manager.submit("resource_install", worker)
        assert entered.wait(3)
        if outcome == "cancelled":
            assert manager.cancel(submitted["operationId"])
        release.set()
        manager._operations[submitted["operationId"]].future.result(3)
        assert manager.get(submitted["operationId"])["status"] == outcome
        assert worker_actions == ["安装资源"]
        assert sum("后台任务进度" in record.getMessage() for record in logs) == 1
        text = "\n".join(record.getMessage() for record in logs)
        start_messages = [record.getMessage() for record in logs if "后台任务开始执行" in record.getMessage()]
        assert len(start_messages) == 1 and "耗时" not in start_messages[0]
        assert "任务已提交" not in text
        assert "来源操作" not in text
        assert "任务编号" not in text and "操作编号" not in text
        assert submitted["operationId"] not in text
        assert {"completed": "执行完成", "failed": "执行失败", "cancelled": "已取消并停止执行"}[outcome] in text
        assert (tmp_path / "operations" / (submitted["operationId"] + ".json")).is_file()
    finally:
        release.set()
        manager.close()


def test_project_log_templates_have_no_field_equals() -> None:
    root = Path(__file__).resolve().parents[1] / "ECL"
    violations = []
    for source in root.rglob("*.py"):
        if source.is_relative_to(root / "game") or source.is_relative_to(root / "services" / "florolding"):
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"info", "debug", "warning", "error", "exception"}
                and node.args
            ):
                message = node.args[0]
                if isinstance(message, ast.Constant) and isinstance(message.value, str) and "=" in message.value:
                    violations.append((source.name, node.lineno))
    assert violations == []


def test_plugin_command_timeout_keeps_worker_context_and_reports_real_result(records) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from ECL.plugins.manager.contracts import PluginCommandError
    from ECL.plugins.manager.registry import PluginRegistry

    logger, logs = records
    entered, release = Event(), Event()
    worker_actions = []

    def handler(**params):
        worker_actions.append(current_operation().action)
        entered.set()
        assert release.wait(3)
        return "done"

    with ThreadPoolExecutor(max_workers=1) as executor:
        state = SimpleNamespace(
            logger=logger,
            _command_executor=executor,
            _status={"demo": "enabled"},
            _candidate_map={},
            _plugins={"demo": SimpleNamespace(_commands={"run": handler})},
        )
        try:
            with operation_scope("插件调用"), pytest.raises(PluginCommandError):
                PluginRegistry.call_command(state, "demo:run", {"password": "never-log-params"}, timeout=0.01)
            assert entered.wait(3)
        finally:
            release.set()
    assert worker_actions == ["插件调用"]
    messages = [record.getMessage() for record in logs]
    assert any("后台线程可能仍在执行" in message for message in messages)
    assert "插件命令执行完成" in messages[-1]
    assert "never-log-params" not in str(messages)


def test_queued_task_cancellation_is_logged_after_future_stops(tmp_path, records) -> None:
    logger, logs = records
    manager = OperationManager(tmp_path, EventBus(), max_workers=1)
    manager._logger = logger
    entered, release = Event(), Event()

    def blocked(context):
        entered.set()
        assert release.wait(3)
        context.check_cancelled()

    try:
        first = manager.submit("resource_install", blocked)
        assert entered.wait(3)
        queued = manager.submit("resource_update", lambda context: pytest.fail("排队任务不得执行"))
        manager.close()
        assert manager.get(queued["operationId"])["status"] == "cancelled"
        assert any("未开始执行" in record.getMessage() for record in logs)
    finally:
        release.set()
        manager._operations[first["operationId"]].future.result(3)
        manager.close()


def test_validation_exception_does_not_dump_request_into_logs(records) -> None:
    from external_plugins.ecl_onlinegame.onlinegame.nodes import ConnectorNodeConfig

    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        ConnectorNodeConfig.model_validate(body)

    result = asyncio.run(guard_ipc_handler(state, "settings_set", handler)({"mode": "private-input", "nodes": []}))
    assert result["success"] is False
    assert any("未通过结构检查" in record.getMessage() for record in logs)
    assert "private-input" not in str([record.getMessage() for record in logs])
    assert all(record.exc_info is None for record in logs)


@pytest.mark.parametrize("timed", [False, True])
def test_stage_logs_duration_follows_timed_declaration(records, timed) -> None:
    logger, logs = records
    with operation_scope("保存设置", timed=timed) as trace:
        assert not hasattr(trace, "operation_id")
        trace.log(logger, "配置已保存")
    message = logs[-1].getMessage()
    if timed:
        assert message.startswith("配置已保存；耗时：")
        assert message.count("耗时：") == 1
    else:
        assert message == "配置已保存"
    assert "编号" not in message
    assert not hasattr(logs[-1], "operation_id")


def test_untimed_operation_omits_duration_from_nested_logs(records) -> None:
    logger, logs = records
    with operation_scope("读取设置"):
        logger.info("配置已读取")
    assert logs[-1].getMessage() == "配置已读取"


def test_timed_operation_appends_duration_to_nested_logs_once(records) -> None:
    logger, logs = records
    with operation_scope("读取图片", timed=True):
        logger.info("图片读取成功")
    message = logs[-1].getMessage()
    assert message.startswith("图片读取成功；耗时：")
    assert message.count("耗时：") == 1


def test_start_stage_never_outputs_duration(records) -> None:
    logger, logs = records
    with operation_scope("下载安装包", timed=True) as trace:
        trace.log(logger, "开始下载安装包", include_duration=False)
    assert logs[-1].getMessage() == "开始下载安装包"


def test_timed_query_success_is_silent_even_at_debug(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success()

    asyncio.run(guard_ipc_handler(state, "game_versions", handler)({}))
    assert logs == []


def test_ordinary_operation_logs_result_without_start(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success()

    asyncio.run(guard_ipc_handler(state, "user_agreement_save", handler)({}))
    assert len(logs) == 1
    assert logs[0].levelno == logging.INFO
    assert "成功" in logs[0].getMessage()
    assert "开始" not in logs[0].getMessage()


def test_nested_error_conversion_records_one_failure_and_one_stack(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    @_ipc_handler()
    async def settings_get(self, body):
        raise RuntimeError("boom")

    result = asyncio.run(guard_ipc_handler(state, "settings_get", lambda body: settings_get(state, body))({}))
    assert result["errorCode"] == "INTERNAL_ERROR"
    assert len(logs) == 1
    assert logs[0].exc_info is not None


def test_concurrent_failures_are_independent_and_each_recorded_once(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    @_ipc_handler("INVALID_REQUEST")
    async def settings_set(self, body):
        await asyncio.sleep(0)
        raise ValueError("受控失败")

    guarded = guard_ipc_handler(state, "settings_set", lambda body: settings_set(state, body))

    async def requests():
        return await asyncio.gather(guarded({}), guarded({}))

    assert all(not result["success"] for result in asyncio.run(requests()))
    assert len(logs) == 2
    assert all(record.exc_info is None for record in logs)
    assert all("错误码：INVALID_REQUEST" in record.getMessage() for record in logs)
    assert current_operation() is None


def test_service_result_from_worker_thread_is_not_repeated_by_ipc(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        await asyncio.to_thread(current_operation().log_result, logger, "实际操作已保存")
        return success()

    asyncio.run(guard_ipc_handler(state, "user_agreement_save", handler)({}))
    assert len(logs) == 1
    assert logs[0].getMessage() == "实际操作已保存"


@pytest.mark.parametrize("raises", [False, True])
def test_user_cancellation_is_info_without_stack(records, raises) -> None:
    from ECL.utils.errors import GameServiceError

    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        if raises:
            raise GameServiceError("用户取消", "LAUNCH_CANCELLED")
        return failure("用户取消", "LAUNCH_CANCELLED")

    response = asyncio.run(guard_ipc_handler(state, "game_launch", handler)({}))
    assert response["errorCode"] == "LAUNCH_CANCELLED"
    assert len(logs) == 1
    assert logs[0].levelno == logging.INFO
    assert logs[0].exc_info is None


def test_query_and_sync_success_are_silent_in_debug_file_and_frontend_history(tmp_path) -> None:
    import ECL.utils.logging as logging_module
    from ECL.utils.logging import LoggingRuntime, get_frontend_log_history

    events = EventBus()
    manager = LoggingRuntime(colored=False, data_path=tmp_path)
    manager.install_frontend_handler(events)
    try:
        state = SimpleNamespace(logger=manager.get_logger("FrontendApi"), events=events)

        async def handler(body):
            return success()

        for command in ("settings_get", "plugin_list", "plugin_notify_sidebar_state", "launcher_errors_ack"):
            asyncio.run(guard_ipc_handler(state, command, handler)({}))
        for log_handler in manager.get_logger().handlers:
            log_handler.flush()
        text = (tmp_path / "logs/EuoraCraft-Launcher.log").read_text(encoding="utf-8")
        assert "FrontendApi" not in text
        assert not any(item["logger"].endswith("FrontendApi") for item in get_frontend_log_history())
    finally:
        manager.shutdown()
        logging_module.FrontendLogRuntimeState.buffer = None


def test_synchronous_long_operation_retains_start_and_timed_result(records) -> None:
    logger, logs = records
    state = SimpleNamespace(logger=logger, events=EventBus())

    async def handler(body):
        return success()

    asyncio.run(guard_ipc_handler(state, "export_logs", handler)({}))
    assert len(logs) == 2
    assert logs[0].getMessage() == "开始导出启动器日志"
    assert "耗时" not in logs[0].getMessage()
    assert logs[1].getMessage().startswith("导出启动器日志成功；耗时：")


def test_directory_config_reads_and_noop_writes_are_silent(tmp_path, records) -> None:
    from ECL.services.game.scan import ScanCoordinator

    logger, logs = records
    state = SimpleNamespace(logger=logger, _lock=RLock(), _ecl_json_path=lambda root: tmp_path / "ecl.json")
    state.read_ecl_config = lambda root: ScanCoordinator.read_ecl_config(state, root)
    state.write_ecl_config = lambda root, value: ScanCoordinator.write_ecl_config(state, root, value)
    value = {"activeVersion": "private-version-name"}
    assert state.read_ecl_config(tmp_path) == {}
    assert logs == []

    state.write_ecl_config(tmp_path, value)
    assert len(logs) == 1 and logs[0].levelno == logging.INFO
    assert "private-version-name" not in logs[0].getMessage()
    logs.clear()
    assert state.read_ecl_config(tmp_path) == value
    state.write_ecl_config(tmp_path, value)
    assert ScanCoordinator.patch_ecl_config(state, tmp_path, value) == value
    assert logs == []
