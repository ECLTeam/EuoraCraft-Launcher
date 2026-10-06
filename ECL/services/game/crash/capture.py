# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：崩溃捕获：崩溃标记监测、信号判定与后台分析调度。
#
# 公开接口：
#   - class CrashRunSnapshot — 一次游戏运行结束时的崩溃判定输入快照。
#   - class CrashCapture — 监测崩溃标记并调度自动崩溃分析。
#       - handle_line(run_token, line) -> None — 扫描进程输出行并记录崩溃标记。
#       - finalize(snapshot) -> None — 结算一次运行：判定信号并按需调度分析。
#       - close() -> None — 停止接受新的分析任务并丢弃未决结果。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from ECL.foundation import EventBus
from ECL.utils import get_logger

if TYPE_CHECKING:
    from .analyzer import CrashAnalyzer


@dataclass(frozen=True, slots=True)
class CrashRunSnapshot:
    """
    一次游戏运行结束时的崩溃判定输入快照。

    快照由启动协调器在运行结算时构造，只携带崩溃判定所需的不可变数据，
    使崩溃捕获不依赖运行表的内部对象。

    :param run_token: 运行标识，用于关联崩溃标记状态
    :param action: 结束动作，``exited`` / ``stopped`` / ``launcher_closed``
    :param instance_id: 实例注册 ID，注册未完成时为 None
    :param version_id: 版本目录名称
    :param game_path: Minecraft 根目录
    :param game_directory: 启动参数实际使用的游戏目录
    :param started_wall_time: 进程创建时的墙上时钟时间戳
    :param output_lines: 进程输出环形缓冲的快照
    :param exit_code: 进程退出码，尚未退出时为 None
    :param stopping: 用户是否主动停止本次运行
    :param startup_complete: 是否已观察到启动完成信号
    :param crash_analysis_disabled: 本次运行是否禁用自动崩溃分析
    """

    run_token: str
    action: str
    instance_id: str | None
    version_id: str
    game_path: Path
    game_directory: Path
    started_wall_time: float
    output_lines: tuple[str, ...]
    exit_code: int | None
    stopping: bool
    startup_complete: bool
    crash_analysis_disabled: bool


class CrashCapture:
    """
    监测崩溃标记并调度自动崩溃分析。

    崩溃标记由进程输出行驱动，与运行表解耦；分析在专用执行器中运行，
    完成后通过 ``launcher:error`` 事件推送 ``kind=game_crash`` 的结果。
    服务关闭后不再接受新任务，进行中的分析结果被静默丢弃。
    """

    crash_log_markers = (
        "crash report saved to",
        "this crash report has been saved to",
        "could not save crash report",
        "/error]: unable to launch",
        "an exception was thrown, the game will display an error screen and halt",
        "exception_access_violation",
    )

    def __init__(self, analyzer: CrashAnalyzer, events: EventBus, executor: ThreadPoolExecutor) -> None:
        self.logger = get_logger("CrashCapture")
        self._analyzer = analyzer
        self._events = events
        self._executor = executor
        self._marked_runs: set[str] = set()
        self._futures: set[Future] = set()
        self._closed = False
        self._lock = RLock()

    def handle_line(self, run_token: str, line: str) -> None:
        """
        扫描一行进程输出并记录崩溃标记。

        :param run_token: 输出所属的运行标识
        :param line: 已去除行尾换行的输出行
        """
        folded = str(line or "").casefold()
        if not any(marker in folded for marker in self.crash_log_markers):
            return
        with self._lock:
            if not self._closed:
                self._marked_runs.add(run_token)

    def finalize(self, snapshot: CrashRunSnapshot) -> None:
        """
        结算一次运行：清除标记状态并按信号判定是否调度分析。

        :param snapshot: 运行结束快照
        """
        with self._lock:
            marked = snapshot.run_token in self._marked_runs
            self._marked_runs.discard(snapshot.run_token)
            closed = self._closed
        detected_by = self._detection_signals(snapshot, marked)
        if closed or not detected_by or snapshot.crash_analysis_disabled:
            return
        self._schedule(snapshot, detected_by)

    def close(self) -> None:
        """
        停止接受新的分析任务；已提交的分析完成后不再推送事件。
        """
        with self._lock:
            self._closed = True
            self._marked_runs.clear()

    @staticmethod
    def _detection_signals(snapshot: CrashRunSnapshot, marked: bool) -> list[str]:
        # 既有语义：只有正常退出路径参与判定；用户主动停止、未退出或
        # 注册未完成的运行不触发崩溃分析。
        if snapshot.action != "exited" or snapshot.stopping or snapshot.exit_code is None:
            return []
        signals = []
        if snapshot.exit_code != 0:
            signals.append("exit_code")
        if marked:
            signals.append("crash_log")
        if not snapshot.startup_complete:
            signals.append("startup_incomplete")
        return signals

    def _schedule(self, snapshot: CrashRunSnapshot, detected_by: list[str]) -> None:
        # 文件收集与规则分析交给专用执行器；完成回调负责推送事件。
        with self._lock:
            if self._closed:
                return
            future = self._executor.submit(
                self._analyzer.analyze_runtime,
                version_id=snapshot.version_id,
                game_path=snapshot.game_path,
                game_directory=snapshot.game_directory,
                started_wall_time=snapshot.started_wall_time,
                output_lines=list(snapshot.output_lines),
                exit_code=int(snapshot.exit_code or 0),
                detected_by=detected_by,
            )
            self._futures.add(future)
        future.add_done_callback(self._analysis_done(snapshot))

    def _analysis_done(self, snapshot: CrashRunSnapshot) -> Callable[[Future], None]:
        def done(completed: Future) -> None:
            with self._lock:
                self._futures.discard(completed)
                closed = self._closed
            if closed or completed.cancelled():
                return
            try:
                result = completed.result()
            except Exception:
                error_id = uuid4().hex
                self.logger.exception(
                    "Minecraft 崩溃分析失败：版本：%s；错误编号：%s",
                    snapshot.version_id,
                    error_id,
                )
                self._events.emit(
                    "launcher:error",
                    {
                        "error_id": error_id,
                        "title": "Minecraft 实例崩溃",
                        "message": f"实例“{snapshot.version_id}”异常退出，但崩溃报告生成失败",
                    },
                )
                return
            report_id = str(result["reportId"])
            exit_code = result.get("exitCode")
            self._events.emit(
                "launcher:error",
                {
                    "error_id": report_id,
                    "title": "Minecraft 实例崩溃",
                    "message": f"实例“{snapshot.version_id}”异常退出，退出码：{exit_code}",
                    "kind": "game_crash",
                    "crash": result,
                },
            )
            self.logger.warning(
                "Minecraft 崩溃分析完成：版本：%s；退出码：%s；报告编号：%s",
                snapshot.version_id,
                exit_code,
                report_id,
            )

        return done


__all__ = ["CrashCapture", "CrashRunSnapshot"]
