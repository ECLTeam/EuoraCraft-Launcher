# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 crash 崩溃捕获模块的自动化测试。
#
# 公开接口：
#   - test_finalize_dispatches_expected_detection_signals(tmp_path) -> None
#   - test_marker_is_discarded_after_finalize(tmp_path) -> None
#   - test_analysis_result_emits_game_crash_event(tmp_path) -> None
#   - test_analysis_failure_emits_error_event(tmp_path) -> None
#   - test_closed_capture_skips_scheduling(tmp_path) -> None
# ============================================================

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ECL.events import EventBus
from ECL.services.game.crash.capture import CrashCapture, CrashRunSnapshot


class FakeAnalyzer:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def analyze_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "reportId": "report-1",
            "versionId": kwargs.get("version_id"),
            "exitCode": kwargs.get("exit_code"),
            "detectedBy": list(kwargs.get("detected_by") or []),
            "reasons": [],
            "sourceFiles": [],
            "hasOutput": False,
        }


def _snapshot(**overrides: Any) -> CrashRunSnapshot:
    values: dict[str, Any] = {
        "run_token": "token-1",
        "action": "exited",
        "instance_id": "instance-1",
        "version_id": "1.21.8",
        "game_path": Path(".minecraft"),
        "game_directory": Path(".minecraft/versions/1.21.8"),
        "started_wall_time": 100.0,
        "output_lines": ("boom",),
        "exit_code": 1,
        "stopping": False,
        "startup_complete": True,
        "crash_analysis_disabled": False,
    }
    values.update(overrides)
    return CrashRunSnapshot(**values)


def _wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("等待超时")
        time.sleep(0.01)


def test_finalize_dispatches_expected_detection_signals(tmp_path: Path) -> None:
    analyzer = FakeAnalyzer()
    capture = CrashCapture(analyzer, EventBus(), ThreadPoolExecutor(max_workers=1))
    try:
        capture.finalize(_snapshot())
        _wait_until(lambda: len(analyzer.calls) == 1)
        assert analyzer.calls[0]["detected_by"] == ["exit_code"]

        capture.handle_line("token-2", "This crash report has been saved to: crash-reports/x.txt")
        capture.finalize(_snapshot(run_token="token-2", exit_code=0))
        _wait_until(lambda: len(analyzer.calls) == 2)
        assert analyzer.calls[1]["detected_by"] == ["crash_log"]

        capture.finalize(_snapshot(run_token="token-3", exit_code=0, startup_complete=False))
        _wait_until(lambda: len(analyzer.calls) == 3)
        assert analyzer.calls[2]["detected_by"] == ["startup_incomplete"]

        for snapshot in (
            _snapshot(run_token="token-4", action="stopped"),
            _snapshot(run_token="token-5", stopping=True),
            _snapshot(run_token="token-6", crash_analysis_disabled=True),
        ):
            capture.finalize(snapshot)
        time.sleep(0.05)
        assert len(analyzer.calls) == 3
    finally:
        capture.close()


def test_marker_is_discarded_after_finalize(tmp_path: Path) -> None:
    analyzer = FakeAnalyzer()
    capture = CrashCapture(analyzer, EventBus(), ThreadPoolExecutor(max_workers=1))
    try:
        capture.handle_line("token-1", "Could not save crash report")
        capture.finalize(_snapshot(run_token="token-1", action="stopped"))
        capture.finalize(_snapshot(run_token="token-1", exit_code=1))
        _wait_until(lambda: len(analyzer.calls) == 1)
        assert analyzer.calls[0]["detected_by"] == ["exit_code"]
    finally:
        capture.close()


def test_analysis_result_emits_game_crash_event(tmp_path: Path) -> None:
    events = EventBus()
    emitted: list[dict] = []
    events.subscribe("launcher:error", emitted.append)
    capture = CrashCapture(FakeAnalyzer(), events, ThreadPoolExecutor(max_workers=1))
    try:
        capture.finalize(_snapshot())
        _wait_until(lambda: len(emitted) == 1)
        assert emitted[0]["kind"] == "game_crash"
        assert emitted[0]["error_id"] == "report-1"
        assert emitted[0]["crash"]["reportId"] == "report-1"
    finally:
        capture.close()


def test_analysis_failure_emits_error_event(tmp_path: Path) -> None:
    events = EventBus()
    emitted: list[dict] = []
    events.subscribe("launcher:error", emitted.append)
    capture = CrashCapture(FakeAnalyzer(error=RuntimeError("boom")), events, ThreadPoolExecutor(max_workers=1))
    try:
        capture.finalize(_snapshot())
        _wait_until(lambda: len(emitted) == 1)
        assert "kind" not in emitted[0]
        assert emitted[0]["error_id"]
        assert "崩溃报告生成失败" in emitted[0]["message"]
    finally:
        capture.close()


def test_closed_capture_skips_scheduling(tmp_path: Path) -> None:
    analyzer = FakeAnalyzer()
    capture = CrashCapture(analyzer, EventBus(), ThreadPoolExecutor(max_workers=1))
    capture.close()

    capture.handle_line("token-1", "exception_access_violation")
    capture.finalize(_snapshot())

    time.sleep(0.05)
    assert analyzer.calls == []
