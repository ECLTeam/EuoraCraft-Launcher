# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：日志体系：控制台彩色格式化、文件落盘与前端日志缓冲。
#
# 公开接口：
#   - class LoggingPolicy — 保存日志根名称和 ANSI 输出样式。
#   - class ColoredFormatter — 为交互式终端中的日志级别和消息添加 ANSI 颜色。
#       - format(record) -> str — 格式化日志副本，避免修改随后写入文件的原始记录。
#   - resolve_log_level(name) -> int — 将配置中的日志级别名称解析为标准库日志级别。
#   - class FrontendLogHandler — 把结构化日志同步转发到事件总线，并保留最近日志供前端补全历史。
#       - emit(record) -> None — 序列化一条日志记录到环形缓冲并发布到事件总线。
#   - class LoggingRuntime — 管理一次应用运行所使用的控制台与滚动文件处理器。
#       - get_logger(name=…) -> logging.Logger — 返回启动器根日志器或其命名子日志器。
#       - set_level(level) -> None — 调整控制台详细程度，同时保留文件中的 Debug 诊断记录。
#       - install_frontend_handler(events, history_limit=…) -> None — 挂接一个把日志转发到前端并保留最近历史的处理器。
#       - shutdown() -> None — 刷新并关闭本次运行创建的全部日志处理器。
#   - configure_logging(data_path, colored=…) -> LoggingRuntime — 为一次启动器运行配置统一日志输出。
#   - get_logger(name=…) -> logging.Logger — 获取由组合入口配置的启动器日志器。
#   - get_frontend_log_history() -> list[dict[str, Any]] — 返回最近推送给前端的日志记录快照，供终端打开时补全历史。
# ============================================================

from __future__ import annotations

import gzip
import logging
import logging.handlers
import shutil
from collections import deque
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from ECL.utils.operation_logging import OperationLogFilter, ReadableLogFormatter

if TYPE_CHECKING:
    from ECL.foundation.event_bus import EventBus


class LoggingPolicy:
    """
    保存日志根名称和 ANSI 输出样式。
    """

    logger_name = "EuoraCraft-Launcher"
    colors = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    reset = "\033[0m"


class ColoredFormatter(ReadableLogFormatter):
    """
    为交互式终端中的日志级别和消息添加 ANSI 颜色。

    文件日志始终使用无颜色格式，保证归档文件可被普通文本工具读取。
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        格式化日志副本，避免修改随后写入文件的原始记录。

        :param record: 标准库创建的日志记录
        :return: 带终端颜色的日志文本
        """
        copy = logging.makeLogRecord(record.__dict__.copy())
        color = LoggingPolicy.colors.get(copy.levelname)
        if color:
            copy.levelname = f"{color}\033[1m{copy.levelname:8s}{LoggingPolicy.reset}"
            copy.msg = f"{color}{copy.msg}{LoggingPolicy.reset}"
        return super().format(copy)


def _gzip_rotator(source: str, destination: str) -> None:
    # 将轮转后的日志压缩为 gzip 归档。
    with Path(source).open("rb") as source_file, gzip.open(destination, "wb") as destination_file:
        shutil.copyfileobj(source_file, destination_file)
    Path(source).unlink()


def resolve_log_level(name: str | None) -> int:
    """
    将配置中的日志级别名称解析为标准库日志级别。

    :param name: 小写或大写级别名（debug/info/warning/error）
    :return: logging 模块的级别值，无法识别时回退为 INFO
    """
    if isinstance(name, str):
        level = logging.getLevelName(name.upper())
        if isinstance(level, int):
            return level
    return logging.INFO


class FrontendLogRuntimeState:
    """
    保存前端日志缓冲的进程级运行状态。
    """

    buffer: deque[dict[str, Any]] | None = None
    buffer_lock = RLock()


class FrontendLogHandler(logging.Handler):
    """
    把结构化日志同步转发到事件总线，并保留最近日志供前端补全历史。

    :param events: 承载 ``launcher:log`` 事件的事件总线
    :param buffer: 存放最近日志的环形缓冲
    """

    def __init__(self, events: EventBus, buffer: deque[dict[str, Any]]) -> None:
        super().__init__()
        self._events = events
        self._buffer = buffer
        self.setFormatter(logging.Formatter())

    def emit(self, record: logging.LogRecord) -> None:
        """
        序列化一条日志记录到环形缓冲并发布到事件总线。
        """
        entry = {
            "time": self.formatter.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "filename": record.filename,
            "lineno": record.lineno,
            "message": record.getMessage(),
        }
        with FrontendLogRuntimeState.buffer_lock:
            self._buffer.append(entry)
        with suppress(Exception):
            self._events.emit("launcher:log", entry)


class LoggingRuntime:
    """
    管理一次应用运行所使用的控制台与滚动文件处理器。

    完整日志文件始终接收 ``DEBUG`` 及以上记录；启动器的 Debug 开关只控制控制台
    是否展示诊断信息，避免关闭开关后丢失复现问题所需的文件日志。

    :param data_path: 启动器数据目录，日志写入其 ``logs`` 子目录
    :param colored: 控制台是否使用 ANSI 颜色
    """

    def __init__(self, data_path: Path, colored: bool = True) -> None:
        # 创建日志目录并安装本次运行独占的处理器。
        self.data_path = Path(data_path)  # 启动器数据目录。
        self.log_dir = self.data_path / "logs"  # 当前运行的日志目录。
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.root_logger = logging.getLogger(LoggingPolicy.logger_name)  # 应用根日志器。
        self.root_logger.setLevel(logging.DEBUG)
        self.root_logger.propagate = False
        self._replace_handlers(colored)

    def _replace_handlers(self, colored: bool) -> None:
        # 关闭旧处理器，再安装本次运行所需的处理器。
        for handler in tuple(self.root_logger.handlers):
            handler.close()
            self.root_logger.removeHandler(handler)
        plain = ReadableLogFormatter(
            "%(asctime)s [%(levelname)s] [%(name)s] [%(filename)s:%(lineno)d] - %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(
            ColoredFormatter(
                "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] - %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
            if colored
            else plain
        )
        self._console_handler = console
        self.root_logger.addHandler(console)
        self.root_logger.addHandler(self._file_handler("EuoraCraft-Launcher.log", logging.DEBUG, plain))
        self.root_logger.addHandler(self._file_handler("error.log", logging.ERROR, plain))
        for handler in self.root_logger.handlers:
            handler.addFilter(OperationLogFilter())

    def _file_handler(
        self,
        filename: str,
        level: int,
        formatter: logging.Formatter,
    ) -> logging.handlers.TimedRotatingFileHandler:
        # 创建按天轮转并压缩归档的文件处理器。
        handler = logging.handlers.TimedRotatingFileHandler(
            self.log_dir / filename,
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        handler.namer = lambda name: f"{name}.gz"
        handler.rotator = _gzip_rotator
        handler.setLevel(level)
        handler.setFormatter(formatter)
        return handler

    def get_logger(self, name: str | None = None) -> logging.Logger:
        """
        返回启动器根日志器或其命名子日志器。

        :param name: 可选的组件名称
        :return: 继承统一处理器配置的日志器
        """
        return self.root_logger.getChild(name) if name else self.root_logger

    def set_level(self, level: int) -> None:
        """
        调整控制台详细程度，同时保留文件中的 Debug 诊断记录。

        :param level: 控制台最低日志级别
        """
        self.root_logger.setLevel(logging.DEBUG)
        self._console_handler.setLevel(level)

    def install_frontend_handler(self, events: EventBus, history_limit: int = 500) -> None:
        """
        挂接一个把日志转发到前端并保留最近历史的处理器。

        幂等：只安装一次，重复调用不会产生多个处理器。

        :param events: 用于中转 ``launcher:log`` 事件的事件总线
        :param history_limit: 历史日志缓冲的最大条数
        """
        if FrontendLogRuntimeState.buffer is not None:
            return
        with FrontendLogRuntimeState.buffer_lock:
            if FrontendLogRuntimeState.buffer is not None:
                return
            buffer: deque[dict[str, Any]] = deque(maxlen=history_limit)
            handler = FrontendLogHandler(events, buffer)
            handler.addFilter(OperationLogFilter())
            self.root_logger.addHandler(handler)
            FrontendLogRuntimeState.buffer = buffer

    def shutdown(self) -> None:
        """
        刷新并关闭本次运行创建的全部日志处理器。
        """
        for handler in tuple(self.root_logger.handlers):
            with suppress(OSError, RuntimeError):
                handler.flush()
                handler.close()
            self.root_logger.removeHandler(handler)


def configure_logging(data_path: Path, colored: bool = True) -> LoggingRuntime:
    """
    为一次启动器运行配置统一日志输出。

    :param data_path: 启动器数据目录
    :param colored: 控制台是否使用 ANSI 颜色
    :return: 负责日志处理器生命周期的运行对象
    """
    return LoggingRuntime(data_path, colored)


def get_logger(name: str | None = None) -> logging.Logger:
    """
    获取由组合入口配置的启动器日志器。

    :param name: 可选的组件名称
    :return: 启动器根日志器或其命名子日志器
    """
    logger = logging.getLogger(LoggingPolicy.logger_name)
    return logger.getChild(name) if name else logger


def get_frontend_log_history() -> list[dict[str, Any]]:
    """
    返回最近推送给前端的日志记录快照，供终端打开时补全历史。

    :return: 结构化的日志记录列表，未安装处理器时为空列表
    """
    with FrontendLogRuntimeState.buffer_lock:
        buffer = FrontendLogRuntimeState.buffer
        return list(buffer) if buffer is not None else []


__all__ = [
    "LoggingRuntime",
    "configure_logging",
    "get_frontend_log_history",
    "get_logger",
    "resolve_log_level",
]
