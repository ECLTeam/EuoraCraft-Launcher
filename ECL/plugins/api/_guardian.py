# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在独立 POSIX 会话中监管一个 Worker，宿主断开后仍清理普通后代。
#
# 公开接口：
#   - run_guardian — 执行只有受信管道控制、不会导入插件的监管角色。
# ============================================================

from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import stat
import struct
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing, suppress
from pathlib import Path
from time import monotonic, sleep

from ECL.bootstrap import PluginProcessOptions


def _receive(timeout: float) -> dict[str, object] | None:
    """
    只从宿主保留的标准输入管道读取限长控制帧；EOF 表示需要清理。
    """
    deadline = monotonic() + timeout

    def read(count: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            remaining = deadline - monotonic()
            if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                raise TimeoutError
            part = os.read(0, count - len(result))
            if not part:
                raise EOFError
            result.extend(part)
        return bytes(result)

    try:
        size = struct.unpack("!I", read(4))[0]
        if not 0 < size <= 65536:
            raise ValueError("监管控制消息超限")
        result = json.loads(read(size))
        if not isinstance(result, dict):
            raise ValueError("监管控制消息无效")
        return result
    except EOFError:
        return None


def _send(value: dict[str, object]) -> None:
    """
    在固定预算内回复宿主控制帧，失去接收方后转入本地清理。
    """
    content = json.dumps(value, separators=(",", ":")).encode()
    frame = memoryview(struct.pack("!I", len(content)) + content)
    deadline = monotonic() + 5
    while frame:
        remaining = deadline - monotonic()
        if remaining <= 0 or not select.select([], [1], [], remaining)[1]:
            raise TimeoutError
        count = os.write(1, frame)
        if not count:
            raise BrokenPipeError
        frame = frame[count:]


def _linux_subreaper() -> None:
    """
    仅本次专属守护进程接管孤儿后代，不改变桌面宿主的子进程归属。
    """
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        function = library.prctl
        function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        function.restype = ctypes.c_int
        if function(36, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "插件监管条件不可用")


def _members(group: int) -> tuple[int, ...]:
    """
    只列举本 Worker 进程组和 Linux 守护进程已接管的活跃孤儿。

    原始组首进程保留为未回收子进程直到清理结束，组标识不会被其他来源复用。
    """
    result: list[int] = []
    if sys.platform == "linux":
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z" and (int(fields[2]) == group or int(fields[1]) == os.getpid()):
                    result.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
    else:
        response = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True, check=True, timeout=2)
        for line in response.stdout.decode("ascii").splitlines():
            pid, pgid, state = line.split()
            if int(pgid) == group and not state.startswith("Z"):
                result.append(int(pid))
    return tuple(result)


def _stop(group: int, timeout: float = 5.0) -> None:
    """
    确认本组活跃成员归零；Linux 孤儿由本守护进程直接持有身份。
    """
    deadline = monotonic() + timeout
    while members := _members(group):
        if monotonic() >= deadline:
            raise TimeoutError("插件进程树尚未退出")
        with suppress(ProcessLookupError):
            os.killpg(group, signal.SIGKILL)
        if sys.platform == "linux":
            for pid in members:
                try:
                    # 只对仍由本守护进程直接持有、尚未回收的孤儿补充停止。
                    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[1]) == os.getpid():
                        os.kill(pid, signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    continue
        sleep(0.025)


def _launch(request: dict[str, object], options: PluginProcessOptions) -> subprocess.Popen[bytes]:
    """
    只启动同一发行入口的 Worker 角色，不作为通用命令执行器。
    """
    if set(request) != {"arguments", "cwd", "environment"}:
        raise ValueError("监管启动字段无效")
    arguments, cwd, environment = request["arguments"], request["cwd"], request["environment"]
    if (
        not isinstance(arguments, list)
        or not 8 <= len(arguments) <= 16
        or any(not isinstance(arg, str) or len(arg) > 32768 for arg in arguments)
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
        or not isinstance(environment, dict)
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items())
    ):
        raise ValueError("监管启动参数无效")
    expected_tail = [
        "--headless",
        "--role",
        "plugin",
        "--endpoint",
        options.endpoint,
        "--host-pid",
        str(options.host_pid),
        "--run-id",
        options.run_id,
    ]
    prefix = arguments[: -len(expected_tail)]
    executable = Path(sys.executable if getattr(sys, "frozen", False) else sys._base_executable).resolve()
    expected_prefix = [str(executable)]
    if not getattr(sys, "frozen", False):
        expected_prefix += ["-S", str(Path(__file__).resolve().parents[2] / "main.py")]
    if prefix != expected_prefix or arguments[-len(expected_tail) :] != expected_tail:
        raise ValueError("监管启动入口与运行不符")
    return subprocess.Popen(
        arguments,
        cwd=cwd,
        env=environment,
        process_group=0,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(worker: subprocess.Popen[bytes]) -> None:
    """
    仅在活跃成员归零后回收原始组首进程及已接管的孤儿。
    """
    worker.wait(timeout=1)
    if sys.platform == "linux":
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break


def _control_loop(worker: subprocess.Popen[bytes], has_exited: Callable[[], bool]) -> None:
    """
    一次请求对应一次回复；清理超时保留监管身份，宿主 EOF 后继续清理。
    """
    is_stopping = False
    is_stopped = False
    has_host = True
    while True:
        is_stopping = is_stopping or has_exited()
        command = None
        if has_host and select.select([0], [], [], 0.1)[0]:
            command = _receive(5)
            if command is None:
                has_host = False
                is_stopping = True
            elif command == {"action": "stop"}:
                is_stopping = True
            elif command == {"action": "finish"} and is_stopped:
                break
            elif command != {"action": "status"}:
                raise ValueError("监管控制方法无效")
        if is_stopping and not is_stopped:
            try:
                _stop(worker.pid, 0.25)
                is_stopped = True
            except TimeoutError:
                # D 状态等不可立即终止的成员需要继续监管，不能提前释放组身份。
                pass
        if not has_host and is_stopped:
            break
        if command is not None:
            try:
                _send({"stopped": is_stopped, "active": len(_members(worker.pid))})
            except (OSError, TimeoutError):
                has_host = False
                is_stopping = True
    _reap(worker)


def _control(worker: subprocess.Popen[bytes]) -> None:
    """
    macOS 使用退出事件，Linux 使用 WNOWAIT；两者均不提前回收原进程。
    """
    if sys.platform == "darwin":
        with closing(select.kqueue()) as observer:
            observer.control(
                [
                    select.kevent(
                        worker.pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
            _control_loop(worker, lambda: bool(observer.control(None, 1, 0)))
    else:
        _control_loop(worker, lambda: os.waitid(os.P_PID, worker.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)


def _cleanup(worker: subprocess.Popen[bytes]) -> None:
    """
    外部控制失败后仍持有原始组身份；每轮有界清理，直到实际确认退出。
    """
    while True:
        try:
            _stop(worker.pid)
            _reap(worker)
            return
        except (OSError, TimeoutError, subprocess.SubprocessError):
            # 失败不能转成成功退出，也不能丢掉尚在执行的普通后代。
            sleep(0.1)


def run_guardian(options: PluginProcessOptions) -> int:
    """
    在宿主 EOF、正常停止或 Worker 退出时回收本来源普通进程树。

    :param options: 轻量入口已校验的运行信息
    :return: 确认清理成功为零；平台或控制失败为二，失败也先完成清理
    """
    if sys.platform not in {"linux", "darwin"}:
        return 2
    worker: subprocess.Popen[bytes] | None = None
    try:
        if not stat.S_ISFIFO(os.fstat(0).st_mode) or not stat.S_ISFIFO(os.fstat(1).st_mode):
            return 2
        if os.getsid(0) != os.getpgrp() or os.getsid(0) == os.getsid(options.host_pid):
            return 2
        os.set_blocking(1, False)
        _linux_subreaper()
        _send({"ready": True})
        request = _receive(30)
        if request is None:
            return 0
        worker = _launch(request, options)
        _send({"pid": worker.pid})
        _control(worker)
        return 0
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return 2
    finally:
        if worker is not None and worker.returncode is None:
            _cleanup(worker)
