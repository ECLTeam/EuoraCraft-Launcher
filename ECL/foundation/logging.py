# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：不依赖业务服务的日志脱敏与异常格式化，供桌面和无头入口共用。
#
# 公开接口：
#   - safe_log_text — 脱敏凭据、房间码与用户目录。
#   - class ReadableLogFormatter — 对日志和异常堆栈统一脱敏。
# ============================================================

from __future__ import annotations

import logging
import re


class LogTextPolicy:
    """
    保存凭据和用户目录的识别规则，不修改正常地址及外部输出的符号。
    """

    credentials: re.Pattern[str] = re.compile(
        r"(?i)((?:access[_-]?token|refresh[_-]?token|client[_-]?secret|password|authorization|"
        r"session[_-]?(?:token|key)|api[_-]?key|token)[\"']?\s*[:=]\s*)"
        r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?:Bearer\s+)?[^\s,;\"'&}]+)"
    )
    user_info: re.Pattern[str] = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
    user_directory: re.Pattern[str] = re.compile(
        r"(?i)(?:[a-z]:[\\/](?:Users|Documents and Settings)[\\/]|/Users/|/home/)[^\\/\r\n,;，；\"']+"
    )
    room_code: re.Pattern[str] = re.compile(r"\b[UZ]/[A-Za-z0-9]+(?:-[A-Za-z0-9]+){3}\b")


def safe_log_text(text: str) -> str:
    """
    脱敏常见凭据、地址认证信息和用户目录，保留业务说明。

    :param text: 项目生成的日志或异常文本
    :return: 可写入日志的文本；不改变非敏感地址、协议和外部输出中的等号
    """
    text = LogTextPolicy.credentials.sub(r"\1<已隐藏>", text)
    text = LogTextPolicy.user_info.sub(r"\1<已隐藏>@", text)
    text = LogTextPolicy.room_code.sub("<房间码已隐藏>", text)
    return LogTextPolicy.user_directory.sub("<用户目录>", text)


class ReadableLogFormatter(logging.Formatter):
    """
    保留现有日志头与异常堆栈，并对异常文本进行统一脱敏。
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        脱敏最终文本，覆盖其他处理器已经缓存的异常堆栈。

        :param record: 标准库日志记录，可能已经由其他处理器格式化
        :return: 保留堆栈结构的安全文本
        """
        return safe_log_text(super().format(record))

    def formatException(self, ei: object) -> str:  # noqa: N802 - 标准库接口
        """
        格式化异常信息，不把异常中的凭据或用户目录写入归档。

        :param ei: logging 提供的异常三元组
        :return: 脱敏后的堆栈文本
        """
        return safe_log_text(super().formatException(ei))
