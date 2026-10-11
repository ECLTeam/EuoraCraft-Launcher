from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event

import pytest

from ecl_plugin_api._rpc import RpcPeer, RpcRequest
from ecl_plugin_api.contracts import ProtocolError, WireMessage


class ByteEndpoint:
    def __init__(self, incoming, outgoing) -> None:
        self.incoming = incoming
        self.outgoing = outgoing
        self.closed = Event()

    def send(self, message: WireMessage, timeout: float = 5) -> None:
        if self.closed.is_set():
            raise EOFError
        self.outgoing.put(message.encode(), timeout=timeout)

    def receive(self, timeout: float = 5) -> WireMessage:
        try:
            raw = self.incoming.get(timeout=timeout)
        except Empty as exc:
            raise TimeoutError from exc
        if raw is None:
            raise EOFError
        return WireMessage.decode(raw)

    def close(self) -> None:
        self.closed.set()
        self.incoming.put(None)
        self.outgoing.put(None)


@pytest.fixture
def peers():
    created = []

    def create(host_handler, worker_handler):
        host_input, worker_input = Queue(), Queue()
        host_channel, worker_channel = ByteEndpoint(host_input, worker_input), ByteEndpoint(worker_input, host_input)
        host, worker = RpcPeer(host_channel, "host", host_handler), RpcPeer(worker_channel, "worker", worker_handler)
        created.extend((host, worker))
        host.start()
        worker.start()
        return host, worker, host_channel, worker_channel

    yield create
    for peer in created:
        peer.close()


def echo(request: RpcRequest):
    return {"value": request.params.get("value")}


def test_json_rpc_roundtrip_and_mutation_key(peers) -> None:
    def handler(request):
        assert request.mutation_key == "once"
        request.check_deadline()
        return {"value": request.params["value"]}

    host, _worker, _, _ = peers(echo, handler)
    assert host.call("test.echo", {"value": [1, "中文", None]}, mutation_key="once") == {
        "ok": True,
        "data": {"value": [1, "中文", None]},
    }


def test_callback_can_call_host_while_host_waits_without_deadlock(peers) -> None:
    worker = None

    def reverse(request):
        return worker.call("settings.get", request.params)["data"]

    host, worker, _, _ = peers(echo, reverse)
    assert host.call("callback.invoke", {"value": "nested"}, timeout=2)["data"] == {"value": "nested"}


def test_concurrent_calls_are_correlated_even_when_results_reorder(peers) -> None:
    host, _worker, _, _ = peers(echo, echo)
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda value: host.call("echo", {"value": value})["data"]["value"], range(24)))
    assert values == list(range(24))


def test_expired_wait_drops_late_response_and_preserves_next_call(peers) -> None:
    started, release, finished = Event(), Event(), Event()

    def handler(request):
        if request.method == "slow":
            started.set()
            assert release.wait(2)
            finished.set()
        return {"value": request.method}

    host, _worker, _, _ = peers(echo, handler)
    try:
        with pytest.raises(ProtocolError) as error:
            host.call("slow", {}, timeout=0.03)
        assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
        assert started.is_set()
    finally:
        release.set()
    assert finished.wait(2)
    assert host.call("fast", {}) == {"ok": True, "data": {"value": "fast"}}


def test_callback_exception_is_stable_and_private_text_is_not_transferred(peers) -> None:
    def handler(_request):
        raise RuntimeError("private-token-not-for-ipc")

    host, _worker, _, _ = peers(echo, handler)
    result = host.call("raises", {})
    assert result["error"]["code"] == "PLUGIN_CALLBACK_FAILED"
    assert "private-token" not in str(result)


def test_oversized_callback_response_is_rejected_without_poisoning_channel(peers) -> None:
    def handler(request):
        return {"value": "x" * (1024 * 1024)} if request.method == "large" else {"value": "small"}

    host, _worker, _, _ = peers(echo, handler)
    assert host.call("large", {})["error"]["code"] == "PLUGIN_MESSAGE_TOO_LARGE"
    assert host.call("small", {})["data"]["value"] == "small"


def test_close_wakes_waiting_calls_without_claiming_callback_stopped(peers) -> None:
    started, release, finished = Event(), Event(), Event()

    def handler(_request):
        started.set()
        try:
            assert release.wait(5)
            return {}
        finally:
            finished.set()

    host, _worker, _, _ = peers(echo, handler)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(host.call, "slow", {}, 4)
        assert started.wait(2)
        host.close()
        with pytest.raises(ProtocolError) as error:
            future.result(timeout=2)
        assert error.value.error_code == "PLUGIN_PIPE_CLOSED"
        assert not finished.is_set()
        release.set()
    assert finished.wait(2)


def test_request_replay_is_rejected_by_connection_sequence(peers) -> None:
    host, worker, channel, _ = peers(echo, echo)
    assert host.call("echo", {})["ok"]
    channel.send(WireMessage("request", {}, "host:1", "echo"))
    assert worker.wait_closed(2)
    assert host.wait_closed(2)


def test_malformed_reply_closes_channel_and_releases_waiter(peers) -> None:
    started, release = Event(), Event()

    def handler(_request):
        started.set()
        assert release.wait(3)
        return {}

    host, _worker, _, channel = peers(echo, handler)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(host.call, "slow", {}, 2)
            assert started.wait(2)
            channel.send(WireMessage("response", {"ok": 1, "data": {}}, "host:1"))
            with pytest.raises(ProtocolError):
                future.result(timeout=2)
    finally:
        release.set()


def test_pending_calls_have_a_bounded_capacity(peers, monkeypatch) -> None:
    monkeypatch.setattr(RpcPeer, "max_pending", 1)
    started, release = Event(), Event()

    def handler(_request):
        started.set()
        assert release.wait(3)
        return {}

    host, _worker, _, _ = peers(echo, handler)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(host.call, "slow", {}, 2)
        assert started.wait(2)
        try:
            with pytest.raises(ProtocolError) as error:
                host.call("extra", {})
            assert error.value.error_code == "PLUGIN_RATE_LIMITED"
        finally:
            release.set()
        assert future.result(timeout=2)["ok"]


def test_control_shutdown_is_acknowledged_and_threads_exit(peers) -> None:
    host, worker, _, _ = peers(echo, echo)
    host.shutdown()
    assert host.wait_closed(1) and worker.wait_closed(1)
    assert all(not thread.is_alive() for thread in host._threads)


@pytest.mark.parametrize("timeout", [True, 0, -1, 121, float("nan"), float("inf")])
def test_invalid_time_budget_is_rejected(peers, timeout) -> None:
    host, _worker, _, _ = peers(echo, echo)
    with pytest.raises(ProtocolError):
        host.call("echo", {}, timeout)
