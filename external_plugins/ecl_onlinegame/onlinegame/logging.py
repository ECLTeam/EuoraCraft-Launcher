# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：关联联机插件线程与回调的操作日志。
#
# 公开接口：
#   - class OperationTrace — 插件业务接口。
#   - current_operation() — 有界业务操作。
#   - trace_scope() — 有界业务操作。
#   - operation_scope() — 有界业务操作。
# ============================================================

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic


@dataclass(frozen=True, slots=True)
class OperationTrace:
    """
    在插件自己的线程和回调之间传递阶段及耗时。
    """

    name: str
    started: float = field(default_factory=monotonic)

    def log(
        self, logger: logging.Logger, message: str, level: int = logging.INFO, *, include_duration: bool = True
    ) -> None:
        """
        记录阶段摘要，不包含房间码、账户或路径。
        """
        if include_duration:
            logger.log(level, "%s；耗时：%.0f 毫秒", message, (monotonic() - self.started) * 1000)
        else:
            logger.log(level, "%s", message)


current_trace: ContextVar[OperationTrace | None] = ContextVar("onlinegame_operation", default=None)


def current_operation() -> OperationTrace | None:
    """
    返回当前线程的联机操作上下文。
    """
    return current_trace.get()


@contextmanager
def trace_scope(trace: OperationTrace | None) -> Iterator[None]:
    """
    在回调执行后恢复之前的上下文。
    """
    token = current_trace.set(trace)
    try:
        yield
    finally:
        current_trace.reset(token)


@contextmanager
def operation_scope(name: str, *, timed: bool = True) -> Iterator[OperationTrace]:
    """
    为一次真实操作建立插件私有的阶段上下文。
    """
    trace = OperationTrace(name)
    with trace_scope(trace):
        yield trace
