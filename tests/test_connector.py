# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 connector 模块的自动化测试。
#
# 公开接口：
#   - test_fetch_nodes_reuses_memory_cache() -> None
#   - test_fetch_nodes_force_refreshes_memory_cache() -> None
#   - test_fetch_nodes_falls_back_to_multiple_defaults_when_api_fails() -> None
#   - test_nat_result_maps_easytier_stun_snapshot() -> None
#   - test_nat_detection_starts_and_stops_temporary_easytier_node(monkeypatch) -> None
#   - test_minecraft_listener_ports_limits_candidates_to_java_processes(monkeypatch) -> None
#   - test_decode_varint_rejects_incomplete_input() -> None
#   - test_minecraft_status_probe_accepts_valid_status_response(monkeypatch) -> None
#   - test_current_players_skips_closed_client_loop() -> None
#   - test_stop_async_thread_client_handles_closed_loop() -> None
#   - test_close_stops_active_room() -> None
#   - test_florolding_host_uses_room_specific_scaffolding_ipv4(monkeypatch) -> None
#   - test_florolding_host_ipv4_is_stable_per_room() -> None
#   - test_florolding_server_reads_coalesced_requests_without_dropping_frames() -> None
#   - test_florolding_client_serializes_concurrent_requests() -> None
#   - test_run_in_daemon_returns_result() -> None
#   - test_run_in_daemon_uses_daemon_thread() -> None
# ============================================================

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("easytier_pyo3")

from ECL.services.connector import ConnectorService


class _FailingHttpClient:
    def get(self, *_args, **_kwargs):
        raise ConnectionError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")


class _NodeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _NodeHttpClient:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, *_args, **_kwargs) -> _NodeResponse:
        self.calls += 1
        return _NodeResponse(self.payload)


def test_fetch_nodes_reuses_memory_cache() -> None:
    payload = [{"url": "tcp://node.example:11010"}]
    first_http = _NodeHttpClient(payload)
    first = ConnectorService(http_client=first_http)

    assert first.fetch_nodes() == ["tcp://node.example:11010"]
    assert first.fetch_nodes() == ["tcp://node.example:11010"]
    assert first_http.calls == 1


def test_fetch_nodes_force_refreshes_memory_cache() -> None:
    http = _NodeHttpClient([{"url": "tcp://first.example:11010"}])
    service = ConnectorService(http_client=http)
    assert service.fetch_nodes() == ["tcp://first.example:11010"]

    http.payload = [{"url": "tcp://second.example:11010"}]
    assert service.fetch_nodes(force=True) == ["tcp://second.example:11010"]
    assert http.calls == 2


def test_fetch_nodes_falls_back_to_multiple_defaults_when_api_fails() -> None:
    service = ConnectorService(http_client=_FailingHttpClient())

    nodes = service.fetch_nodes(force=True)

    assert nodes == list(ConnectorService.default_nodes)
    assert len(ConnectorService.default_nodes) > 1, "默认节点必须保留多节点兜底，避免单点 DNS 失效导致无法联机"
    assert any(url.startswith("tcp://") for url in ConnectorService.default_nodes)


def test_nat_result_maps_easytier_stun_snapshot() -> None:
    result = ConnectorService._nat_result_from_stun_info(
        {
            "udp_nat_type": "PortRestricted",
            "public_ip": ["2001:db8::1", "203.0.113.42"],
            "min_port": 51820,
            "max_port": 51824,
        }
    )

    assert result == {
        "type": "cone",
        "detailType": "portRestricted",
        "publicIp": "203.0.113.42",
        "publicPort": 51820,
        "publicPortEnd": 51824,
        "supportsIpv6": True,
    }


def test_nat_detection_starts_and_stops_temporary_easytier_node(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Node:
        def __init__(self, config) -> None:
            captured["config"] = config
            captured["node"] = self
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def node_info(self) -> dict[str, object]:
            return {
                "stun_info": {
                    "udp_nat_type": "Symmetric",
                    "public_ip": ["203.0.113.9"],
                    "min_port": 40000,
                    "max_port": 40000,
                }
            }

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("ECL.services.connector.easytier_pyo3.Node", Node)
    result = ConnectorService().get_nat_type()
    node = captured["node"]

    assert result["type"] == "symmetric"
    assert node.started is True
    assert node.stopped is True
    assert captured["config"]["flags"] == {"no_tun": True, "bind_device": False, "enable_ipv6": True}


def test_minecraft_listener_ports_limits_candidates_to_java_processes(monkeypatch) -> None:
    java_process = SimpleNamespace(info={"pid": 101, "name": "javaw.exe", "exe": "C:/Java/bin/javaw.exe"})
    other_process = SimpleNamespace(info={"pid": 102, "name": "python.exe", "exe": "C:/Python/python.exe"})
    connections = [
        SimpleNamespace(pid=101, status="LISTEN", laddr=SimpleNamespace(port=38123)),
        SimpleNamespace(pid=101, status="ESTABLISHED", laddr=SimpleNamespace(port=25565)),
        SimpleNamespace(pid=102, status="LISTEN", laddr=SimpleNamespace(port=25565)),
    ]
    monkeypatch.setattr("ECL.services.connector.psutil.process_iter", lambda _attrs: [java_process, other_process])
    monkeypatch.setattr("ECL.services.connector.psutil.net_connections", lambda kind: connections)
    monkeypatch.setattr("ECL.services.connector.psutil.CONN_LISTEN", "LISTEN")

    assert ConnectorService._minecraft_listener_ports() == [38123]


def test_decode_varint_rejects_incomplete_input() -> None:
    assert ConnectorService._decode_varint(b"\x80") == (None, 0)
    assert ConnectorService._decode_varint(ConnectorService._encode_varint(758)) == (758, 2)


def test_minecraft_status_probe_accepts_valid_status_response(monkeypatch) -> None:
    status = json.dumps({"version": {"name": "1.21.1", "protocol": 767}}).encode()
    packet = b"\x00" + ConnectorService._encode_varint(len(status)) + status
    response = ConnectorService._encode_varint(len(packet)) + packet

    class Connection:
        def __init__(self) -> None:
            self.response = bytearray(response)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def sendall(self, _data: bytes) -> None:
            return None

        def recv(self, length: int) -> bytes:
            chunk = self.response[:length]
            del self.response[:length]
            return bytes(chunk)

    monkeypatch.setattr("ECL.services.connector.socket.create_connection", lambda *_args, **_kwargs: Connection())

    assert ConnectorService._is_minecraft_server(38123) is True


def test_current_players_skips_closed_client_loop() -> None:
    service = ConnectorService()
    loop = asyncio.new_event_loop()
    loop.close()
    service._mode = "guest"
    service._client = SimpleNamespace(
        loop=loop,
        writer=object(),
        player_name="Alice",
        vendor="test",
        machine_id="m1",
    )

    players = service._current_players()
    assert players == [{"name": "Alice", "vendor": "test", "iconBase64": None, "kind": "guest", "machineId": "m1"}]


def test_stop_async_thread_client_handles_closed_loop() -> None:
    service = ConnectorService()
    loop = asyncio.new_event_loop()
    loop.close()
    service._client = SimpleNamespace(loop=loop, writer=object())

    service._stop_async_thread_client()
    assert service._client is None


def test_close_stops_active_room() -> None:
    service = ConnectorService()
    service._mode = "host"
    service._room_server = SimpleNamespace(sync_stop=lambda: None)
    service._easy_tier_node = SimpleNamespace(stop=lambda: None)

    service.close()
    assert service._mode == "idle"
    assert service._room_server is None
    assert service._easy_tier_node is None


def test_florolding_host_uses_room_specific_scaffolding_ipv4(monkeypatch) -> None:
    florolding_module = importlib.import_module("ECL.services.florolding.Florolding.Florolding")
    captured: dict[str, object] = {}

    class Node:
        def __init__(self, config) -> None:
            captured["config"] = config
            self.config = config

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def peer_id(self) -> int:
            return 123

        def node_info(self) -> dict[str, object]:
            return {"ipv4_addr": self.config["ipv4"]}

        def connectors(self) -> list[dict[str, str]]:
            return [{"status": "Connected"}]

        def latest_error(self):
            return None

    class Server:
        def __init__(self, **_kwargs) -> None:
            return None

        async def start(self) -> None:
            return None

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(florolding_module.easytier_pyo3, "Node", Node)
    monkeypatch.setattr(florolding_module, "AsyncFloroldingServer", Server)
    monkeypatch.setattr(florolding_module, "Thread", ImmediateThread)
    monkeypatch.setattr(florolding_module, "find_free_port", lambda: 38123)

    room_code, _server, _node = florolding_module.Florolding().create_room("Host", 25565)
    config = captured["config"]

    assert room_code.startswith("U/")
    assert config["dhcp"] is False
    assert config["ipv4"] == florolding_module.Florolding.host_virtual_ipv4(room_code)
    assert config["ipv4"].startswith("10.")
    assert config["ipv4"].endswith(".1/24")
    assert config["hostname"] == "scaffolding-mc-server-38123"


def test_florolding_host_ipv4_is_stable_per_room() -> None:
    florolding_module = importlib.import_module("ECL.services.florolding.Florolding.Florolding")

    first = florolding_module.Florolding.host_virtual_ipv4("U/AAAA-AAAA-AAAA-AAAA")
    repeated = florolding_module.Florolding.host_virtual_ipv4("U/AAAA-AAAA-AAAA-AAAA")
    other = florolding_module.Florolding.host_virtual_ipv4("U/BBBB-BBBB-BBBB-BBBB")

    assert first == repeated
    assert first != other


async def test_florolding_server_reads_coalesced_requests_without_dropping_frames() -> None:
    client_module = importlib.import_module("ECL.services.florolding.Florolding.F_Client")
    server_module = importlib.import_module("ECL.services.florolding.Florolding.F_Server")
    reader = asyncio.StreamReader()
    reader.feed_data(
        client_module.AsyncFloroldingClient._create_request("c:ping", b"first")
        + client_module.AsyncFloroldingClient._create_request("c:ping", b"second")
    )

    assert await server_module.AsyncFloroldingServer._read_request(reader) == ("c:ping", b"first")
    assert await server_module.AsyncFloroldingServer._read_request(reader) == ("c:ping", b"second")


async def test_florolding_client_serializes_concurrent_requests() -> None:
    client_module = importlib.import_module("ECL.services.florolding.Florolding.F_Client")
    client = client_module.AsyncFloroldingClient(machine_id="m1", easytier_id=1, player_name="Player")
    reader = asyncio.StreamReader()

    class Writer:
        def __init__(self) -> None:
            self.pending: list[bytes] = []
            self.active = 0
            self.max_active = 0

        def write(self, data: bytes) -> None:
            self.pending.append(data)

        async def drain(self) -> None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            request = self.pending.pop(0)
            body_length = int.from_bytes(request[-4:], "big") if request.endswith(b"\0\0\0\0") else 0
            response_body = b"ok" if body_length == 0 else b""
            reader.feed_data(bytes([0]) + len(response_body).to_bytes(4, "big") + response_body)
            self.active -= 1

    writer = Writer()
    client.reader = reader
    client.writer = writer

    responses = await asyncio.gather(
        client.send_request("c:server_port"),
        client.send_request("c:player_profiles_list"),
    )

    assert responses == [(0, b"ok"), (0, b"ok")]
    assert writer.max_active == 1


async def test_run_in_daemon_returns_result() -> None:
    from ECL.api.connector import _run_in_daemon

    assert await _run_in_daemon(lambda: 42) == 42


async def test_run_in_daemon_uses_daemon_thread() -> None:
    from ECL.api.connector import _run_in_daemon

    captured: dict[str, bool] = {}

    def probe() -> str:
        captured["daemon"] = threading.current_thread().daemon
        return "ok"

    assert await _run_in_daemon(probe) == "ok"
    assert captured["daemon"] is True


@pytest.fixture
def nat_probe(monkeypatch):
    class Node:
        def __init__(self, config) -> None:
            self.stopped = False
            self.info = {"stun_info": {"udp_nat_type": "FullCone", "public_ip": ["203.0.113.9"]}}
            self.failure = ""

        def start(self) -> None:
            if self.failure == "start":
                raise RuntimeError("start failed")

        def node_info(self):
            if self.failure == "read":
                raise RuntimeError("read failed")
            return self.info

        def stop(self) -> None:
            self.stopped = True
            if self.failure == "stop":
                raise RuntimeError("stop failed")

    node = Node({})
    monkeypatch.setattr("ECL.services.connector.easytier_pyo3.Node", lambda config: node)
    monkeypatch.setattr("ECL.services.connector.sleep", lambda seconds: None)
    return node


@pytest.mark.parametrize("phase", ["start", "read"])
def test_nat_failure_cleans_node_and_allows_retry(nat_probe, phase: str) -> None:
    from ECL.services.connector import ConnectorNatError

    service = ConnectorService()
    nat_probe.failure = phase
    with pytest.raises(ConnectorNatError) as captured:
        service.get_nat_type()
    assert captured.value.error_code == "CONNECTOR_NAT_TYPE_FAILED"
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert nat_probe.stopped
    nat_probe.failure = ""
    assert service.get_nat_type()["detailType"] == "fullCone"


def test_nat_creation_failure_releases_lock(monkeypatch, nat_probe) -> None:
    from ECL.services.connector import ConnectorNatError

    def fail(config):
        raise RuntimeError("creation failed")

    service = ConnectorService()
    monkeypatch.setattr("ECL.services.connector.easytier_pyo3.Node", fail)
    with pytest.raises(ConnectorNatError) as captured:
        service.get_nat_type()
    assert captured.value.error_code == "CONNECTOR_NAT_TYPE_FAILED"
    monkeypatch.setattr("ECL.services.connector.easytier_pyo3.Node", lambda config: nat_probe)
    assert service.get_nat_type()["type"] == "cone"


def test_nat_empty_timeout_is_not_success(nat_probe) -> None:
    from ECL.services.connector import ConnectorNatError

    service = ConnectorService()
    service.nat_probe_timeout_seconds = 0
    with pytest.raises(ConnectorNatError) as captured:
        service.get_nat_type()
    assert captured.value.error_code == "CONNECTOR_NAT_TYPE_TIMEOUT"
    assert nat_probe.stopped


@pytest.mark.parametrize("public_ips,has_partial_result", [([], False), (["203.0.113.9"], True)])
def test_nat_unknown_requires_probe_information(monkeypatch, nat_probe, public_ips, has_partial_result: bool) -> None:
    from ECL.services.connector import ConnectorNatError

    nat_probe.info = {"stun_info": {"udp_nat_type": "Unknown", "public_ip": public_ips}}
    ticks = iter([0.0, 0.0, 26.0])
    monkeypatch.setattr("ECL.services.connector.monotonic", lambda: next(ticks))
    service = ConnectorService()
    if has_partial_result:
        result = service.get_nat_type()
        assert result["type"] == "unknown"
        assert result["publicIp"] == "203.0.113.9"
    else:
        with pytest.raises(ConnectorNatError) as captured:
            service.get_nat_type()
        assert captured.value.error_code == "CONNECTOR_NAT_TYPE_TIMEOUT"
    assert nat_probe.stopped


def test_nat_room_node_is_never_stopped(nat_probe) -> None:
    service = ConnectorService()
    service._easy_tier_node = nat_probe
    assert service.get_nat_type()["type"] == "cone"
    assert not nat_probe.stopped
    nat_probe.failure = "read"
    from ECL.services.connector import ConnectorNatError

    with pytest.raises(ConnectorNatError):
        service.get_nat_type()
    assert not nat_probe.stopped


def test_nat_udp_blocked_is_valid_result(nat_probe) -> None:
    nat_probe.info = {"stun_info": {"udp_nat_type": "UdpBlocked"}}
    result = ConnectorService().get_nat_type()
    assert result["type"] == "blocked"
    assert result["publicIp"] is None
    assert nat_probe.stopped


def test_nat_stop_failure_keeps_original_error(monkeypatch, nat_probe, caplog) -> None:
    from ECL.services.connector import ConnectorNatError

    nat_probe.failure = "stop"

    def fail():
        raise RuntimeError("original failure")

    monkeypatch.setattr(nat_probe, "node_info", fail)
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorNatError) as captured:
        ConnectorService().get_nat_type()
    assert str(captured.value.__cause__) == "original failure"
    assert "停止 NAT" in caplog.text


def test_nat_concurrent_detection_is_rejected(nat_probe, monkeypatch) -> None:
    from ECL.services.connector import ConnectorNatError

    service = ConnectorService()
    entered = threading.Event()
    release = threading.Event()
    results = []
    original = nat_probe.node_info

    def wait():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(nat_probe, "node_info", wait)
    worker = threading.Thread(target=lambda: results.append(service.get_nat_type()))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ConnectorNatError) as captured:
            service.get_nat_type()
        assert captured.value.error_code == "CONNECTOR_NAT_TYPE_BUSY"
        assert not nat_probe.stopped
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0]["type"] == "cone"
    assert service.get_nat_type()["type"] == "cone"


@pytest.mark.parametrize(
    "error_code", ["CONNECTOR_NAT_TYPE_TIMEOUT", "CONNECTOR_NAT_TYPE_BUSY", "CONNECTOR_NAT_TYPE_FAILED"]
)
async def test_nat_ipc_preserves_domain_error_codes(error_code: str) -> None:
    from ECL.api.connector import ConnectorHandlers
    from ECL.services.connector import ConnectorNatError

    def fail():
        raise ConnectorNatError("探测未完成", error_code)

    state = SimpleNamespace(
        connector=SimpleNamespace(get_nat_type=fail),
        logger=logging.getLogger("test"),
        events=SimpleNamespace(emit=lambda *args: None),
    )
    result = await ConnectorHandlers.connector_nat_type(state, {})
    assert result == {"success": False, "message": "探测未完成", "errorCode": error_code, "presentation": "message"}
