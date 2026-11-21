# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：看门狗子进程入口：监控宿主心跳，超时先写卡死报告再终止宿主。
#
# 公开接口：
#   - run_watchdog(options) -> int — 执行看门狗监控循环并返回退出码。
# ============================================================

from __future__ import annotations

import ctypes
import os
import queue
import signal
import stat
import sys
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import NoReturn

from ECL.bootstrap import WatchdogOptions
from ECL.watchdog.protocol import FrameError, read_frame, write_frame

# 等待宿主 hello 帧的时间上限；超时视为连接不完整。
hello_deadline_seconds = 10.0
# 主循环轮询间隔；真实触发精度由宿主下发的超时值决定。
poll_interval_seconds = 0.5
# 卡死超时的合法范围：下限服务于测试的短超时，上限防止配置成永久不触发。
min_timeout_seconds = 1
max_timeout_seconds = 3600

# 退出码：0 正常结束；2 连接或协议校验失败；3 已执行卡死终止。
exit_code_ok = 0
exit_code_invalid = 2
exit_code_hang_terminated = 3


def _read_frames(frames: queue.Queue[dict[str, object] | None]) -> None:
    """
    在独立线程中持续读取宿主帧；任何终止条件都以 None 结束读取。

    管道是外部边界：宿主关闭管道可能在读取中触发任意 io 异常，
    统一转换为 EOF 哨兵，不把异常泄漏给监控循环。
    """
    try:
        while True:
            frames.put(read_frame(sys.stdin.buffer))
    except BaseException:
        frames.put(None)


def _finish(code: int) -> NoReturn:
    """
    以指定退出码直接结束进程，不进入解释器收尾。

    读取线程是阻塞在 stdin 上的守护线程；Windows 上正常收尾会与已关闭的
    管道句柄竞态并触发访问违例。本入口没有注册任何清理回调，报告文件与
    出站帧在决定退出前均已落盘或冲刷。
    """
    os._exit(code)


def _await_hello(frames: queue.Queue[dict[str, object] | None]) -> tuple[int, Path] | None:
    """
    等待并校验宿主 hello 帧，超时或 EOF 都不进入监控。

    :return: 卡死超时秒数与报告文件路径；连接不完整或格式不符为 None
    """
    deadline = monotonic() + hello_deadline_seconds
    while True:
        try:
            frame = frames.get(timeout=max(0.01, deadline - monotonic()))
        except queue.Empty:
            return None
        if frame is None:
            return None
        hello = frame.get("hello")
        if not isinstance(hello, dict):
            return None
        timeout = hello.get("timeout_seconds")
        raw_log_path = hello.get("log_path")
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not min_timeout_seconds <= timeout <= max_timeout_seconds
            or not isinstance(raw_log_path, str)
            or not raw_log_path
            or not Path(raw_log_path).is_absolute()
        ):
            return None
        return timeout, Path(raw_log_path)


def _write_hang_report(log_path: Path, timeout: int, host_pid: int) -> None:
    """
    在终止宿主前追加一条纯文本卡死报告，写入失败不阻止终止动作。
    """
    line = (
        f"{datetime.now().isoformat(timespec='seconds')} 看门狗判定启动器无响应："
        f"host_pid={host_pid} timeout_seconds={timeout} watchdog_pid={os.getpid()}\n"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as report_file:
            report_file.write(line)
    except OSError:
        pass


def _terminate_host(host_pid: int) -> None:
    """
    按平台终止宿主进程；失败时保留退出码语义，由退出码记录终止结果。
    """
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        process = kernel32.OpenProcess(0x0001, False, host_pid)  # PROCESS_TERMINATE
        if not process:
            return
        kernel32.TerminateProcess(process, 1)
        kernel32.CloseHandle(process)
        return
    if sys.platform in {"linux", "darwin"}:
        with suppress(OSError):
            os.kill(host_pid, signal.SIGKILL)


def _monitor(frames: queue.Queue[dict[str, object] | None], timeout: int, log_path: Path, host_pid: int) -> int:
    """
    监控心跳直到宿主退出、收到 shutdown 或心跳超时。

    :return: 0 正常结束；2 协议错乱；3 已执行卡死终止
    """
    last_heartbeat = monotonic()
    while True:
        frame: dict[str, object] | None = None
        has_frame = False
        try:
            frame = frames.get(timeout=poll_interval_seconds)
            has_frame = True
        except queue.Empty:
            pass
        if has_frame:
            if frame is None:
                # 宿主正常退出或已崩溃时管道关闭，看门狗静默退出，不做任何终止。
                return exit_code_ok
            if "shutdown" in frame:
                return exit_code_ok
            if "heartbeat" in frame and len(frame) == 1:
                last_heartbeat = monotonic()
            else:
                return exit_code_invalid
        if monotonic() - last_heartbeat > timeout:
            _write_hang_report(log_path, timeout, host_pid)
            _terminate_host(host_pid)
            return exit_code_hang_terminated


def run_watchdog(options: WatchdogOptions) -> int:
    """
    监控宿主心跳：干净 EOF、shutdown 帧静默退出；心跳超时写报告并终止宿主。

    本入口只依赖标准库与引导分流结果；宿主身份以父进程真实关系为准，
    不把连接参数视为授权凭据。

    :param options: 引导阶段已结构校验的宿主连接参数
    :return: 0 正常结束；2 连接或协议校验失败；3 已执行卡死终止
    """
    if sys.platform not in {"win32", "linux", "darwin"}:
        _finish(exit_code_invalid)
    if not stat.S_ISFIFO(os.fstat(0).st_mode) or not stat.S_ISFIFO(os.fstat(1).st_mode):
        _finish(exit_code_invalid)
    # 连接参数中的宿主 PID 必须与真实父进程一致，防止复用或误连。
    if os.getppid() != options.host_pid:
        _finish(exit_code_invalid)
    frames: queue.Queue[dict[str, object] | None] = queue.Queue()
    reader = threading.Thread(target=_read_frames, args=(frames,), daemon=True)
    reader.start()
    handshake = _await_hello(frames)
    if handshake is None:
        _finish(exit_code_invalid)
    timeout, log_path = handshake
    try:
        write_frame(sys.stdout.buffer, {"ready": True, "pid": os.getpid()})
    except (FrameError, OSError):
        _finish(exit_code_invalid)
    _finish(_monitor(frames, timeout, log_path, options.host_pid))


__all__ = ["run_watchdog"]
