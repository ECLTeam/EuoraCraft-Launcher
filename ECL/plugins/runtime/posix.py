# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过专属守护进程保留 POSIX Worker 身份、控制流及实际退出责任。
#
# 公开接口：
#   - class PosixScope — 独立会话内的单来源监管范围。
#   - class PosixReference — 已认证进程的内核存活引用。
# ============================================================

from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING

import psutil

from ECL.bootstrap import local_worker_endpoint
from ECL.plugin_api._errors import PluginRuntimeError
from ECL.plugins.runtime.executable import ExecutableLaunchSpec

if TYPE_CHECKING:
    from ECL.plugins.runtime.process_scope import ProcessReference


@dataclass(slots=True)
class PosixReference:
    """
    Linux 持有 pidfd，macOS 持有原进程退出事件；不以裸 PID 轮询代替身份。
    """

    pid: int
    created_at: int
    image_path: Path
    descriptor: int = -1
    queue: select.kqueue | None = None
    _exited: bool = False

    def is_alive(self) -> bool:
        """
        查询已经绑定的内核对象是否退出，不查询可能重用的同名 PID。

        :return: 原对象仍在执行时为 True
        """
        if self._exited:
            return False
        if self.queue is not None:
            self._exited = bool(self.queue.control(None, 1, 0))
            return not self._exited
        if self.descriptor < 0:
            return False
        return not bool(select.select([self.descriptor], [], [], 0)[0])

    def close(self) -> None:
        """
        幂等释放身份资源，不终止对应执行。
        """
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1
        self._exited = True


@dataclass(slots=True)
class _PreparedGuardian:
    scope: PosixScope
    pid: int
    arguments: tuple[str, ...]
    cwd_path: Path
    environment: dict[str, str]
    is_resumed: bool = False

    def resume(self) -> None:
        """
        监管已就绪后才交付同一入口的 Worker 启动信息。
        """
        if self.is_resumed:
            raise PluginRuntimeError("插件监管启动已经使用", "PLUGIN_SESSION_EXPIRED")
        self.scope._resume(self.arguments, self.cwd_path, self.environment)
        self.is_resumed = True


class PosixScope:
    """
    宿主只管理本守护角色；业务回调仍走经过系统身份认证的独立本地通道。
    """

    def __init__(self, executable: ExecutableLaunchSpec, run_id: str) -> None:
        """
        保存本运行入口，不在构造时启动进程。

        :param executable: 固定受信发行入口
        :param run_id: 宿主生成的运行标识
        """
        self._executable = executable
        self._run_id = run_id
        self._guardian: subprocess.Popen[bytes] | None = None
        self._group: int | None = None
        self._lock = RLock()
        self._stopped = False
        self._control_failed = False
        self._finish_sent = False

    def prepare(self, arguments: tuple[str, ...], cwd_path: Path, environment: dict[str, str]) -> _PreparedGuardian:
        """
        先建立不加载插件的专属会话和控制流，再允许调用方恢复 Worker。

        :param arguments: 同一发行入口的 Worker 参数
        :param cwd_path: 受信工作目录
        :param environment: 净化后的完整环境
        :return: 尚未启动 Worker 的监管对象
        :raises PluginRuntimeError: 准备、控制或平台失败
        """
        with self._lock:
            if self._guardian is not None or sys.platform not in {"linux", "darwin"}:
                raise PluginRuntimeError("插件监管范围不可创建", "PLUGIN_RUNTIME_UNSUPPORTED")
            address = local_worker_endpoint(self._run_id)
            self._guardian = subprocess.Popen(
                self._executable.arguments(address, os.getpid(), self._run_id, guardian=True),
                cwd=cwd_path,
                env=environment,
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert self._guardian.stdin is not None
            os.set_blocking(self._guardian.stdin.fileno(), False)
            if self._receive(30) != {"ready": True}:
                raise PluginRuntimeError("插件监管引导失败", "PLUGIN_JOB_ASSIGN_FAILED")
            return _PreparedGuardian(self, self._guardian.pid, arguments, cwd_path, dict(environment))

    def _send(self, message: dict[str, object], timeout: float = 5.0) -> None:
        """
        使用宿主私有管道发送有界控制帧，不经过插件的请求分派。
        """
        guardian = self._guardian
        if guardian is None or guardian.stdin is None:
            raise PluginRuntimeError("插件监管控制流已关闭", "PLUGIN_HOST_CLOSED")
        try:
            content = json.dumps(message, separators=(",", ":")).encode()
            if len(content) > 65536:
                raise PluginRuntimeError("插件监管消息过长", "PLUGIN_INVALID_REQUEST")
            frame = memoryview(struct.pack("!I", len(content)) + content)
            deadline = monotonic() + timeout
            while frame:
                remaining = deadline - monotonic()
                if remaining <= 0 or not select.select([], [guardian.stdin], [], remaining)[1]:
                    raise PluginRuntimeError("插件监管消息超时", "PLUGIN_DEADLINE_EXCEEDED")
                count = os.write(guardian.stdin.fileno(), frame)
                if not count:
                    raise PluginRuntimeError("插件监管控制流已断开", "PLUGIN_PIPE_CLOSED")
                frame = frame[count:]
        except (OSError, ValueError, PluginRuntimeError):
            self._control_failed = True
            raise

    def _receive(self, timeout: float) -> dict[str, object]:
        """
        从受信守护角色读取完整控制帧，所有等待共用同一截止。
        """
        guardian = self._guardian
        if guardian is None or guardian.stdout is None:
            raise PluginRuntimeError("插件监管未就绪", "PLUGIN_HOST_CLOSED")
        try:
            deadline = monotonic() + timeout

            def read(size: int) -> bytes:
                result = bytearray()
                while len(result) < size:
                    remaining = deadline - monotonic()
                    if remaining <= 0 or not select.select([guardian.stdout], [], [], remaining)[0]:
                        raise PluginRuntimeError("插件监管响应超时", "PLUGIN_DEADLINE_EXCEEDED")
                    content = os.read(guardian.stdout.fileno(), size - len(result))
                    if not content:
                        raise PluginRuntimeError("插件监管已退出", "PLUGIN_PIPE_CLOSED")
                    result.extend(content)
                return bytes(result)

            size = struct.unpack("!I", read(4))[0]
            if not 0 < size <= 65536:
                raise PluginRuntimeError("插件监管消息无效", "PLUGIN_PIPE_AUTH_FAILED")
            try:
                result = json.loads(read(size))
            except (ValueError, UnicodeError):
                raise PluginRuntimeError("插件监管消息无效", "PLUGIN_PIPE_AUTH_FAILED") from None
            if not isinstance(result, dict):
                raise PluginRuntimeError("插件监管消息无效", "PLUGIN_PIPE_AUTH_FAILED")
            return result
        except (OSError, ValueError, PluginRuntimeError):
            self._control_failed = True
            raise

    def _resume(self, arguments: tuple[str, ...], cwd_path: Path, environment: dict[str, str]) -> None:
        """
        监管角色就绪后串行交付 Worker 参数并保留其原始进程组身份。
        """
        with self._lock:
            self._send({"arguments": list(arguments), "cwd": str(cwd_path), "environment": environment})
            reply = self._receive(30)
            pid = reply.get("pid")
            if type(pid) is not int or pid <= 0:
                raise PluginRuntimeError("插件监管身份无效", "PLUGIN_PIPE_AUTH_FAILED")
            self._group = pid

    def reference(self, pid: int, executable_path: Path) -> PosixReference:
        """
        将内核对端 PID 绑定到本会话、进程组、UID、镜像及持久身份引用。

        :param pid: 本地传输内核返回的 PID
        :param executable_path: 宿主固定发行入口
        :return: 调用方负责释放的内核引用
        :raises PluginRuntimeError: 身份不符或原生绑定失败
        """
        process = psutil.Process(pid)
        created = int(process.create_time() * 1_000_000_000)
        image = Path(process.exe()).resolve()
        if process.uids().effective != os.geteuid() or image != executable_path.resolve():
            raise PluginRuntimeError("插件连接镜像或所有者不符", "PLUGIN_PIPE_AUTH_FAILED")
        reference = PosixReference(pid, created, image)
        try:
            if sys.platform == "linux":
                reference.descriptor = os.pidfd_open(pid)
            else:
                reference.queue = select.kqueue()
                reference.queue.control(
                    [
                        select.kevent(
                            pid,
                            filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                            fflags=select.KQ_NOTE_EXIT,
                        )
                    ],
                    0,
                    0,
                )
            if int(psutil.Process(pid).create_time() * 1_000_000_000) != created or not self.is_member(reference):
                raise PluginRuntimeError("插件进程身份或监管归属已变化", "PLUGIN_PIPE_AUTH_FAILED")
            return reference
        except BaseException:
            reference.close()
            raise

    def is_member(self, reference: ProcessReference) -> bool:
        """
        查询实际会话和进程组，不根据进程名或 PID 前缀推断来源。

        :param reference: 由本实现产生的身份引用
        :return: 原进程仍活跃且属于本范围时为 True
        """
        guardian = self._guardian
        if guardian is None or self._group is None or not isinstance(reference, PosixReference):
            return False
        try:
            return (
                reference.is_alive()
                and os.getsid(reference.pid) == guardian.pid
                and os.getpgid(reference.pid) == self._group
            )
        except ProcessLookupError:
            return False

    def active_count(self) -> int:
        """
        从专属守护角色取得活跃成员数，不把根进程退出等同于后代退出。

        :return: 本来源普通进程树的活跃成员数
        """
        with self._lock:
            if self._guardian is None or self._group is None or self._stopped:
                return 0
            if self._control_failed:
                raise PluginRuntimeError("插件监管控制流失效", "PLUGIN_JOB_STOP_PENDING")
            self._send({"action": "status"})
            reply = self._receive(5)
            count = reply.get("active")
            if type(count) is not int or count < 0:
                raise PluginRuntimeError("插件监管计数无效", "PLUGIN_PIPE_AUTH_FAILED")
            if reply.get("stopped") is True:
                self._stopped = True
            return count

    def close(self, timeout: float = 5.0) -> None:
        """
        保留控制与身份直至实际清理确认，超时允许后续重试。

        :param timeout: 本次关闭等待预算
        :raises PluginRuntimeError: 守护角色或普通后代尚未确认停止
        """
        with self._lock:
            guardian = self._guardian
            if guardian is None:
                return
            if self._group is None or self._control_failed:
                if guardian.stdin is not None:
                    guardian.stdin.close()
            else:
                if not self._stopped:
                    self._send({"action": "stop"}, timeout)
                    reply = self._receive(timeout)
                    if reply != {"stopped": True, "active": 0}:
                        raise PluginRuntimeError("插件进程树尚未确认停止", "PLUGIN_JOB_STOP_PENDING")
                    self._stopped = True
                if not self._finish_sent:
                    self._send({"action": "finish"}, timeout)
                    self._finish_sent = True
            try:
                result = guardian.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise PluginRuntimeError("插件监管角色尚未退出", "PLUGIN_JOB_STOP_PENDING") from None
            if result not in {0, 2} or (result == 2 and not self._control_failed and self._group is not None):
                raise PluginRuntimeError("插件监管退出未确认清理", "PLUGIN_JOB_STOP_PENDING")
            if guardian.stdin is not None and not guardian.stdin.closed:
                guardian.stdin.close()
            if guardian.stdout is not None:
                guardian.stdout.close()
            self._guardian = None
