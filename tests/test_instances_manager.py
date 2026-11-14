# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 instances_manager 模块的自动化测试。
#
# 公开接口：
#   - class FakeProcess
#       - poll() -> int | None
#       - wait(timeout=…) -> int
#       - terminate() -> None
#       - kill() -> None
#   - test_stop_instance_terminates_then_waits() -> None
#   - test_stop_instance_forces_process_only_after_timeout() -> None
#   - test_stop_instance_kills_when_forced() -> None
#   - test_create_instance_does_not_lose_immediate_exit_callback(monkeypatch) -> None
#   - test_windows_priority_constants_are_not_read_on_posix(monkeypatch) -> None
#   - test_apply_priority_skips_normal(monkeypatch) -> None
#   - test_apply_priority_sets_posix_nice_for_high(monkeypatch) -> None
# ============================================================

from __future__ import annotations

import importlib
import io
import subprocess

from ECL.minecraft.game import InstancesManager


class FakeProcess:
    def __init__(self, *, times_out: bool = False) -> None:
        self.times_out = times_out
        self.running = True
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float | int | None] = []

    def poll(self) -> int | None:
        return None if self.running else 0

    def wait(self, timeout: float | int | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.times_out and self.kill_calls == 0:
            raise subprocess.TimeoutExpired("java", timeout)
        self.running = False
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.running = False

    def kill(self) -> None:
        self.kill_calls += 1
        self.running = False


def _manager_with_process(process: FakeProcess) -> InstancesManager:
    manager = InstancesManager()
    manager.instances["minecraft"] = {"Instance": process}
    return manager


def test_stop_instance_terminates_then_waits() -> None:
    process = FakeProcess()
    manager = _manager_with_process(process)

    exited_normally = manager.stop_instance("minecraft", wait_timeout=3.0)

    assert exited_normally is True
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [3.0]
    assert process.kill_calls == 0


def test_stop_instance_forces_process_only_after_timeout() -> None:
    process = FakeProcess(times_out=True)
    manager = _manager_with_process(process)

    exited_normally = manager.stop_instance("minecraft", wait_timeout=3.0)

    assert exited_normally is False
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [3.0, None]
    assert process.kill_calls == 1


def test_stop_instance_kills_when_forced() -> None:
    process = FakeProcess()
    manager = _manager_with_process(process)

    exited_normally = manager.stop_instance("minecraft", force=True, wait_timeout=None)

    assert exited_normally is True
    assert process.kill_calls == 1
    assert process.terminate_calls == 0


def test_create_instance_does_not_lose_immediate_exit_callback(monkeypatch) -> None:
    module = importlib.import_module("ECL.minecraft.game.Core.InstancesManager")

    class ImmediateProcess:
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self) -> int:
            return 1

    class SynchronousThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: ImmediateProcess())
    monkeypatch.setattr(module.threading, "Thread", SynchronousThread)
    manager = InstancesManager()
    exits = []

    instance_id, _process = manager.create_instance(
        "instant-failure",
        "Minecraft",
        ["java", "broken"],
        exit_callback=lambda code, name: exits.append((code, name)),
    )

    assert exits == [(1, instance_id)]


def test_apply_priority_skips_normal(monkeypatch) -> None:
    module = importlib.import_module("ECL.minecraft.game.Core.InstancesManager")

    class FakeProc:
        pid = 4242

    def rejected(pid) -> object:
        raise AssertionError("normal 优先级不应触碰进程")

    monkeypatch.setattr(module.psutil, "Process", rejected)

    InstancesManager._apply_priority(FakeProc(), "normal")


def test_windows_priority_constants_are_not_read_on_posix(monkeypatch) -> None:
    module = importlib.import_module("ECL.minecraft.game.Core.InstancesManager")
    priority_constants = (
        "IDLE_PRIORITY_CLASS",
        "BELOW_NORMAL_PRIORITY_CLASS",
        "NORMAL_PRIORITY_CLASS",
        "ABOVE_NORMAL_PRIORITY_CLASS",
        "HIGH_PRIORITY_CLASS",
    )

    monkeypatch.setattr(module.sys, "platform", "linux")
    for name in priority_constants:
        monkeypatch.delattr(module.psutil, name, raising=False)

    assert module._windows_priority_classes() == {}


def test_apply_priority_sets_posix_nice_for_high(monkeypatch) -> None:
    module = importlib.import_module("ECL.minecraft.game.Core.InstancesManager")

    class FakeProc:
        pid = 4242

    seen: list[int] = []

    class FakeProcess:
        def __init__(self, pid) -> None:
            pass

        def nice(self, value: int) -> None:
            seen.append(value)

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.psutil, "Process", FakeProcess)

    InstancesManager._apply_priority(FakeProc(), "high")

    assert seen == [-10]
