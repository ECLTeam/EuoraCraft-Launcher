from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from ECL.events import EventBus
from ECL.game import Downloader
from ECL.services.custom_downloads import CustomDownloadRequest, CustomDownloadService, DownloadPolicy
from ECL.services.operations import OperationManager
from ECL.utils.errors import GameServiceError


def test_download_filename_corpus_matches_frontend() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[1] / "frontend/src/features/download/fixtures/downloadFilenameCases.json"
    )
    for sample in json.loads(fixture_path.read_text(encoding="utf-8")):
        assert DownloadPolicy.is_valid_name(sample["name"]) is sample["valid"], sample["name"]


def test_operation_revision_and_cancellation_are_executor_owned(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    manager = OperationManager(tmp_path, EventBus())

    def worker(context) -> None:
        context.progress(50, "等待安全退出")
        started.set()
        assert release.wait(timeout=3)
        context.check_cancelled()

    try:
        operation_id = manager.submit("world_copy", worker)["operationId"]
        assert started.wait(timeout=3)
        running = manager.get(operation_id)
        assert running["canCancel"] is True
        assert manager.cancel(operation_id) is True
        cancelling = manager.get(operation_id)
        assert cancelling["status"] == "running"
        assert cancelling["cancellationRequested"] is True
        assert cancelling["revision"] > running["revision"]
        release.set()
        stopped = wait_operation(manager, operation_id)
        assert stopped["status"] == "cancelled"
        assert stopped["canCancel"] is False
        assert stopped["revision"] > cancelling["revision"]
    finally:
        release.set()
        manager.close()


def wait_operation(manager, operation_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.get(operation_id)
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        time.sleep(0.02)
    pytest.fail("下载任务未及时结束")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://host/file",
        "https://user:pw@host/file",
        "http://",
        "https://host:70000/file",
        "https://host/file#fragment",
    ],
)
def test_rejects_unsupported_download_urls(tmp_path, url) -> None:
    with pytest.raises(ValidationError):
        CustomDownloadRequest(url=url, target_path=tmp_path / "file")


def test_rejects_relative_or_directory_target(tmp_path) -> None:
    for destination in (Path("relative.zip"), tmp_path):
        with pytest.raises(ValidationError):
            CustomDownloadRequest(url="https://host/file", target_path=destination)


def test_real_downloader_downloads_atomic_file_and_reports_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    payload = b"EuoraCraft" * 16384

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()

        def do_GET(self):
            self.do_HEAD()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    events = EventBus()
    progress = []
    events.subscribe("game:operation_progress", progress.append)
    manager = OperationManager(tmp_path / "data", events)
    service = CustomDownloadService(manager)
    target = tmp_path / "custom folder" / "文件.bin"
    try:
        operation = service.submit(
            CustomDownloadRequest(url=f"http://127.0.0.1:{server.server_port}/file", target_path=target)
        )
        result = wait_operation(manager, operation["operationId"])
        assert result["status"] == "completed", result
        assert target.read_bytes() == payload
        assert any(item.get("kind") == "custom_download" and item.get("done", 0) > 0 for item in progress)
        assert not list(target.parent.glob(".ecl-download-*"))
        with pytest.raises(GameServiceError, match="文件已存在"):
            service.submit(CustomDownloadRequest(url="http://host/file", target_path=target))
    finally:
        manager.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cancel_stops_download_preserves_existing_file_and_retry_has_new_id(tmp_path) -> None:
    started = threading.Event()
    stopped = threading.Event()
    should_finish = threading.Event()

    class ControlledDownloader:
        def __init__(self, downloads, **kwargs):
            self.path = downloads[0][1]
            self.failed_entries = set()
            self.use_byte_progress = True
            self.client = None

        async def run(self):
            self.path.write_bytes(b"new content")
            started.set()
            while not should_finish.is_set():
                await asyncio.sleep(0.02)

        def stop(self):
            stopped.set()

    manager = OperationManager(tmp_path / "data", EventBus())
    service = CustomDownloadService(manager, ControlledDownloader)
    target = tmp_path / "original.bin"
    target.write_bytes(b"original")
    request = CustomDownloadRequest(url="http://host/file", target_path=target, overwrite=True)
    try:
        first = service.submit(request)
        assert started.wait(timeout=3)
        with pytest.raises(GameServiceError, match="正在执行"):
            service.submit(request)
        assert manager.cancel(first["operationId"])
        assert wait_operation(manager, first["operationId"])["status"] == "cancelled"
        assert stopped.is_set()
        assert target.read_bytes() == b"original"
        assert not list(tmp_path.glob(".ecl-download-*"))
        should_finish.set()
        retried = service.retry(first["operationId"])
        assert retried["operationId"] != first["operationId"]
        assert wait_operation(manager, retried["operationId"])["status"] == "completed"
        assert target.read_bytes() == b"new content"
    finally:
        should_finish.set()
        manager.close()


def test_operation_lookup_cannot_escape_registry(tmp_path) -> None:
    manager = OperationManager(tmp_path, EventBus())
    try:
        with pytest.raises(GameServiceError):
            manager.get("../secret")
    finally:
        manager.close()


def test_cancellation_is_refused_after_atomic_finalization(tmp_path) -> None:
    committed = threading.Event()
    allow_return = threading.Event()
    manager = OperationManager(tmp_path, EventBus())

    def worker(context):
        context.finalize(lambda: committed.set())
        assert allow_return.wait(timeout=3)
        return {"published": True}

    try:
        operation = manager.submit("custom_download", worker)
        assert committed.wait(timeout=3)
        assert manager.cancel(operation["operationId"]) is False
        allow_return.set()
        assert wait_operation(manager, operation["operationId"])["status"] == "completed"
    finally:
        allow_return.set()
        manager.close()


@pytest.fixture
def download_server():
    payload = b"EuoraCraft-file-content" * 60000
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self, should_write):
            requests.append((self.command, self.path, dict(self.headers)))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/final-name.zip")
                self.end_headers()
                return
            if self.command == "HEAD" and self.path == "/no-head":
                self.send_response(405)
                self.end_headers()
                return
            body = payload
            range_header = self.headers.get("Range")
            if range_header:
                first, last = map(int, range_header.removeprefix("bytes=").split("-"))
                body = payload[first : last + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {first}-{last}/{len(payload)}")
            else:
                self.send_response(200)
            if self.path in {"/unicode", "/no-head"}:
                self.send_header(
                    "Content-Disposition", f"attachment; filename=fallback.zip; filename*=UTF-8''{quote('中文.zip')}"
                )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if should_write:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payload, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_defaults_use_injected_data_directory_without_creating_it(tmp_path):
    manager = OperationManager(tmp_path / "state", EventBus())
    service = CustomDownloadService(manager, data_path=tmp_path / "actual-data")
    try:
        assert service.defaults() == {
            "downloadDirectory": str(tmp_path / "actual-data" / "downloads"),
            "userAgent": "EuoraCraft-Launcher",
        }
        assert not (tmp_path / "actual-data" / "downloads").exists()
    finally:
        manager.close()


@pytest.mark.parametrize(
    "route,name",
    [("/unicode", "中文.zip"), ("/redirect", "final-name.zip"), ("/no-head", "中文.zip"), ("/", "download.bin")],
)
def test_original_name_real_download_and_headers(tmp_path, download_server, monkeypatch, route, name):
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    url, payload, requests = download_server
    manager = OperationManager(tmp_path / "state", EventBus())
    service = CustomDownloadService(manager, data_path=tmp_path / "app-data")
    try:
        operation = service.submit(
            CustomDownloadRequest(url=url + route, user_agent="Test-UA/1", headers={"X-Test": "secret"})
        )
        result = wait_operation(manager, operation["operationId"])
        assert result["status"] == "completed", result
        target_path = tmp_path / "app-data" / "downloads" / name
        assert target_path.read_bytes() == payload
        assert result["result"] == {"path": str(target_path), "name": name}
        assert {method for method, _, _ in requests} == {"HEAD", "GET"}
        assert all(headers["User-Agent"] == "Test-UA/1" and headers["X-Test"] == "secret" for _, _, headers in requests)
        snapshot_file = tmp_path / "state" / "operations" / f"{operation['operationId']}.json"
        deadline = time.monotonic() + 3
        while not snapshot_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = snapshot_file.read_text(encoding="utf-8")
        assert "secret" not in snapshot and "Test-UA" not in snapshot
    finally:
        manager.close()


def test_custom_name_exact_and_range_requests_use_headers(tmp_path, download_server, monkeypatch):
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    url, payload, requests = download_server
    manager = OperationManager(tmp_path / "state", EventBus())

    def factory(*args, **kwargs):
        return Downloader(*args, **kwargs, skip_preflight=True, chunk_threshold_mb=0.1, min_chunk_mb=1, max_chunks=4)

    service = CustomDownloadService(manager, factory)
    request = CustomDownloadRequest(
        url=url + "/unicode",
        download_directory=tmp_path / "chosen",
        naming_mode="custom",
        custom_name="exact-name",
        user_agent="Range-UA",
        headers={"Authorization": "test-session-token", "Referer": url},
    )
    try:
        operation = service.submit(request)
        request.headers["Authorization"] = "caller-changed"
        result = wait_operation(manager, operation["operationId"])
        assert result["status"] == "completed", result
        assert (tmp_path / "chosen" / "exact-name").read_bytes() == payload
        assert any("Range" in headers for _, _, headers in requests)
        assert all(
            headers["User-Agent"] == "Range-UA" and headers["Authorization"] == "test-session-token"
            for _, _, headers in requests
        )
        assert sum(method == "HEAD" for method, _, _ in requests) == 1  # Core Range 探测；名称不额外探测。
    finally:
        manager.close()


@pytest.mark.parametrize(
    "name", ["", "../x", "folder/file", "folder\\file", "CON.zip", "LPT¹.txt", "trailing.", "end ", "x\n", "中" * 86]
)
def test_rejects_invalid_custom_file_names(tmp_path, name):
    with pytest.raises(ValidationError):
        CustomDownloadRequest(
            url="http://host/file", download_directory=tmp_path, naming_mode="custom", custom_name=name
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Test": "a", "x-test": "b"},
        {"Bad Key": "a"},
        {"X-Test": "a\r\nb"},
        {"User-Agent": "x"},
        {"X-Test": "中"},
        {str(index): "a" for index in range(65)},
        {"X-Test": "a" * 32768},
    ],
)
def test_rejects_invalid_request_headers(tmp_path, headers):
    with pytest.raises(ValidationError):
        CustomDownloadRequest(url="http://host/file", target_path=tmp_path / "x", headers=headers)


@pytest.mark.parametrize(
    "disposition,url,expected",
    [
        ('attachment; filename="quoted;name.zip"', "http://host/file", "quoted;name.zip"),
        ("attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87.zip; filename=fallback.zip", "http://host/file", "中文.zip"),
        ("attachment; filename*=UTF-8''%FF; filename=fallback.zip", "http://host/file", "fallback.zip"),
        ('attachment; filename="../../CON.txt"', "http://host/file", "_CON.txt"),
        ('attachment; filename="C:\\unsafe\\good.zip"', "http://host/file", "good.zip"),
        ("", "http://host/encoded%20name.zip?token=abc", "encoded name.zip"),
        ("", "http://host/", "download.bin"),
    ],
)
def test_response_file_name_policy(disposition, url, expected):
    assert DownloadPolicy.response_name(disposition, url) == expected


def test_rejects_mixed_save_modes(tmp_path):
    with pytest.raises(ValidationError):
        CustomDownloadRequest(url="http://host/file", target_path=tmp_path / "file", download_directory=tmp_path)


def test_automatic_target_conflict_retry_freezes_name_and_headers(tmp_path, download_server, monkeypatch):
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    url, payload, requests = download_server
    manager = OperationManager(tmp_path / "state", EventBus())
    service = CustomDownloadService(manager, data_path=tmp_path / "data")
    directory_path = tmp_path / "chosen"
    directory_path.mkdir()
    target_path = directory_path / "中文.zip"
    target_path.write_bytes(b"old")
    try:
        first = service.submit(
            CustomDownloadRequest(
                url=url + "/unicode",
                download_directory=directory_path,
                user_agent="Retry-UA",
                headers={"X-Test": "retry-setting"},
            )
        )
        result = wait_operation(manager, first["operationId"])
        assert result["status"] == "failed" and result["errorCode"] == "DOWNLOAD_TARGET_EXISTS"
        assert target_path.read_bytes() == b"old"
        target_path.unlink()
        before = len(requests)
        retried = service.retry(first["operationId"])
        assert wait_operation(manager, retried["operationId"])["status"] == "completed"
        assert target_path.read_bytes() == payload
        assert all(
            headers["User-Agent"] == "Retry-UA" and headers["X-Test"] == "retry-setting"
            for _, _, headers in requests[before:]
        )
        assert sum(method == "HEAD" for method, _, _ in requests[before:]) == 1
    finally:
        manager.close()


def test_cancelled_queued_download_releases_target(tmp_path):
    started = threading.Event()
    release = threading.Event()
    manager = OperationManager(tmp_path / "state", EventBus(), max_workers=1)

    def blocker(context):
        started.set()
        assert release.wait(timeout=5)

    class LocalDownloader:
        def __init__(self, entries, **kwargs):
            self.target_path = entries[0][1]
            self.client = None
            self.failed_entries = set()

        async def run(self):
            self.target_path.write_bytes(b"result")

        def stop(self):
            return

    service = CustomDownloadService(manager, LocalDownloader)
    request = CustomDownloadRequest(url="http://host/file", target_path=tmp_path / "file")
    try:
        manager.submit("blocker", blocker)
        assert started.wait(timeout=2)
        queued = service.submit(request)
        assert manager.cancel(queued["operationId"])
        release.set()
        assert wait_operation(manager, queued["operationId"])["status"] == "cancelled"
        retried = service.retry(queued["operationId"])
        assert wait_operation(manager, retried["operationId"])["status"] == "completed"
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_atomic_new_target_on_windows_and_posix(tmp_path, download_server, monkeypatch, platform):
    monkeypatch.setattr("ECL.services.custom_downloads.sys.platform", platform)
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    url, payload, _ = download_server
    manager = OperationManager(tmp_path / "state", EventBus())
    service = CustomDownloadService(manager)
    try:
        operation = service.submit(CustomDownloadRequest(url=url + "/file", target_path=tmp_path / "file"))
        assert wait_operation(manager, operation["operationId"])["status"] == "completed"
        assert (tmp_path / "file").read_bytes() == payload
    finally:
        manager.close()
