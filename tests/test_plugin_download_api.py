from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from plugin_download_helpers import call, decide, finish, submit, target
from plugin_download_helpers import downloads as downloads
from plugin_network_helpers import MappedDialer, server
from pydantic import ValidationError

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.runtime.download_api import RuntimeDownloads
from ECL.plugins.runtime.models import DownloadSubmitRequest
from ECL.utils.windows_file_handles import FileBoundaryError, FileHandle


def directory(tmp_path, broker):
    return tmp_path / "plugin_data" / broker.session.view().installation_id / "files"


@pytest.mark.parametrize(
    "updates",
    [
        {"overwrite": True},
        {"overwrite": 0},
        {"overwrite": None},
        {"expected_sha256": None},
        {"expected_sha256": "A" * 64},
        {"headers": {}},
        {"method": "HEAD"},
        {"body": ""},
        {"url": "http://127.0.0.1/a"},
        {"url": "http://a/x"},
        {"target": {"root_ref": "a" * 32, "relative_path": "../x"}},
        {"target": {"root_ref": "a" * 32, "relative_path": ""}},
    ],
)
def test_download_dto_refuses_unimplemented_or_ambiguous_inputs(updates):
    params = {
        "url": "https://example.com/a",
        "target": {"root_ref": "a" * 32, "relative_path": "x"},
        "overwrite": False,
    }
    with pytest.raises((ValidationError, ProtocolError)):
        DownloadSubmitRequest.model_validate(params | updates)


@pytest.mark.parametrize(
    "size,framing,is_tls",
    [
        (0, "fixed", False),
        (70000, "fixed", True),
        (70000, "unknown", False),
        (70000, "chunked", True),
        (8388608, "fixed", False),
    ],
)
def test_full_bounded_stream_publishes_only_complete_file(downloads, tmp_path, size, framing, is_tls):
    body = b"x" * size

    def respond(*_):
        if framing == "fixed":
            yield f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n\r\n".encode()
        elif framing == "chunked":
            yield b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        else:
            yield b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n"
        for offset in range(0, size, 8192):
            chunk = body[offset : offset + 8192]
            yield (f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n") if framing == "chunked" else chunk
        if framing == "chunked":
            yield b"0\r\n\r\n"

    create, manager, storage, _ = downloads
    with server(respond, is_tls=is_tls) as (port, requests, _sni):
        scheme = "https" if is_tls else "http"
        broker = create([f"{scheme}:p10.example.test:{port}"])
        view = finish(
            broker,
            manager,
            submit(
                broker, f"{scheme}://p10.example.test:{port}/data", expected_sha256=hashlib.sha256(body).hexdigest()
            ),
        )
        assert (view["state"], view["effect"], view["percent"]) == ("completed", "committed", 100)
        assert view["result"]["size"] == size
        assert (directory(tmp_path, broker) / "download.bin").read_bytes() == body
        assert not storage.uploads(broker.session.run_id)
        assert len(requests) == 1
        headers = {k.lower(): v for k, v in requests[0][2].items()}
        assert headers["accept-encoding"] == "identity" and "range" not in headers
        if size > 32768:
            with pytest.raises(ProtocolError) as exc:
                call(broker, "network.request", {"method": "GET", "url": f"{scheme}://p10.example.test:{port}/data"})
            assert exc.value.error_code == "PLUGIN_RESPONSE_TOO_LARGE"


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 8388609\r\n\r\n", "PLUGIN_RESPONSE_TOO_LARGE"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nshort", "PLUGIN_NETWORK_FAILED"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: nope\r\n\r\n", "PLUGIN_NETWORK_FAILED"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx", "PLUGIN_NETWORK_FAILED"),
        (
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 1\r\n\r\nx",
            "PLUGIN_NETWORK_ENCODING_UNSUPPORTED",
        ),
        *[
            (f"HTTP/1.1 {status} No\r\nContent-Length: 1\r\n\r\nx".encode(), "PLUGIN_DOWNLOAD_FAILED")
            for status in (204, 206, 304, 400, 404, 500)
        ],
        (
            b"HTTP/1.1 302 Found\r\nLocation: http://unapproved.test/x\r\nContent-Length: 0\r\n\r\n",
            "PLUGIN_RESOURCE_DENIED",
        ),
    ],
)
def test_invalid_response_never_publishes_and_does_not_retry(downloads, tmp_path, raw, code):
    create, manager, _, _ = downloads
    with server(lambda *_: raw) as (port, requests, _):
        broker = create([f"http:p10.example.test:{port}"])
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/secret?token=private-value"))
        assert view["state"] == "failed" and view["error_code"] == code
        assert view["effect"] == "none" and view["result"] is None
        assert not list(directory(tmp_path, broker).iterdir())
        assert len(requests) == 1
        audits = list((tmp_path / "operations/plugin").rglob("*.json"))
        assert audits and all("private-value" not in path.read_text(encoding="utf-8") for path in audits)


def test_unknown_length_over_limit_cleans_all_staging(downloads, tmp_path):
    create, manager, _, _ = downloads

    def respond(*_):
        yield b"HTTP/1.1 200 OK\r\n\r\n"
        for _ in range(1025):
            yield b"x" * 8192

    with server(respond) as (port, _, _):
        broker = create([f"http:p10.example.test:{port}"])
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/"))
        assert view["error_code"] == "PLUGIN_RESPONSE_TOO_LARGE"
        assert not list(directory(tmp_path, broker).iterdir())


@pytest.mark.parametrize("permission", ["network.read"])
def test_missing_either_permission_rejects_before_network_or_staging(downloads, tmp_path, permission):
    create, _, storage, _ = downloads
    dialer = MappedDialer()
    broker = create(["https:example.com:443"], allowed={"network.read"} - {permission}, dialer=dialer)
    params = {
        "url": "https://example.com/x",
        "target": {"root_ref": "a" * 32, "relative_path": "x"},
        "overwrite": False,
    }
    with pytest.raises(ProtocolError):
        call(broker, "downloads.submit", params)
    assert not dialer.connects and not storage.uploads(broker.session.run_id)
    assert not directory(tmp_path, broker).exists()


def test_same_mutation_key_concurrently_returns_one_task(downloads, tmp_path):
    create, manager, _, _ = downloads
    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, requests, _):
        broker = create([f"http:p10.example.test:{port}"])
        params = {"url": f"http://p10.example.test:{port}/", "target": target(broker), "overwrite": False}
        with ThreadPoolExecutor(max_workers=4) as executor:
            receipts = list(executor.map(lambda _: call(broker, "downloads.submit", params, "same-download"), range(4)))
        assert all(receipt == receipts[0] for receipt in receipts)
        assert finish(broker, manager, receipts[0])["state"] == "completed"
        assert len(requests) == 1
        assert call(broker, "mutations.get", {"mutation_key": "same-download"})["result"] == receipts[0]
        decide(broker, downloads[3], "network.read")
        assert call(broker, "mutations.get", {"mutation_key": "same-download"})["result"] == receipts[0]


@pytest.mark.parametrize("action", ["cancel", "network.read", "plugin.base", "close"])
def test_blocked_read_is_woken_without_false_idle(downloads, tmp_path, action):
    create, manager, storage, store = downloads
    entered, release = Event(), Event()

    def respond(*_):
        yield b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\nfirst"
        entered.set()
        release.wait(5)

    with server(respond) as (port, _, _):
        broker = create([f"http:p10.example.test:{port}"])
        receipt = submit(broker, f"http://p10.example.test:{port}/")
        try:
            assert entered.wait(2)
            if action == "cancel":
                assert call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})["requested"]
            elif action == "close":
                broker.close()
            else:
                decide(broker, store, action)
            broker.network.refresh_permissions()
            operation = manager._operations[broker.operations._references[receipt["operation_ref"]].operation_id]
            operation.future.result(2)
            assert operation.status in {"cancelled", "failed"}
            assert broker.wait_idle(1)
            assert not list(directory(tmp_path, broker).iterdir())
            assert not storage.uploads(broker.session.run_id)
        finally:
            release.set()


def test_orphan_dns_keeps_network_and_task_cleanup_pending(downloads, tmp_path, monkeypatch):
    create, manager, storage, _ = downloads
    entered, release = Event(), Event()

    def resolver(*_):
        entered.set()
        release.wait(5)
        return ("1.1.1.1",)

    broker = create(["https:example.com:443"], resolver=resolver)
    monkeypatch.setattr(RuntimeDownloads, "task_seconds", 0.15)
    try:
        receipt = submit(broker, "https://example.com/data")
        assert entered.wait(1)
        view = finish(broker, manager, receipt)
        assert view["error_code"] == "PLUGIN_DEADLINE_EXCEEDED"
        assert not storage.uploads(broker.session.run_id)
        assert not broker.wait_idle()
        assert not broker.network.service._dialer.connects
    finally:
        release.set()
    for call_record in broker.network.service.calls(broker.network):
        if call_record._dns_future:
            call_record._dns_future.result(2)
    assert broker.wait_idle(1)


@pytest.mark.parametrize("point", ["before", "after"])
def test_commit_acceptance_orders_cancel_and_preserves_real_result(downloads, tmp_path, monkeypatch, point):
    create, manager, _, _ = downloads
    original = FileHandle.publish

    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, _, _):
        broker = create([f"http:p10.example.test:{port}"])
        gate, release = Event(), Event()
        original_digest = FileHandle.digest

        def digest(self, deadline):
            gate.set()
            assert release.wait(3)
            return original_digest(self, deadline)

        monkeypatch.setattr(FileHandle, "digest", digest)
        receipt = submit(broker, f"http://p10.example.test:{port}/")
        assert gate.wait(2)
        if point == "before":
            call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
        else:
            # 已进入原生发布时 cancel 应明确拒绝，不影响发布；模拟外部请求被拒。
            def publish(self, name):
                with pytest.raises(ProtocolError) as exc:
                    call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
                assert exc.value.error_code == "PLUGIN_COMMIT_STARTED"
                return original(self, name)

            monkeypatch.setattr(FileHandle, "publish", publish)
        release.set()
        view = finish(broker, manager, receipt)
        assert view["state"] == ("cancelled" if point == "before" else "completed")
        assert (directory(tmp_path, broker) / "download.bin").exists() == (point == "after")


def test_hash_mismatch_and_basic_result_remains_bound_to_original_epoch(downloads, tmp_path):
    create, manager, _, _ = downloads
    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, _, _):
        refs = [f"http:p10.example.test:{port}"]
        broker = create(refs, allowed={"network.read"})
        url = f"http://p10.example.test:{port}/"
        failed = finish(broker, manager, submit(broker, url, expected_sha256="0" * 64))
        assert failed["error_code"] == "PLUGIN_HASH_MISMATCH" and failed["effect"] == "none"
        view = finish(broker, manager, submit(broker, url))
        assert view["state"] == "completed" and view["result"] is not None
        decide(broker, downloads[3], "plugin.base", "allow")
        assert call(broker, "operations.get", {"operation_ref": view["operation_ref"]})["result"] is None


def test_late_target_collision_never_overwrites_existing_file(downloads, tmp_path, monkeypatch):
    create, manager, storage, _ = downloads
    original = FileHandle.publish
    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, _, _):
        broker = create([f"http:p10.example.test:{port}"])

        def publish(self, name):
            (directory(tmp_path, broker) / name).write_bytes(b"original")
            return original(self, name)

        monkeypatch.setattr(FileHandle, "publish", publish)
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/"))
        assert view["error_code"] == "PLUGIN_FILE_EXISTS"
        assert (directory(tmp_path, broker) / "download.bin").read_bytes() == b"original"
        assert not storage.uploads(broker.session.run_id)


def test_dual_lease_acceptance_rechecks_under_one_lock(downloads, monkeypatch):
    from ECL.plugins.grant_store import PluginGrantSession

    create, _, _, store = downloads
    broker = create(["https:example.com:443"])
    leases = (
        broker.session.authorize("plugin.base"),
        broker.session.authorize("network.read", ("https:example.com:443",)),
    )
    accepted = Event()
    original = PluginGrantSession.check_all

    def check(self, current):
        original(self, current)
        decide(broker, store, "network.read")

    monkeypatch.setattr(PluginGrantSession, "check_all", check)
    with pytest.raises(PluginGrantError):
        broker.session.accept_all(leases, accepted.set)
    assert not accepted.is_set()


def test_queued_downloads_share_upload_quota_and_cancel_without_network(downloads, tmp_path):
    create, manager, storage, _ = downloads
    broker = create(["https:example.com:443"])
    release, entered = Event(), Event()

    def blocker(context):
        entered.set()
        release.wait(5)

    manager.submit("hold", blocker)
    assert entered.wait(1)
    try:
        receipts = [submit(broker, "https://example.com/x", f"{number}.bin") for number in range(4)]
        assert sum(item.reserved_bytes for item in storage.uploads(broker.session.run_id)) == 32 * 1024 * 1024
        assert all(item.expected_bytes is None for item in storage.uploads(broker.session.run_id))
        with pytest.raises(ProtocolError) as exc:
            submit(broker, "https://example.com/x", "fifth.bin")
        assert exc.value.error_code == "PLUGIN_RATE_LIMITED"
        for receipt in receipts:
            call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
        # 请求取消尚不代表排队 future 已经结束，预留不能提前归还。
        assert storage.uploads(broker.session.run_id)
        assert not broker.network.service._dialer.connects
        release.set()
        for receipt in receipts:
            assert finish(broker, manager, receipt)["state"] == "cancelled"
        assert not storage.uploads(broker.session.run_id)
        assert not list(directory(tmp_path, broker).iterdir())
    finally:
        release.set()


@pytest.mark.parametrize("resource", ["network", "staged", "published"])
def test_failed_resource_close_retains_pending_and_retries_without_request_replay(
    downloads, tmp_path, monkeypatch, resource
):
    create, manager, storage, _ = downloads
    dialer = MappedDialer()
    original_discard, original_close = FileHandle.discard, FileHandle.close

    def fail_discard(self):
        raise FileBoundaryError("暂存删除未完成", "PLUGIN_FILE_BUSY")

    def fail_close(self):
        if self.is_published:
            raise FileBoundaryError("已发布句柄关闭未完成", "PLUGIN_FILE_BUSY")
        return original_close(self)

    status = 500 if resource == "staged" else 200
    with server(lambda *_: f"HTTP/1.1 {status} Result\r\nContent-Length: 2\r\n\r\nok".encode()) as (port, requests, _):
        broker = create([f"http:p10.example.test:{port}"], dialer=dialer)
        prepared = call(
            broker, "files.write_begin", {"target": target(broker, "warm.bin"), "total_bytes": 0, "overwrite": False}
        )
        call(broker, "files.write_abort", {"write_ref": prepared["write_ref"]})
        if resource == "network":
            dialer.close_failures = 1000
        elif resource == "staged":
            monkeypatch.setattr(FileHandle, "discard", fail_discard)
        else:
            monkeypatch.setattr(FileHandle, "close", fail_close)
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/"))
        assert not broker.wait_idle()
        assert (directory(tmp_path, broker) / "download.bin").exists() == (resource == "published")
        if resource == "published":
            assert view["state"] == "completed" and view["effect"] == "committed"
        dialer.close_failures = 0
        monkeypatch.setattr(FileHandle, "discard", original_discard)
        monkeypatch.setattr(FileHandle, "close", original_close)
        assert broker.wait_idle(1)
        assert not storage.uploads(broker.session.run_id)
        assert len(requests) == 1


@pytest.mark.parametrize("phase", ["queued", "executing", "committing", "terminal"])
def test_audit_failure_does_not_publish_early_or_replay_completed_download(downloads, tmp_path, monkeypatch, phase):
    import ECL.services.operations as module

    create, manager, _, _ = downloads
    original = module.atomic_write_text

    def write(path, content):
        if json.loads(content)["phase"] == phase:
            raise OSError("审计暂不可写")
        return original(path, content)

    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, requests, _):
        broker = create([f"http:p10.example.test:{port}"])
        monkeypatch.setattr(module, "atomic_write_text", write)
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/"))
        assert (directory(tmp_path, broker) / "download.bin").exists() == (phase == "terminal")
        assert view["effect"] == ("committed" if phase == "terminal" else "none")
        assert len(requests) == (0 if phase in {"queued", "executing"} else 1)
        monkeypatch.setattr(module, "atomic_write_text", original)
        assert broker.wait_idle(1)


@pytest.mark.parametrize("stage", ["before_connect", "after_connect", "before_tls", "after_tls"])
def test_connection_and_tls_revoke_never_send_http(downloads, stage):
    create, manager, _, store = downloads
    dialer = MappedDialer()
    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", is_tls=True) as (port, requests, _):
        broker = create([f"https:p10.example.test:{port}"], dialer=dialer)
        setattr(dialer, stage, lambda: decide(broker, store, "plugin.base"))
        view = finish(broker, manager, submit(broker, f"https://p10.example.test:{port}/"))
        assert view["state"] == "failed" and view["effect"] == "none"
        assert not requests
        assert not dialer.writes


def test_final_commit_cannot_extend_total_deadline(downloads, tmp_path, monkeypatch):
    create, manager, _, _ = downloads
    original = FileHandle.digest
    monkeypatch.setattr(RuntimeDownloads, "task_seconds", 0.25)

    def digest(self, deadline):
        value = original(self, deadline)
        Event().wait(0.3)
        return value

    monkeypatch.setattr(FileHandle, "digest", digest)
    with server(lambda *_: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as (port, _, _):
        broker = create([f"http:p10.example.test:{port}"])
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/"))
        assert view["error_code"] == "PLUGIN_DEADLINE_EXCEEDED"
        assert not list(directory(tmp_path, broker).iterdir())


def test_raw_informational_response_budget_is_bounded(downloads, tmp_path):
    create, manager, _, _ = downloads

    def respond(*_):
        for _ in range(1250):
            yield b"HTTP/1.1 103 Early Hints\r\nX-Pad: " + b"x" * 8000 + b"\r\n\r\n"
        yield b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

    with server(respond) as (port, _, _):
        broker = create([f"http:p10.example.test:{port}"])
        view = finish(broker, manager, submit(broker, f"http://p10.example.test:{port}/"))
        assert view["error_code"] == "PLUGIN_RESPONSE_TOO_LARGE"
        assert not list(directory(tmp_path, broker).iterdir())
