from __future__ import annotations

import base64
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address
from threading import Event
from time import monotonic, sleep

import httpcore
import pytest
from plugin_network_helpers import MappedDialer, decide, make_runtime, server
from plugin_network_helpers import trusted_ssl_context as ssl_context

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.network_targets import NetworkOrigin
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.network_api import RuntimeNetwork


def read(runtime, url, *, method="GET", headers=None, timeout=4):
    return runtime.handle({"method": method, "url": url, "headers": headers or {}}, monotonic() + timeout)


def wait_idle(runtime):
    deadline = monotonic() + 3
    while not runtime.is_idle():
        assert monotonic() < deadline
        sleep(0.01)


@pytest.mark.parametrize("is_tls", [False, True])
def test_real_numeric_connection_host_sni_identity_and_body(tmp_path, monkeypatch, is_tls):
    with server(
        lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nETag: test\r\nSet-Cookie: private\r\n\r\nhello",
        is_tls=is_tls,
    ) as (port, requests, sni):
        scheme = "https" if is_tls else "http"
        reference = f"{scheme}:p10.example.test:{port}"
        dialer = MappedDialer()
        runtime, _ = make_runtime(
            tmp_path, [reference], resolver=lambda *_: ("93.184.216.34",), dialer=dialer, ssl_context=ssl_context()
        )
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("SSL_CERT_FILE", "missing-private-ca")
        try:
            result = read(runtime, f"{scheme}://p10.example.test:{port}/a?private-query=1")
            assert base64.b64decode(result["body_b64"]) == b"hello"
            assert result["headers"] == {"content-length": "5", "etag": "test"}
            assert dialer.connects == [("93.184.216.34", port)]
            assert requests[0][2]["host"] == f"p10.example.test:{port}"
            assert requests[0][2]["accept-encoding"] == "identity"
            assert sni == (["p10.example.test"] if is_tls else [])
            assert max(dialer.read_sizes) <= 8192
            assert runtime.is_idle()
        finally:
            runtime.close()
            assert runtime.service.close()


def test_wrong_certificate_fails_before_http_send(tmp_path):
    with server(lambda *_: b"", is_tls=True) as (port, requests, _sni):
        dialer = MappedDialer()
        runtime, _ = make_runtime(
            tmp_path,
            [f"https:wrong.example.test:{port}"],
            resolver=lambda *_: ("1.1.1.1",),
            dialer=dialer,
            ssl_context=ssl_context(),
        )
        try:
            with pytest.raises(ProtocolError) as error:
                read(runtime, f"https://wrong.example.test:{port}/")
            assert error.value.error_code == "PLUGIN_NETWORK_FAILED"
            assert not dialer.writes and not requests
            assert runtime.is_idle()
        finally:
            runtime.close()
            assert runtime.service.close()


@pytest.mark.parametrize(
    "results,code",
    [
        (("127.0.0.1",), "PLUGIN_NETWORK_TARGET_DENIED"),
        (("8.8.8.8", "169.254.169.254"), "PLUGIN_NETWORK_TARGET_DENIED"),
        (("2606:4700:4700::1111", "::ffff:8.8.8.8"), "PLUGIN_NETWORK_TARGET_DENIED"),
        (("fe80::1%eth0",), "PLUGIN_NETWORK_TARGET_DENIED"),
        (("2002::1",), "PLUGIN_NETWORK_TARGET_DENIED"),
        ((), "PLUGIN_NETWORK_RESOLUTION_FAILED"),
        (tuple(f"8.8.8.{i}" for i in range(1, 18)), "PLUGIN_NETWORK_RESOLUTION_FAILED"),
        (("bad-address",), "PLUGIN_NETWORK_RESOLUTION_FAILED"),
    ],
)
def test_dns_policy_rejects_whole_result_without_dialing(tmp_path, results, code):
    dialer = MappedDialer()
    runtime, _ = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=lambda *_: results, dialer=dialer)
    try:
        with pytest.raises(ProtocolError) as error:
            read(runtime, "http://p10.example.test/")
        assert error.value.error_code == code
        assert not dialer.connects
        assert runtime.is_idle()
    finally:
        runtime.close()
        assert runtime.service.close()


def test_peer_mismatch_does_not_send_http(tmp_path):
    with server(lambda *_: b"") as (port, requests, _sni):
        dialer = MappedDialer()
        dialer.is_peer_mismatched = True
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=dialer
        )
        try:
            with pytest.raises(ProtocolError) as error:
                read(runtime, f"http://p10.example.test:{port}/")
            assert error.value.error_code == "PLUGIN_NETWORK_TARGET_DENIED"
            assert not dialer.writes and not requests
        finally:
            runtime.close()
            assert runtime.service.close()


@pytest.mark.parametrize(
    "raw,method,code,size",
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 32768\r\n\r\n" + b"x" * 32768, "GET", None, 32768),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 32769\r\n\r\n" + b"x" * 32769, "GET", "PLUGIN_RESPONSE_TOO_LARGE", 0),
        (b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 32769, "GET", "PLUGIN_RESPONSE_TOO_LARGE", 0),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8001\r\n" + b"x" * 32769 + b"\r\n0\r\n\r\n",
            "GET",
            "PLUGIN_RESPONSE_TOO_LARGE",
            0,
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\nContent-Encoding: gzip\r\n\r\n", "HEAD", None, 0),
        (b"HTTP/1.1 304 Not Modified\r\nContent-Length: 999999\r\n\r\n", "GET", None, 0),
        (b"HTTP/1.1 204 No Content\r\n\r\n", "GET", None, 0),
        (b"HTTP/1.1 404 Not Found\r\nContent-Length: 3\r\n\r\nerr", "GET", None, 3),
        (b"HTTP/1.1 500 Failure\r\nContent-Length: 0\r\n\r\n", "GET", None, 0),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Encoding: gzip\r\n\r\nabc",
            "GET",
            "PLUGIN_NETWORK_ENCODING_UNSUPPORTED",
            0,
        ),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort", "GET", "PLUGIN_NETWORK_FAILED", 0),
        (b"HTTP/1.1 200 OK\r\nETag: a\r\nETag: b\r\nContent-Length: 0\r\n\r\n", "GET", "PLUGIN_NETWORK_FAILED", 0),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: " + b"x" * 4096 + b"\r\nContent-Length: 0\r\n\r\n",
            "GET",
            "PLUGIN_RESPONSE_TOO_LARGE",
            0,
        ),
        (b"HTTP/1.1 200 OK\r\nLarge: " + b"x" * 300000 + b"\r\n\r\n", "GET", "PLUGIN_NETWORK_FAILED", 0),
        (
            b"HTTP/1.1 103 Early Hints\r\n\r\n" * 12000 + b"HTTP/1.1 204 No Content\r\n\r\n",
            "GET",
            "PLUGIN_RESPONSE_TOO_LARGE",
            0,
        ),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + b"1;" + b"x" * 300000,
            "GET",
            "PLUGIN_NETWORK_FAILED",
            0,
        ),
    ],
    ids=[f"response-{i}" for i in range(16)],
)
def test_real_http_response_limits_and_semantics(tmp_path, raw, method, code, size):
    with server(lambda *_: raw) as (port, _requests, _sni):
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer()
        )
        try:
            if code:
                with pytest.raises(ProtocolError) as error:
                    read(runtime, f"http://p10.example.test:{port}/", method=method)
                assert error.value.error_code == code
            else:
                result = read(runtime, f"http://p10.example.test:{port}/", method=method)
                assert len(base64.b64decode(result["body_b64"])) == size
                assert len(json.dumps(result).encode()) <= 65536
                PluginHostBroker._bounded_result(result)
            assert runtime.is_idle()
        finally:
            runtime.close()
            assert runtime.service.close()


def test_cross_origin_redirect_permanently_removes_authentication(tmp_path):
    def respond(target, headers):
        location = None
        if target == "/":
            location = f"https://other.example.test:{port}/second"
        elif target == "/second":
            location = f"https://p10.example.test:{port}/last"
        if location:
            return f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nSet-Cookie: private\r\nContent-Length: 99999999\r\n\r\n".encode()
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

    with server(respond, is_tls=True) as (port, requests, _sni):
        references = [f"https:{host}:{port}" for host in ("p10.example.test", "other.example.test")]
        dialer = MappedDialer()
        runtime, _ = make_runtime(
            tmp_path, references, resolver=lambda *_: ("1.1.1.1",), dialer=dialer, ssl_context=ssl_context()
        )
        try:
            result = read(
                runtime,
                f"https://p10.example.test:{port}/",
                headers={"Authorization": "private-auth", "X-API-Key": "private-key"},
            )
            assert result["url"].endswith("/last")
            assert requests[0][2]["authorization"] == "private-auth"
            for request in requests[1:]:
                assert not {"authorization", "x-api-key", "cookie"} & request[2].keys()
            assert len(dialer.connects) == 3
        finally:
            runtime.close()
            assert runtime.service.close()


@pytest.mark.parametrize(
    "location,code",
    [
        ("http://unapproved.example/", "PLUGIN_RESOURCE_DENIED"),
        ("/", "PLUGIN_REDIRECT_DENIED"),
        ("http://127.0.0.1/", "PLUGIN_REDIRECT_DENIED"),
        ("/a#fragment", "PLUGIN_REDIRECT_DENIED"),
        ("", "PLUGIN_REDIRECT_DENIED"),
        ("/" + "x" * 4096, "PLUGIN_REDIRECT_DENIED"),
    ],
)
def test_redirect_rejected_before_next_connection(tmp_path, location, code):
    with server(lambda *_: f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()) as (
        port,
        _requests,
        _sni,
    ):
        dialer = MappedDialer()
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=dialer
        )
        try:
            with pytest.raises(ProtocolError) as error:
                read(runtime, f"http://p10.example.test:{port}/")
            assert error.value.error_code == code
            assert len(dialer.connects) == 1
        finally:
            runtime.close()
            assert runtime.service.close()


@pytest.mark.parametrize("stage", ["after_connect", "before_tls", "after_tls"])
def test_revoke_at_real_connection_tls_boundaries_never_sends_http(tmp_path, stage):
    with server(lambda *_: b"", is_tls=True) as (port, requests, _sni):
        references = [f"https:p10.example.test:{port}"]
        dialer = MappedDialer()
        runtime, store = make_runtime(
            tmp_path, references, resolver=lambda *_: ("1.1.1.1",), dialer=dialer, ssl_context=ssl_context()
        )
        setattr(dialer, stage, lambda: decide(store, runtime.session, [], is_allowed=False))
        try:
            with pytest.raises(PluginGrantError):
                read(runtime, f"https://p10.example.test:{port}/")
            assert not dialer.writes and not requests
            assert runtime.is_idle()
        finally:
            runtime.close()
            assert runtime.service.close()


def test_dns_timeout_keeps_real_pending_capacity_and_never_connects_late(tmp_path):
    entered, release = Event(), Event()
    dialer = MappedDialer()

    def resolve(*_):
        entered.set()
        assert release.wait(3)
        return ("1.1.1.1",)

    runtime, _ = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=resolve, dialer=dialer)
    try:
        with pytest.raises(ProtocolError) as error:
            read(runtime, "http://p10.example.test/", timeout=0.1)
        assert entered.is_set() and error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
        assert not runtime.is_idle()
        runtime.close()
        assert not runtime.service.close()
        release.set()
        wait_idle(runtime)
        assert runtime.service.close()
        assert not dialer.connects
    finally:
        release.set()
        runtime.close()
        runtime.service.close()


def test_run_and_dns_quotas_are_not_released_when_waiter_times_out(tmp_path):
    entered, release = Event(), Event()
    dialer = MappedDialer()

    def resolve(*_):
        entered.set()
        assert release.wait(4)
        return ("1.1.1.1",)

    runtime, _ = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=resolve, dialer=dialer)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(read, runtime, "http://p10.example.test/", timeout=0.15) for _ in range(2)]
            assert entered.wait(1)
            for future in pending:
                with pytest.raises(ProtocolError):
                    future.result(2)
        with pytest.raises(ProtocolError) as error:
            read(runtime, "http://p10.example.test/")
        assert error.value.error_code == "PLUGIN_RATE_LIMITED"
        assert not runtime.is_idle()
    finally:
        runtime.close()
        release.set()
        wait_idle(runtime)
        assert runtime.service.close()


def test_revocation_during_body_wakes_reader_and_does_not_affect_other_run(tmp_path):
    entered, release = Event(), Event()

    def respond(*_):
        yield b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\none"
        entered.set()
        release.wait(3)
        yield b"two"

    with server(respond) as (port, _requests, _sni):
        references = [f"http:p10.example.test:{port}"]
        runtime, store = make_runtime(tmp_path, references, resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer())
        other_session = store.open_session(store.new_installation_id(), runtime.session.binding)
        decide(store, other_session, references)
        other = RuntimeNetwork(other_session, runtime.service)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(read, runtime, f"http://p10.example.test:{port}/")
                assert entered.wait(1)
                decide(store, runtime.session, [], is_allowed=False)
                runtime.refresh_permissions()
                with pytest.raises(PluginGrantError):
                    future.result(2)
            assert not other.is_closed
            assert other.session.authorize("network.read", tuple(references))
            assert runtime.is_idle()
        finally:
            release.set()
            runtime.close()
            other.close()
            assert runtime.service.close()


def test_dns_failure_is_sanitized_and_does_not_retry(tmp_path):
    attempts = []

    def resolve(*_):
        attempts.append(1)
        raise socket.gaierror("private-dns-query")

    runtime, _ = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=resolve)
    try:
        with pytest.raises(ProtocolError) as error:
            read(runtime, "http://p10.example.test/?private-query=1")
        assert error.value.error_code == "PLUGIN_NETWORK_RESOLUTION_FAILED"
        assert "private" not in str(error.value)
        assert attempts == [1]
    finally:
        runtime.close()
        assert runtime.service.close()


@pytest.mark.parametrize("stage", ["after_connect", "after_tls"])
def test_close_during_inflight_connect_or_tls_remains_pending_until_real_return(tmp_path, stage):
    entered, release = Event(), Event()
    with server(lambda *_: b"", is_tls=True) as (port, requests, _sni):
        dialer = MappedDialer()
        runtime, _ = make_runtime(
            tmp_path,
            [f"https:p10.example.test:{port}"],
            resolver=lambda *_: ("1.1.1.1",),
            dialer=dialer,
            ssl_context=ssl_context(),
        )

        def block():
            entered.set()
            assert release.wait(3)

        setattr(dialer, stage, block)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(read, runtime, f"https://p10.example.test:{port}/")
                assert entered.wait(1)
                runtime.close()
                assert not runtime.is_idle()
                assert not runtime.service.close()
                release.set()
                with pytest.raises(ProtocolError):
                    future.result(2)
            assert not dialer.writes and not requests
            wait_idle(runtime)
            assert runtime.service.close()
        finally:
            release.set()
            runtime.close()
            runtime.service.close()


def test_failed_close_retains_real_resource_and_can_be_retried(tmp_path):
    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as (port, _requests, _sni):
        dialer = MappedDialer()
        dialer.close_failures = 20
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=dialer
        )
        try:
            with pytest.raises(ProtocolError):
                read(runtime, f"http://p10.example.test:{port}/")
            assert not runtime.is_idle()
            assert not runtime.service.close()
            dialer.close_failures = 0
            wait_idle(runtime)
            assert runtime.service.close()
        finally:
            dialer.close_failures = 0
            runtime.close()
            runtime.service.close()


def test_host_limit_is_eight_real_calls_across_runs(tmp_path):
    runtime, store = make_runtime(tmp_path, ["http:p10.example.test:80"])
    owners = [runtime]
    calls = []
    try:
        for _ in range(4):
            session = store.open_session(store.new_installation_id(), runtime.session.binding)
            decide(store, session, ["http:p10.example.test:80"])
            owners.append(RuntimeNetwork(session, runtime.service))
        for owner in owners[:4]:
            lease = owner.session.authorize("network.read", ("http:p10.example.test:80",))
            calls.extend(runtime.service.begin(owner, lease, monotonic() + 10) for _ in range(2))
        owner = owners[4]
        lease = owner.session.authorize("network.read", ("http:p10.example.test:80",))
        with pytest.raises(ProtocolError) as error:
            runtime.service.begin(owner, lease, monotonic() + 10)
        assert error.value.error_code == "PLUGIN_RATE_LIMITED"
        assert not runtime.service.close()
    finally:
        for call in calls:
            call.finish()
        for owner in owners:
            owner.close()
        assert runtime.service.close()


def test_dns_four_real_jobs_no_queue_and_late_results_only_release(tmp_path):
    release = Event()
    dialer = MappedDialer()

    def resolve(*_):
        assert release.wait(4)
        return ("1.1.1.1",)

    runtime, store = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=resolve, dialer=dialer)
    owners = [runtime]
    try:
        for _ in range(4):
            session = store.open_session(store.new_installation_id(), runtime.session.binding)
            decide(store, session, ["http:p10.example.test:80"])
            owners.append(RuntimeNetwork(session, runtime.service))
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(read, owner, "http://p10.example.test/", timeout=0.2) for owner in owners[:4]]
            for future in futures:
                with pytest.raises(ProtocolError):
                    future.result(2)
        assert runtime.service._dns_count == 4
        with pytest.raises(ProtocolError) as error:
            read(owners[4], "http://p10.example.test/")
        assert error.value.error_code == "PLUGIN_RATE_LIMITED"
        assert runtime.service._dns_count == 4
    finally:
        release.set()
        for owner in owners:
            owner.close()
            wait_idle(owner)
        assert runtime.service.close()
        assert not dialer.connects


def test_total_deadline_stops_continuous_small_chunks(tmp_path):
    def respond(*_):
        yield b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        for _ in range(20):
            yield b"1\r\nx\r\n"
            sleep(0.05)
        yield b"0\r\n\r\n"

    with server(respond) as (port, _requests, _sni):
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer()
        )
        try:
            started = monotonic()
            with pytest.raises(ProtocolError) as error:
                read(runtime, f"http://p10.example.test:{port}/", timeout=0.2)
            assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
            assert monotonic() - started < 1
            assert runtime.is_idle()
        finally:
            runtime.close()
            assert runtime.service.close()


def test_rebinding_on_redirect_rejects_new_private_result(tmp_path):
    with server(lambda *_: b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n") as (
        port,
        requests,
        _sni,
    ):
        answers = iter([("1.1.1.1",), ("127.0.0.1",)])
        dialer = MappedDialer()
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: next(answers), dialer=dialer
        )
        try:
            with pytest.raises(ProtocolError) as error:
                read(runtime, f"http://p10.example.test:{port}/")
            assert error.value.error_code == "PLUGIN_NETWORK_TARGET_DENIED"
            assert len(dialer.connects) == len(requests) == 1
        finally:
            runtime.close()
            assert runtime.service.close()


def test_broker_idle_retains_orphan_dns_until_actual_return(tmp_path):
    release = Event()

    def resolve(*_):
        assert release.wait(3)
        return ("1.1.1.1",)

    runtime, _ = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=resolve)
    broker = PluginHostBroker(
        runtime.session, tmp_path, lambda *_: {}, run_state=lambda: "enabled", network_service=runtime.service
    )
    try:
        with pytest.raises(ProtocolError):
            broker.handle(
                RpcRequest("network.request", {"method": "GET", "url": "http://p10.example.test/"}, monotonic() + 0.1)
            )
        broker.close()
        assert not broker.wait_idle()
        assert not runtime.service.close()
        release.set()
        deadline = monotonic() + 2
        while not broker.wait_idle():
            assert monotonic() < deadline
            sleep(0.01)
        assert runtime.service.close()
    finally:
        release.set()
        broker.close()
        runtime.close()
        runtime.service.close()


def test_delivery_rechecks_original_lease_after_final_cleanup(tmp_path, monkeypatch):
    from ECL.plugins.runtime.network_api import _NetworkCall

    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, _requests, _sni):
        runtime, store = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer()
        )
        original = _NetworkCall.finish

        def finish(call):
            original(call)
            decide(store, runtime.session, [], is_allowed=False)

        monkeypatch.setattr(_NetworkCall, "finish", finish)
        try:
            with pytest.raises(PluginGrantError):
                read(runtime, f"http://p10.example.test:{port}/")
            assert runtime.is_idle()
        finally:
            runtime.close()
            assert runtime.service.close()


def test_public_backend_with_actual_numeric_peer_preserves_tls_identity_without_second_dns(tmp_path, monkeypatch):
    from ECL.plugins.runtime.network_api import _NetworkCall

    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok", is_tls=True) as (port, requests, sni):
        reference = f"https:p10.example.test:{port}"
        runtime, _ = make_runtime(tmp_path, [reference], dialer=httpcore.SyncBackend(), ssl_context=ssl_context())
        lease = runtime.session.authorize("network.read", (reference,))
        call = _NetworkCall(runtime, lease, monotonic() + 4)
        original = socket.getaddrinfo
        lookups = []

        def lookup(host, *args, **kwargs):
            lookups.append(host)
            assert host == "127.0.0.1"
            return original(host, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", lookup)
        try:
            # 受信传输测试只将数字 peer 指向自己的服务器；RPC 仍必须先通过公网策略。
            with runtime.service.pool(NetworkOrigin.parse(reference), ip_address("127.0.0.1"), call) as pool:
                response = pool.request(
                    "GET", f"https://p10.example.test:{port}/", headers={"Host": f"p10.example.test:{port}"}
                )
                assert response.content == b"ok"
            assert lookups == ["127.0.0.1"]
            assert sni == ["p10.example.test"]
            assert requests[0][2]["Host"] == f"p10.example.test:{port}"
        finally:
            call.finish()
            runtime.close()
            assert runtime.service.close()


def test_worst_url_and_escaped_headers_with_full_body_fit_business_json_budget(tmp_path):
    body = b"x" * 32768
    etag = b'"' * (4096 - len("etag") - len("content-length") - len(str(len(body))))
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 32768\r\nETag: " + etag + b"\r\n\r\n" + body
    with server(lambda *_: raw) as (port, _requests, _sni):
        runtime, _ = make_runtime(
            tmp_path, [f"http:p10.example.test:{port}"], resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer()
        )
        prefix = f"http://p10.example.test:{port}/"
        url = prefix + "x" * (4096 - len(prefix))
        try:
            result = read(runtime, url)
            assert len(result["url"].encode()) == 4096
            assert base64.b64decode(result["body_b64"]) == body
            assert sum(len(name) + len(value) for name, value in result["headers"].items()) == 4096
            assert len(json.dumps(result).encode()) <= 65536
            PluginHostBroker._bounded_result(result)
        finally:
            runtime.close()
            assert runtime.service.close()
