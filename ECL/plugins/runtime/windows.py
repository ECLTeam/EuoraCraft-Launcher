# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：挂起创建 Worker、提前加入 Job，并按实际成员回收普通进程树。
#
# 公开接口：
#   - class WindowsProcess — 保留原生句柄和创建身份的受控进程。
#   - class WindowsJob — 不允许脱离的进程创建与回收边界。
# ============================================================

from __future__ import annotations

from ctypes import byref, wintypes
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic, sleep

from ECL.plugin_api._windows import WindowsApi, WindowsRuntimeError


@dataclass(slots=True)
class WindowsProcess:
    """
    保留进程身份与挂起线程，只有已分配到 Job 才能恢复。
    """

    api: WindowsApi
    handle: int
    thread_handle: int
    pid: int
    created_at: int
    image_path: Path
    is_assigned: bool = False

    def resume(self) -> None:
        """
        仅恢复已经核实 Job 归属的线程，并释放主线程句柄。

        :raises WindowsRuntimeError: 归属或恢复失败时抛出
        """
        if not self.is_assigned or not self.thread_handle:
            raise WindowsRuntimeError("插件进程没有受控恢复条件", "PLUGIN_JOB_ASSIGN_FAILED")
        if self.api.kernel.ResumeThread(self.thread_handle) == 0xFFFFFFFF:
            raise WindowsRuntimeError("无法恢复插件进程", "PLUGIN_PROCESS_RESUME_FAILED")
        self.api.close(self.thread_handle)
        self.thread_handle = 0

    def poll(self) -> int | None:
        """
        根据进程句柄的信号状态判断退出，不仅依赖可重用 PID。

        :return: 退出码；仍在执行时返回 None
        """
        if self.api.kernel.WaitForSingleObject(self.handle, 0) == 258:
            return None
        code = wintypes.DWORD()
        self.api.check(self.api.kernel.GetExitCodeProcess(self.handle, byref(code)), "PLUGIN_PROCESS_QUERY_FAILED")
        return code.value

    def close(self) -> None:
        """
        关闭线程及进程句柄；所属 Job 负责终止实际执行。
        """
        if self.thread_handle:
            self.api.close(self.thread_handle)
            self.thread_handle = 0
        if self.handle:
            self.api.close(self.handle)
            self.handle = 0


class WindowsJob:
    """
    在恢复之前分配每个根进程，并禁止普通派生进程脱离。

    只提供进程监管，不限制同用户恶意代码的文件、网络或其他 broker。
    """

    def __init__(self) -> None:
        """
        创建本 Worker 专用的匿名 Job，不设置 CPU 或内存硬限制。
        """
        self.api: WindowsApi = WindowsApi()
        self.handle: int = self.api.create_job()
        self._processes: list[WindowsProcess] = []
        self._lock = RLock()

    def spawn_suspended(
        self, arguments: tuple[str, ...], cwd_path: Path, environment: dict[str, str]
    ) -> WindowsProcess:
        """
        挂起创建根进程，成功分配并复核 Job 后才允许调用方恢复。

        :param arguments: 绝对执行文件和独立参数
        :param cwd_path: 受信工作目录
        :param environment: 净化后的运行环境
        :return: 仍挂起且已经属于本 Job 的进程
        :raises WindowsRuntimeError: 任一步失败时回收挂起根进程并抛出
        """
        with self._lock:
            if not self.handle:
                raise WindowsRuntimeError("插件 Job 已关闭", "PLUGIN_HOST_CLOSED")
            process_handle, thread_handle, pid = self.api.create_suspended(arguments, cwd_path, environment)
            try:
                created_at, image_path = self.api.process_identity(process_handle)
                process = WindowsProcess(self.api, process_handle, thread_handle, pid, created_at, image_path)
                self.api.assign(self.handle, process_handle)
                process.is_assigned = True
            except BaseException:
                self.api.kernel.TerminateProcess(process_handle, 1)
                self.api.kernel.WaitForSingleObject(process_handle, 5000)
                self.api.close(thread_handle)
                self.api.close(process_handle)
                raise
            self._processes.append(process)
            return process

    def contains(self, process_handle: int) -> bool:
        """
        查询真实句柄是否属于本 Job。

        :param process_handle: 需保持打开的进程句柄
        :return: 当前成员资格
        """
        return bool(self.handle) and self.api.is_in_job(self.handle, process_handle)

    def active_count(self) -> int:
        """
        查询实际活跃成员数，已关闭 Job 返回零。

        :return: 普通受控进程树的活跃成员数
        """
        return self.api.active_processes(self.handle) if self.handle else 0

    def close(self, timeout: float = 5.0) -> None:
        """
        请求终止全部成员，确认回收后关闭所有持有句柄。

        :param timeout: 实际退出等待上限
        :raises WindowsRuntimeError: 无法确认退出时保持句柄供调用方重试
        """
        with self._lock:
            if not self.handle:
                return
            if self.active_count():
                self.api.check(self.api.kernel.TerminateJobObject(self.handle, 1), "PLUGIN_JOB_STOP_FAILED")
            deadline = monotonic() + timeout
            while self.active_count():
                if monotonic() >= deadline:
                    raise WindowsRuntimeError("插件进程树尚未确认停止", "PLUGIN_JOB_STOP_PENDING")
                sleep(0.01)
            for process in self._processes:
                remaining_ms = max(1, int((deadline - monotonic()) * 1000))
                if self.api.kernel.WaitForSingleObject(process.handle, remaining_ms) != 0:
                    raise WindowsRuntimeError("插件根进程尚未确认退出", "PLUGIN_JOB_STOP_PENDING")
                process.close()
            self._processes.clear()
            self.api.close(self.handle)
            self.handle = 0

    def __enter__(self) -> WindowsJob:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
