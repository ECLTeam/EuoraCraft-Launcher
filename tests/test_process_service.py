# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 process_service 模块的自动化测试。
#
# 公开接口：
#   - wait_until(cond, timeout=…) -> bool — 轮询等待条件成立，超时返回 False，避免用固定 sleep 造成的时序脆弱。
#   - make_service() -> tuple[ProcessService, EventBus, list[dict[str, Any]]] — 构造使用事件闭环子进程的服务实例。
#   - test_spawn_captures_output_then_exits() -> None
#   - test_stdin_writes_through_to_subprocess() -> None
#   - test_stdin_rejected_without_stdin_pipe() -> None
#   - test_stop_terminates_instance() -> None
#   - test_same_name_instances_all_cleaned_up() -> None
#   - test_spawn_validates_arguments() -> None
#   - test_game_instance_registered_via_event_captures_output() -> None
#   - test_close_keeps_running_game_instance() -> None
# ============================================================

import sys
import time
from collections.abc import Callable
from typing import Any

import pytest

from ECL.foundation import EventBus
from ECL.game import InstancesManager
from ECL.services.processes import ProcessService


def wait_until(cond: Callable[[], bool], timeout: float = 6.0) -> bool:
    """轮询等待条件成立，超时返回 False，避免用固定 sleep 造成的时序脆弱。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make_service() -> tuple[ProcessService, EventBus, list[dict[str, Any]]]:
    """构造使用事件闭环子进程的服务实例。"""
    events = EventBus()
    logs: list[dict[str, Any]] = []
    events.subscribe("process:instance_log", logs.append)
    return ProcessService(events), events, logs


def test_spawn_captures_output_then_exits() -> None:
    service, _events, logs = make_service()
    script = "import time\nfor i in range(3):\n    print(f'line-{i}', flush=True)\ntime.sleep(1)\n"
    iid = service.spawn("demo", "plugin:test", [sys.executable, "-u", "-c", script])

    assert iid
    assert wait_until(lambda: any(entry["line"] == "line-2" for entry in logs))
    instances = service.list()
    assert len(instances) == 1
    assert instances[0]["id"] == iid
    assert instances[0]["running"] is True
    assert instances[0]["lines"] == ["line-0", "line-1", "line-2"]
    service.close()


def test_stdin_writes_through_to_subprocess() -> None:
    service, _events, logs = make_service()
    script = "import sys\nline = sys.stdin.readline()\nprint('echo:' + line.strip())\n"
    iid = service.spawn("io", "plugin:test", [sys.executable, "-u", "-c", script], stdin=True)

    assert service.send_stdin(iid, "hello") is True
    assert wait_until(lambda: any(entry["line"] == "echo:hello" for entry in logs))
    service.close()


def test_stdin_rejected_without_stdin_pipe() -> None:
    service, _events, _logs = make_service()
    script = "import time; time.sleep(1)\n"
    iid = service.spawn("nopipe", "plugin:test", [sys.executable, "-u", "-c", script], stdin=False)

    assert service.send_stdin(iid, "x") is False
    assert service.send_stdin("missing", "x") is False
    service.close()


def test_stop_terminates_instance() -> None:
    service, _events, _logs = make_service()
    iid = service.spawn("sleeper", "plugin:test", [sys.executable, "-u", "-c", "import time; time.sleep(30)"])

    assert service.stop(iid, wait_timeout=3) is True
    assert wait_until(lambda: len(service.list()) == 0)
    service.close()


def test_same_name_instances_all_cleaned_up() -> None:
    service, _events, _logs = make_service()
    command = [sys.executable, "-u", "-c", "import time; time.sleep(1)"]

    first = service.spawn("dup", "plugin:test", command)
    second = service.spawn("dup", "plugin:test", command)

    assert first != second
    assert wait_until(lambda: len(service.list()) == 0)
    service.close()


def test_spawn_validates_arguments() -> None:
    service, _events, _logs = make_service()
    with pytest.raises(ValueError):
        service.spawn("", "plugin:test", [sys.executable])
    with pytest.raises(ValueError):
        service.spawn("name", "", None)  # type: ignore[arg-type]
    service.close()


def test_game_instance_registered_via_event_captures_output() -> None:
    events = EventBus()
    logs: list[dict[str, Any]] = []
    events.subscribe("process:instance_log", logs.append)
    manager = InstancesManager()
    service = ProcessService(events, instances_manager=manager)
    script = "import time\nprint('game-boot', flush=True)\ntime.sleep(1)\n"
    iid, _proc = manager.create_instance(
        instance_name="1.20.1",
        instance_type="Minecraft",
        args=[sys.executable, "-u", "-c", script],
    )
    events.emit("game:instances_changed", {"action": "started", "instanceId": iid, "versionId": "1.20.1"})

    assert wait_until(lambda: any(entry["line"] == "game-boot" for entry in logs))
    instances = service.list()
    assert any(item["id"] == iid and item["type"] == "Minecraft" and item["stdin"] is False for item in instances)
    service.close()


def test_close_keeps_running_game_instance() -> None:
    events = EventBus()
    manager = InstancesManager()
    service = ProcessService(events, instances_manager=manager)
    iid, _proc = manager.create_instance(
        instance_name="1.20.1",
        instance_type="Minecraft",
        args=[sys.executable, "-u", "-c", "import time; time.sleep(30)"],
    )
    events.emit("game:instances_changed", {"action": "started", "instanceId": iid, "versionId": "1.20.1"})
    assert wait_until(lambda: any(item["id"] == iid for item in service.list()))

    service.close()
    assert manager.get_instances_info(), "共享管理器中的游戏实例不应被 ProcessService 关闭"
    service.stop(iid, force=True, wait_timeout=3)
