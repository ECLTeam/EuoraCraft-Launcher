from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest

from ECL.runtime import headless, tasks
from ECL.runtime.context import HeadlessContext
from ECL.runtime.contracts import HeadlessExitCode, HeadlessOptions, HeadlessRuntimeError, HeadlessTask


@pytest.fixture
def task_options(monkeypatch) -> HeadlessOptions:
    # 全套 pytest 已加载桌面模块；真实导入边界由独立进程测试覆盖。
    monkeypatch.setattr(tasks.RuntimeCheckPolicy, "has_prohibited_modules", lambda: False)
    return HeadlessOptions(HeadlessTask.runtime_check)


def test_real_dependencies_and_json_are_separate_from_logs(task_options, capsys) -> None:
    assert headless.run_headless(task_options) == HeadlessExitCode.success
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["success"] and result["protocol_version"] == 1
    assert result["probe"]["has_ui_modules"] is False
    assert [item["name"] for item in result["probe"]["dependencies"]] == ["httpx", "pydantic", "psutil"]
    assert all(item["version"] for item in result["probe"]["dependencies"])
    assert "检查成功" in captured.err
    assert "path" not in result["probe"]


def test_context_owns_only_its_handlers_and_does_not_create_data(tmp_path) -> None:
    logger = logging.getLogger("EuoraCraft-Launcher")
    original_handlers = tuple(logger.handlers)
    data_path = tmp_path / "unused"
    context = HeadlessContext(HeadlessOptions(HeadlessTask.runtime_check, data_path, is_debug=True))
    assert context.runtime_info["data_path"] == data_path
    assert context.logger.level == logging.DEBUG
    handler = context._handler
    context.close()
    context.close()
    assert handler not in context.logger.handlers
    assert tuple(logger.handlers) == original_handlers
    assert not data_path.exists()


def test_data_override_wins_without_creating_either_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ECL_DATA_PATH", str(tmp_path / "environment"))
    context = HeadlessContext(HeadlessOptions(HeadlessTask.runtime_check, tmp_path / "cli"))
    try:
        assert context.runtime_info["data_path"] == tmp_path / "cli"
        assert list(tmp_path.iterdir()) == []
    finally:
        context.close()


@pytest.mark.parametrize(
    "error", [HeadlessRuntimeError("EXPECTED", "预期失败"), RuntimeError("password=secret"), KeyboardInterrupt()]
)
def test_failure_and_cancellation_output_and_close(task_options, error, monkeypatch, capsys) -> None:
    closed = []
    original_close = HeadlessContext.close

    def close(context):
        closed.append(context)
        original_close(context)

    def fail(context):
        raise error

    monkeypatch.setattr(HeadlessContext, "close", close)
    monkeypatch.setattr(headless, "execute_task", fail)
    expected = HeadlessExitCode.cancelled if isinstance(error, KeyboardInterrupt) else HeadlessExitCode.failed
    assert headless.run_headless(task_options) == expected
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert not result["success"]
    assert result["error_code"] in {"EXPECTED", "HEADLESS_TASK_FAILED", "HEADLESS_CANCELLED"}
    assert "secret" not in captured.err and "secret" not in captured.out
    assert len(closed) == 1 and not closed[0].logger.handlers


def test_initialization_failure_emits_result_and_redacted_stack(task_options, monkeypatch, capsys) -> None:
    def fail(options):
        raise RuntimeError("password=secret")

    monkeypatch.setattr(headless, "HeadlessContext", fail)
    assert headless.run_headless(task_options) == HeadlessExitCode.failed
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error_code"] == "HEADLESS_INITIALIZATION_FAILED"
    assert "Traceback" in captured.err and "secret" not in captured.err


def test_dependency_import_failure_is_observable(task_options, monkeypatch, capsys) -> None:
    def fail(name):
        raise ImportError("private path")

    monkeypatch.setattr(tasks.importlib, "import_module", fail)
    assert headless.run_headless(task_options) == HeadlessExitCode.failed
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error_code"] == "HEADLESS_DEPENDENCY_UNAVAILABLE"
    assert "private path" not in captured.out + captured.err


def test_import_boundary_failure_is_observable(task_options, monkeypatch, capsys) -> None:
    monkeypatch.setattr(tasks.RuntimeCheckPolicy, "has_prohibited_modules", lambda: True)
    assert headless.run_headless(task_options) == HeadlessExitCode.failed
    assert json.loads(capsys.readouterr().out)["error_code"] == "HEADLESS_IMPORT_BOUNDARY"


def test_file_output_works_without_standard_streams(tmp_path, task_options, monkeypatch) -> None:
    result_file = tmp_path / "nested" / "result.json"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    options = HeadlessOptions(task_options.task, result_file=result_file)
    assert headless.run_headless(options) == HeadlessExitCode.success
    assert json.loads(result_file.read_text(encoding="utf-8"))["success"]
    assert list(result_file.parent.iterdir()) == [result_file]


def test_existing_result_is_preserved(tmp_path, task_options) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text("original", encoding="utf-8")
    assert headless.run_headless(HeadlessOptions(task_options.task, result_file=result_file)) == HeadlessExitCode.failed
    assert result_file.read_text(encoding="utf-8") == "original"


def test_atomic_replace_failure_preserves_target_and_cleans_temporary(tmp_path, task_options, monkeypatch) -> None:
    result_file = tmp_path / "result.json"

    def fail(source, target):
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "replace", fail)
    assert headless.run_headless(HeadlessOptions(task_options.task, result_file=result_file)) == HeadlessExitCode.failed
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stream_kind", ["none", "closed", "broken"])
def test_unavailable_stdout_does_not_execute_task(task_options, monkeypatch, stream_kind) -> None:
    class BrokenStream(io.StringIO):
        def write(self, value):
            raise OSError("broken pipe")

    stream = None if stream_kind == "none" else BrokenStream() if stream_kind == "broken" else io.StringIO()
    if stream_kind == "closed":
        stream.close()
    monkeypatch.setattr(sys, "stdout", stream)

    def should_not_run(context):
        pytest.fail("输出不可用时不应执行任务")

    monkeypatch.setattr(headless, "execute_task", should_not_run)
    assert headless.run_headless(task_options) == HeadlessExitCode.failed


def test_broken_stderr_does_not_leak_or_break_result(task_options, monkeypatch, capsys) -> None:
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr(sys, "stderr", stream)
    assert headless.run_headless(task_options) == HeadlessExitCode.success
    assert json.loads(capsys.readouterr().out)["success"]
