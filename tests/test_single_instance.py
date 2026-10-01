# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 single_instance 模块的自动化测试。
#
# 公开接口：
#   - test_start_writes_discovery_and_serves_focus(tmp_path) -> None
#   - test_probe_without_primary_returns_false(tmp_path) -> None
#   - test_probe_removes_stale_discovery(tmp_path, monkeypatch) -> None
#   - test_probe_connect_refused_cleans_up(tmp_path, monkeypatch) -> None
#   - test_wrong_token_is_rejected(tmp_path) -> None
#   - test_close_is_idempotent(tmp_path) -> None
# ============================================================

import json
import os
import socket

import pytest

from ECL.events import EventBus
from ECL.services.single_instance import SingleInstanceService, probe_running_instance


def _read_discovery(tmp_path) -> dict:
    return json.loads((tmp_path / "single_instance.json").read_text(encoding="utf-8"))


def test_start_writes_discovery_and_serves_focus(tmp_path) -> None:
    """主实例启动后应写入发现文件，且 focus 请求经事件总线派发。"""
    events = EventBus()
    received: list[dict] = []
    events.subscribe("launcher:focus_request", received.append)
    service = SingleInstanceService(events, tmp_path, "1.2.3-test")
    service.start()
    try:
        document = _read_discovery(tmp_path)
        assert document["pid"] == os.getpid()
        assert document["launcherVersion"] == "1.2.3-test"
        assert document["protocolVersion"] == 1
        assert isinstance(document["port"], int)

        assert probe_running_instance(tmp_path, ["--launch", "Foo"]) is True
        assert received and received[0]["argv"] == ["--launch", "Foo"]
    finally:
        service.close()

    assert not (tmp_path / "single_instance.json").exists()


def test_probe_without_primary_returns_false(tmp_path) -> None:
    """没有发现文件时应返回 False 且不创建任何文件。"""
    assert probe_running_instance(tmp_path, ["x"]) is False
    assert not (tmp_path / "single_instance.json").exists()


def test_probe_removes_stale_discovery(tmp_path, monkeypatch) -> None:
    """发现文件记录的 pid 已失效时应清理残留文件并返回 False。"""
    discovery_path = tmp_path / "single_instance.json"
    discovery_path.write_text(
        json.dumps({"port": 1, "token": "t", "pid": 123, "protocolVersion": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr("ECL.services.single_instance.psutil.pid_exists", lambda _pid: False)

    assert probe_running_instance(tmp_path, ["x"]) is False
    assert not discovery_path.exists()


def test_probe_connect_refused_cleans_up(tmp_path, monkeypatch) -> None:
    """pid 存活但连接被拒绝（主实例正在退出）时应清理残留文件。"""
    # 先绑定再关闭，获得一个确定被拒绝的本地端口
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_socket.bind(("127.0.0.1", 0))
    dead_port = probe_socket.getsockname()[1]
    probe_socket.close()

    discovery_path = tmp_path / "single_instance.json"
    discovery_path.write_text(
        json.dumps({"port": dead_port, "token": "t", "pid": os.getpid(), "protocolVersion": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr("ECL.services.single_instance.psutil.pid_exists", lambda _pid: True)

    assert probe_running_instance(tmp_path, ["x"]) is False
    assert not discovery_path.exists()


def test_wrong_token_is_rejected(tmp_path) -> None:
    """令牌不匹配的请求应被拒绝且不派发事件。"""
    events = EventBus()
    received: list[dict] = []
    events.subscribe("launcher:focus_request", received.append)
    service = SingleInstanceService(events, tmp_path, "1.0")
    service.start()
    try:
        document = _read_discovery(tmp_path)
        with socket.create_connection(("127.0.0.1", document["port"]), timeout=3) as conn:
            conn.sendall(json.dumps({"token": "wrong-token", "action": "focus"}).encode("utf-8") + b"\n")
            reply = json.loads(conn.recv(1024).decode("utf-8"))
        assert reply["ok"] is False
        assert not received
    finally:
        service.close()


def test_close_is_idempotent(tmp_path) -> None:
    """重复 close 不应抛错，且监听线程随关闭退出。"""
    service = SingleInstanceService(EventBus(), tmp_path, "1.0")
    service.start()
    thread = service._thread
    service.close()
    service.close()

    assert service._thread is None
    assert thread is not None and not thread.is_alive()
    assert not (tmp_path / "single_instance.json").exists()


def test_launch_equal_flags_are_queued_once_and_retained_until_ready(tmp_path) -> None:
    service = SingleInstanceService(EventBus(), tmp_path, "1.0")
    service.start()
    try:
        argv = ["--launch=Foo", "--memory=6144", "--windowed", "--jvm-arg=-Dcustom=value"]
        assert probe_running_instance(tmp_path, argv)
        assert probe_running_instance(tmp_path, argv)
        requests = service.take_launch_requests()
        assert len(requests) == 1
        assert requests[0].launch_target == "Foo"
        assert requests[0].game_overrides() == {"memory": 6144, "fullscreen": False, "jvm_args": ["-Dcustom=value"]}
        assert service.take_launch_requests() == []
        assert probe_running_instance(tmp_path, ["--open-page=download"])
        assert service.take_launch_requests()[0].open_page == "download"
    finally:
        service.close()


def test_running_instance_rejects_process_overrides_without_falling_back_to_new_process(tmp_path) -> None:
    service = SingleInstanceService(EventBus(), tmp_path, "1.0")
    service.start()
    try:
        with pytest.raises(ValueError, match="拒绝"):
            probe_running_instance(tmp_path, ["--launch=Foo", "--disable-plugins"])
        assert service.take_launch_requests() == []
        assert service.discovery_path.is_file()
    finally:
        service.close()
