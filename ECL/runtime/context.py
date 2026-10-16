# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：无头任务的路径与日志资源，不创建业务服务或共享持久化日志。
#
# 公开接口：
#   - class HeadlessContext — 拥有一次无头执行的精简资源。
# ============================================================

from __future__ import annotations

import logging
import sys
from typing import TextIO

from ECL.foundation.logging import ReadableLogFormatter
from ECL.foundation.runtime import RuntimeInfo, get_runtime_info
from ECL.runtime.contracts import HeadlessOptions


class _HeadlessStreamHandler(logging.StreamHandler):
    """
    人类日志输出失效时不通过 logging 默认错误处理泄露原始异常文本。

    日志流失效不改变任务结果的专用输出通道。
    """

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - 标准库接口
        """
        丢弃不可写标准错误流上的日志，避免递归报告同一输出错误。

        :param record: 输出失败的日志记录
        """
        return


class HeadlessContext:
    """
    保存任务运行路径和独立日志处理器，关闭时不影响调用者日志。

    数据路径只解析不创建。标准错误流不可用时丢弃人类日志，结构化结果由执行入口负责。
    """

    options: HeadlessOptions
    runtime_info: RuntimeInfo
    logger: logging.Logger
    _handler: logging.Handler
    _is_closed: bool

    def __init__(self, options: HeadlessOptions) -> None:
        """
        解析路径并创建只属于本次调用的日志器，不接入桌面文件或事件日志。

        :param options: 已校验的有限任务参数
        """
        self.options = options
        self.runtime_info = get_runtime_info(data_path_override=options.data_path)
        self.logger = logging.Logger("EuoraCraft-Launcher.Headless")
        self.logger.propagate = False
        level = "DEBUG" if options.is_debug else (options.log_level or "info").upper()
        self.logger.setLevel(level)
        stderr: TextIO | None = sys.stderr
        self._handler = _HeadlessStreamHandler(stderr) if stderr is not None else logging.NullHandler()
        self._handler.setFormatter(ReadableLogFormatter("%(levelname)s %(message)s"))
        self.logger.addHandler(self._handler)
        self._is_closed = False

    def close(self) -> None:
        """
        幂等释放自身日志处理器，不关闭调用者的标准流。
        """
        if self._is_closed:
            return
        self._is_closed = True
        self.logger.removeHandler(self._handler)
        try:
            self._handler.flush()
        except (OSError, ValueError):
            # stderr 由调用者或系统持有；关闭时失效不影响已交付的任务结果。
            self.logger.removeHandler(self._handler)
        finally:
            self._handler.close()
