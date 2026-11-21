# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证看门狗路由、协议与子进程真实行为（含强杀路径）。
# ============================================================

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ECL.bootstrap import ProcessRole, select_process_route
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.watchdog.child import max_timeout_seconds, min_timeout_seconds
from ECL.watchdog.host import WatchdogSession
from ECL.watchdog.protocol import FrameError, encode_frame, read_frame, write_frame

repo_root = Path(__file__).resolve().parents[1]
# 与 ExecutableLaunchSpec.current() 一致：Windows venv 入口是重定向器，
# 必须用真实解释器直接拉起子进程，父进程身份校验才能成立。
base_python = Path(getattr(sys, "_base_executable", sys.executable))
# 集成测试统一走统一发行入口，覆盖 bootstrap 分发与角色参数解析。
entry_command = [
    str(base_python),
    "-c",
    "import sys; from ECL.bootstrap import run_process; sys.exit(run_process(sys.argv[1:]))",
]


def subprocess_env() -> dict[str, str]:
    # 子进程需要能导入 ECL：源码运行依赖仓库根在 PYTHONPATH 中。
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo_root) + (os.pathsep + existing if existing else "")
    return env


def spawn_watchdog(host_pid: int) -> subprocess.Popen[bytes]:
    # 宿主即当前测试进程，满足子进程的父进程身份校验。
    return subprocess.Popen(
        [
            *entry_command,
            "--headless",
            "--role",
            "watchdog",
            "--host-pid",
            str(host_pid),
            "--run-id",
            os.urandom(16).hex(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=subprocess_env(),
        cwd=repo_root,
    )


def test_select_route_accepts_watchdog() -> None:
    route = select_process_route(["--headless", "--role", "watchdog", "--host-pid", "12345", "--run-id", "ab" * 16])

    assert route.role is ProcessRole.watchdog
    assert route.arguments == ("--headless", "--role", "watchdog", "--host-pid", "12345", "--run-id", "ab" * 16)
    assert route.watchdog is not None
    assert route.watchdog.host_pid == 12345
    assert route.watchdog.run_id == "ab" * 16


@pytest.mark.parametrize(
    "arguments",
    [
        ["--role", "watchdog", "--host-pid", "12345", "--run-id", "ab" * 16],  # 缺 --headless
        ["--gui", "--role", "watchdog", "--host-pid", "12345", "--run-id", "ab" * 16],  # gui 与角色冲突
        ["--headless", "--role", "watchdog", "--run-id", "ab" * 16],  # 缺宿主 PID
        ["--headless", "--role", "watchdog", "--host-pid", "12345"],  # 缺运行标识
        ["--headless", "--role", "watchdog", "--host-pid", "12345", "--run-id", "短"],  # 运行标识格式非法
        ["--headless", "--role", "watchdog", "--host-pid", "0", "--run-id", "ab" * 16],  # PID 越界
        ["--headless", "--role", "watchdog", "--endpoint", "x", "--host-pid", "12345", "--run-id", "ab" * 16],
        ["--headless", "--role", "watchdog", "--host-pid", "12345", "--run-id", "ab" * 16, "--host-pid", "6789"],
    ],
)
def test_select_route_rejects_invalid_watchdog(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        select_process_route(arguments)


def test_protocol_frame_round_trip() -> None:
    message = {"hello": {"timeout_seconds": 300, "log_path": "C:/tmp/watchdog.log"}}
    stream = io.BytesIO()
    write_frame(stream, message)
    stream.seek(0)

    assert read_frame(stream) == message


def test_protocol_frame_rejects_malformed_content() -> None:
    # 长度头声称超出上限。
    oversized = io.BytesIO((999999).to_bytes(4, "big") + b"{}")
    with pytest.raises(FrameError):
        read_frame(oversized)
    # 帧中途遇到 EOF。
    truncated = io.BytesIO((16).to_bytes(4, "big") + b"{")
    with pytest.raises(FrameError):
        read_frame(truncated)
    # 内容不是消息对象。
    not_object = io.BytesIO(encode_frame({"x": 1})[:4] + b"[1]")
    with pytest.raises(FrameError):
        read_frame(not_object)
    # 干净 EOF。
    assert read_frame(io.BytesIO()) is None


def test_watchdog_child_rejects_wrong_host_pid() -> None:
    proc = spawn_watchdog(host_pid=0xFFFFFFFE)

    assert proc.wait(timeout=15) == 2


def test_watchdog_child_exits_on_shutdown(tmp_path: Path) -> None:
    proc = spawn_watchdog(host_pid=os.getpid())
    try:
        assert proc.stdin is not None and proc.stdout is not None
        write_frame(
            proc.stdin,
            {"hello": {"timeout_seconds": 60, "log_path": str(tmp_path / "watchdog-hang.log")}},
        )
        ready = read_frame(proc.stdout)
        assert ready is not None and ready.get("ready") is True
        write_frame(proc.stdin, {"shutdown": True})
        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_watchdog_child_exits_on_eof(tmp_path: Path) -> None:
    proc = spawn_watchdog(host_pid=os.getpid())
    try:
        assert proc.stdin is not None and proc.stdout is not None
        write_frame(
            proc.stdin,
            {"hello": {"timeout_seconds": 60, "log_path": str(tmp_path / "watchdog-hang.log")}},
        )
        ready = read_frame(proc.stdout)
        assert ready is not None and ready.get("ready") is True
        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_watchdog_child_heartbeat_resets_timeout(tmp_path: Path) -> None:
    # 阈值 1 秒 + 持续心跳 3 秒：若心跳未重置计时，看门狗早已终止测试进程。
    proc = spawn_watchdog(host_pid=os.getpid())
    try:
        assert proc.stdin is not None and proc.stdout is not None
        write_frame(
            proc.stdin,
            {"hello": {"timeout_seconds": min_timeout_seconds, "log_path": str(tmp_path / "watchdog-hang.log")}},
        )
        ready = read_frame(proc.stdout)
        assert ready is not None and ready.get("ready") is True
        for _ in range(10):
            time.sleep(0.3)
            write_frame(proc.stdin, {"heartbeat": True})
        write_frame(proc.stdin, {"shutdown": True})
        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_watchdog_child_terminates_stalled_host_and_writes_report(tmp_path: Path) -> None:
    report_path = tmp_path / "watchdog-hang.log"
    # 替身宿主自己拉起看门狗：满足父进程身份校验后故意停发心跳，等待被终止。
    dummy_script = (
        "import os, subprocess, sys, time\n"
        "sys.path.insert(0, " + repr(str(repo_root)) + ")\n"
        "from ECL.watchdog.protocol import read_frame, write_frame\n"
        "# 与宿主侧一致：venv 入口是重定向器，必须用真实解释器拉起子进程。\n"
        "base_python = getattr(sys, '_base_executable', sys.executable)\n"
        "entry = [base_python, '-c', "
        "'import sys; from ECL.bootstrap import run_process; sys.exit(run_process(sys.argv[1:]))']\n"
        "child = subprocess.Popen(\n"
        "    entry + ['--headless', '--role', 'watchdog', '--host-pid', str(os.getpid()), '--run-id', os.urandom(16).hex()],\n"
        "    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,\n"
        "    env={**os.environ, 'PYTHONPATH': " + repr(str(repo_root)) + "},\n"
        ")\n"
        "write_frame(child.stdin, {'hello': {'timeout_seconds': "
        + repr(min_timeout_seconds)
        + ", 'log_path': "
        + repr(str(report_path))
        + "}})\n"
        "ready = read_frame(child.stdout)\n"
        "assert ready is not None and ready.get('ready') is True\n"
        "time.sleep(15)\n"
    )
    dummy_path = tmp_path / "stalled_host.py"
    dummy_path.write_text(dummy_script, encoding="utf-8")
    # 同样使用真实解释器：重定向器会改变进程树，导致 PID 对不上。
    dummy = subprocess.Popen([str(base_python), str(dummy_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 阈值 1 秒 + 轮询间隔：替身宿主应远在 15 秒睡眠结束前被终止。
    assert dummy.wait(timeout=20) != 0
    report = report_path.read_text(encoding="utf-8")
    assert f"host_pid={dummy.pid}" in report
    assert f"timeout_seconds={min_timeout_seconds}" in report


def test_watchdog_session_degrades_on_spawn_failure(tmp_path: Path) -> None:
    session = WatchdogSession(
        tmp_path / "watchdog-hang.log",
        executable=ExecutableLaunchSpec(
            executable_path=tmp_path / "missing-entry.exe", project_path=None, builder_kind="pyinstaller"
        ),
    )

    assert session.start() is False
    session.notify_alive()
    session.stop()


def test_watchdog_session_runs_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(repo_root))
    session = WatchdogSession(
        tmp_path / "watchdog-hang.log",
        timeout_seconds=max_timeout_seconds,
        executable=ExecutableLaunchSpec(executable_path=base_python, project_path=repo_root, builder_kind="source"),
    )

    try:
        assert session.start() is True
        session.notify_alive()
    finally:
        session.stop()


def test_blocking_portal_exits_only_after_heartbeat_task_cancelled() -> None:
    # 回归：anyio portal 正常退出路径不取消残余任务，宿主必须主动 cancel
    # 常驻心跳协程，否则启动器退出会永久卡在 portal 收尾上。
    from anyio.from_thread import start_blocking_portal

    async def heartbeat_like_loop() -> None:
        while True:
            await asyncio.sleep(0.05)

    finished = threading.Event()

    def run_portal() -> None:
        with start_blocking_portal("asyncio") as portal:
            future = portal.start_task_soon(heartbeat_like_loop)
            time.sleep(0.2)
            future.cancel()
        finished.set()

    worker = threading.Thread(target=run_portal, daemon=True)
    worker.start()
    assert finished.wait(10), "portal 在取消心跳协程后仍未退出，启动器会卡死在退出流程"
    worker.join(5)
    assert not worker.is_alive()
