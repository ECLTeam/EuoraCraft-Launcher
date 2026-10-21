# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：关联操作阶段日志，并为项目日志中的凭据和用户目录提供统一脱敏。
#
# 公开接口：
#   - class OperationTrace — 关联同一操作的日志与耗时。
#   - operation_scope — 记录当前操作的名称和开始时间，退出时恢复之前的记录。
#   - current_operation — 获取当前线程或异步任务正在记录的操作。
#   - trace_scope — 在退出回调中临时使用之前保存的操作记录。
#   - safe_log_text — 脱敏项目日志文本。
#   - class OperationLogFilter — 为日志记录补充脱敏消息，并在计时操作中附带耗时。
#   - class ReadableLogFormatter — 脱敏异常堆栈，保留原日志格式。
#   - private_transport_scope — 在当前上下文过滤可能包含原始网络数据的库日志。
# ============================================================

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

from ECL.foundation.logging import ReadableLogFormatter, safe_log_text

__all__ = [
    "OperationLogFilter",
    "OperationTrace",
    "ReadableLogFormatter",
    "current_operation",
    "operation_scope",
    "private_transport_scope",
    "safe_log_text",
    "trace_scope",
]


@dataclass(frozen=True, slots=True)
class OperationTrace:
    """
    保存操作名称和开始时间，供异步任务、线程和退出回调记录同一次操作。

    只有声明为计时的操作才输出累计耗时：本地轻量读写本身耗时趋近于零，
    逐行附带耗时只是噪声。开始阶段行恒为 0 毫秒，任何情况下都不输出耗时。
    """

    action: str
    started_at: float
    timed: bool = False

    def log(
        self,
        logger: logging.Logger,
        message: str,
        level: int = logging.INFO,
        *,
        include_duration: bool = True,
    ) -> None:
        """
        记录一个操作阶段；仅计时操作在需要时附带累计耗时。

        :param logger: 使用项目日志处理器的日志器
        :param message: 不包含请求体或凭据的阶段说明
        :param level: 标准库日志级别
        :param include_duration: 该阶段是否允许输出耗时；开始阶段行应传 False
        """
        if self.timed and include_duration:
            logger.log(
                level,
                "%s；耗时：%.0f 毫秒",
                safe_log_text(message),
                (monotonic() - self.started_at) * 1000,
                extra={"operation_action": self.action, "has_operation_duration": True},
            )
            return
        logger.log(
            level,
            "%s",
            safe_log_text(message),
            extra={"operation_action": self.action, "has_operation_duration": True},
        )


class OperationLogState:
    """
    保存当前异步任务或线程正在记录的操作。
    """

    current: ContextVar[OperationTrace | None] = ContextVar("ecl_operation_trace", default=None)
    is_private_transport: ContextVar[bool] = ContextVar("ecl_private_transport", default=False)


@contextmanager
def private_transport_scope() -> Iterator[None]:
    """
    仅过滤当前网络调用的传输日志，退出时恢复，不改变其他任务的日志级别。

    :return: 退出后恢复原过滤上下文
    """
    token = OperationLogState.is_private_transport.set(True)
    try:
        yield
    finally:
        OperationLogState.is_private_transport.reset(token)


def current_operation() -> OperationTrace | None:
    """
    获取当前操作的关联信息。

    :return: 当前操作的记录对象，没有正在记录的操作时返回 None
    """
    return OperationLogState.current.get()


@contextmanager
def trace_scope(trace: OperationTrace | None) -> Iterator[None]:
    """
    临时使用已保存的操作记录，退出时恢复之前的记录。

    :param trace: 创建任务或进程时保存的关联信息
    :return: 退出时恢复之前的操作记录
    """
    token = OperationLogState.current.set(trace)
    try:
        yield
    finally:
        OperationLogState.current.reset(token)


@contextmanager
def operation_scope(action: str, *, timed: bool = False) -> Iterator[OperationTrace]:
    """
    记录一次操作的名称和开始时间，结束时恢复之前的操作记录。

    :param action: 面向人的操作名称
    :param timed: 是否为网络或重型操作输出累计耗时
    :return: 可记录操作阶段和累计耗时的对象
    """
    trace = OperationTrace(action, monotonic(), timed)
    token = OperationLogState.current.set(trace)
    try:
        yield trace
    finally:
        OperationLogState.current.reset(token)


class OperationLogFilter(logging.Filter):
    """
    为同一条项目日志统一脱敏并补充关联信息，避免多个处理器重复处理。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        修改项目日志消息的内容，保留级别和前端协议字段。

        :param record: 标准库日志记录
        :return: 当前敏感传输上下文中的库日志为 False，其他日志照常脱敏输出
        """
        if OperationLogState.is_private_transport.get() and record.name.split(".", 1)[0] in {"httpcore", "httpx"}:
            return False
        if getattr(record, "is_operation_processed", False):
            return True
        message = safe_log_text(record.getMessage())
        trace = current_operation()
        if trace is not None and trace.timed and not getattr(record, "has_operation_duration", False):
            record.operation_action = trace.action
            record.has_operation_duration = True
            message = f"{message}；耗时：{(monotonic() - trace.started_at) * 1000:.0f} 毫秒"
        record.msg = message
        record.args = ()
        record.is_operation_processed = True
        return True
