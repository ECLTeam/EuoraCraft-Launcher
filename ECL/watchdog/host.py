# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：宿主侧看门狗会话：拉起看门狗子进程、转发事件循环心跳并负责回收。
#
# 公开接口：
#   - class WatchdogSession — 看门狗子进程的宿主侧生命周期与心跳转发。
#       - start() -> bool — 启动看门狗；失败只告警，不阻塞启动器。
#       - notify_alive() -> None — 事件循环侧存活信号，非阻塞。
#       - stop() -> None — 幂等停止并回收看门狗子进程。
# ============================================================

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from time import monotonic
from uuid import uuid4

from ECL.plugins.contracts import PluginRuntimeError
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.utils import get_logger

from .protocol import FrameError, read_frame, write_frame

# 心跳发送间隔：远小于默认卡死超时，正常抖动不会触发误判。
heartbeat_interval_seconds = 10.0
# 事件循环停转判定阈值；宿主经 hello 帧下发给看门狗。
default_hang_timeout_seconds = 300
# 等待看门狗就绪帧的时间上限。
ready_deadline_seconds = 5.0
# 正常退出时等待看门狗自行结束的时间上限。
stop_grace_seconds = 2.0


class WatchdogSession:
    """
    持有看门狗子进程的宿主侧生命周期，把事件循环存活信号转发为管道心跳。

    看门狗是尽力而为的安全网：启动失败只记录告警并降级为无看门狗运行，
    任何看门狗异常都不允许影响启动器自身行为。
    """

    def __init__(
        self,
        report_path: Path,
        *,
        timeout_seconds: int = default_hang_timeout_seconds,
        executable: ExecutableLaunchSpec | None = None,
    ) -> None:
        """
        建立尚未启动的看门狗会话。

        :param report_path: 看门狗卡死报告的落盘路径，经 hello 帧下发给子进程
        :param timeout_seconds: 事件循环停转判定阈值，须在看门狗合法范围内
        :param executable: 测试指定的真实发行入口；None 解析当前宿主
        """
        self.report_path = report_path  # 卡死报告追加目标文件
        self.timeout_seconds = timeout_seconds  # 下发给看门狗的停转阈值
        self._spec = executable
        self.logger = get_logger("WatchdogSession")
        self._proc: subprocess.Popen[bytes] | None = None
        self._job_api: object | None = None  # Windows 上持有 Job 句柄的 API 对象
        self._job_handle: int = 0  # Windows 上宿主持有的 KILL_ON_JOB_CLOSE Job 句柄
        self._frames: queue.Queue[dict[str, object] | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._writer: threading.Thread | None = None
        self._last_alive = 0.0
        self._last_sent = 0.0
        self._started = False

    def start(self) -> bool:
        """
        启动看门狗子进程并完成握手；任何失败都降级为无看门狗运行。

        :return: 看门狗已就绪并开始接受心跳时为 True
        """
        if self._started:
            return self._proc is not None
        self._started = True
        try:
            spec = self._spec if self._spec is not None else ExecutableLaunchSpec.current()
            arguments = (
                *spec.executable_prefix(),
                "--headless",
                "--role",
                "watchdog",
                "--host-pid",
                str(os.getpid()),
                "--run-id",
                uuid4().hex,
            )
            # Windows 冻结或控制台入口下禁止弹出子进程控制台窗口。
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=spec.working_directory(),
                env=spec.environment(),
                creationflags=creationflags,
            )
            self._attach_job_guard()
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            self._send_hello()
            if not self._await_ready():
                self.stop()
                return False
            self._writer = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._writer.start()
            self.logger.debug("看门狗已启动：pid %s；超时 %s 秒", self._proc.pid, self.timeout_seconds)
            return True
        except (OSError, ValueError, PluginRuntimeError) as failure:
            self.logger.warning("看门狗启动失败，本次运行不启用看门狗：%s", failure)
            self.stop()
            return False

    def _read_loop(self) -> None:
        """
        在独立线程中持续读取看门狗帧；管道失效时以 None 结束读取。

        管道是外部边界：子进程退出可能在读取中触发任意 io 异常，
        统一转换为 EOF 哨兵，不把异常泄漏给握手流程。
        """
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                self._frames.put(read_frame(self._proc.stdout))
        except BaseException:
            self._frames.put(None)

    def _attach_job_guard(self) -> None:
        """
        Windows 上把看门狗挂入 KILL_ON_JOB_CLOSE 的 Job，宿主死亡时连带回收。

        分配发生在进程已启动之后：毫秒级的未监管窗口对安全网定位没有影响；
        挂载失败时终止子进程并降级为无看门狗运行。
        """
        if sys.platform != "win32":
            return
        from ECL.plugins.sdk._windows import WindowsApi

        assert self._proc is not None
        api = WindowsApi()
        handle = api.create_job()
        try:
            # Popen 未公开进程句柄，此句柄只用于 AssignProcessToJobObject。
            api.assign(handle, self._proc._handle)
        except BaseException:
            api.close(handle)
            raise
        self._job_api = api
        self._job_handle = handle

    def _send_hello(self) -> None:
        """
        下发监控参数：卡死阈值与报告路径。
        """
        assert self._proc is not None and self._proc.stdin is not None
        with self._lock:
            write_frame(
                self._proc.stdin,
                {
                    "hello": {
                        "timeout_seconds": self.timeout_seconds,
                        "log_path": str(self.report_path),
                    }
                },
            )

    def _await_ready(self) -> bool:
        """
        等待看门狗就绪帧，超时或协议不符都视为启动失败。
        """
        deadline = monotonic() + ready_deadline_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty:
                continue
            if frame is None:
                return False
            return frame.get("ready") is True and isinstance(frame.get("pid"), int)

    def notify_alive(self) -> None:
        """
        记录一次事件循环侧存活信号；只更新时间戳，不做任何阻塞操作。
        """
        with self._lock:
            self._last_alive = monotonic()

    def _heartbeat_loop(self) -> None:
        """
        周期检查事件循环存活信号，只有出现新信号才写入心跳帧。

        管道失效时静默结束写线程：宿主继续正常运行，监管职责交还给看门狗
        侧的 EOF 处理。管道是外部边界，写入中的任意 io 异常都等价于失联。
        """
        while not self._stop_event.wait(heartbeat_interval_seconds):
            with self._lock:
                alive = self._last_alive
                proc = self._proc
                stream = proc.stdin if proc is not None else None
                if stream is None or alive <= self._last_sent:
                    continue
            try:
                with self._lock:
                    write_frame(stream, {"heartbeat": True})
                    self._last_sent = alive
            except BaseException:
                return

    def stop(self) -> None:
        """
        幂等停止看门狗：先请求自行退出，超过宽限期再强制回收。
        """
        self._stop_event.set()
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            self._request_shutdown(proc)
        self._reap_process(proc)
        if self._job_api is not None and self._job_handle:
            # 子进程已自行退出时仍需释放句柄，避免宿主生存期内泄漏 Job。
            self._job_api.close(self._job_handle)
            self._job_api = None
            self._job_handle = 0
        if self._writer is not None:
            self._writer.join(1)
            self._writer = None
        if self._reader is not None:
            self._reader.join(1)
            self._reader = None
        self._proc = None

    def _request_shutdown(self, proc: subprocess.Popen[bytes]) -> None:
        """
        发送 shutdown 帧并关闭宿主侧 stdin，管道失效不改变回收流程。
        """
        try:
            with self._lock:
                write_frame(proc.stdin, {"shutdown": True})
        except (FrameError, OSError):
            pass
        with self._lock, suppress(OSError):
            proc.stdin.close()

    def _reap_process(self, proc: subprocess.Popen[bytes] | None) -> None:
        """
        等待看门狗退出；超过宽限期用 Job 或强杀回收，最后释放管道。
        """
        if proc is None:
            return
        try:
            proc.wait(stop_grace_seconds)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32" and self._job_api is not None and self._job_handle:
                # KILL_ON_JOB_CLOSE：关闭宿主持有的最后一个 Job 句柄即终止成员。
                self._job_api.close(self._job_handle)
                self._job_api = None
                self._job_handle = 0
            else:
                proc.kill()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(1)
        if proc.stdout is not None:
            with self._lock, suppress(OSError):
                proc.stdout.close()


__all__ = ["WatchdogSession"]
