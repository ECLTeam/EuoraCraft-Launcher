from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from ECL.events import EventBus
from ECL.services.custom_downloads import CustomDownloadRequest, CustomDownloadService
from ECL.services.operations import OperationManager
from ECL.utils.errors import GameServiceError


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
