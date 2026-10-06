# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：本地背景视频流服务，向 WebView 提供受令牌保护且支持 Range 的只读媒体流。
#
# 公开接口：
#   - class BackgroundMediaService — 管理背景视频的临时 URL 与本地回环 HTTP 服务。
# ============================================================

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import ClassVar
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class _MediaEntry:
    """
    保存一个已授权背景视频的不可变文件快照。

    令牌仅在当前启动器进程有效，并且每次选择新背景视频时会整体失效，避免 WebView
    通过通用本地文件协议访问未被用户选中的路径。

    :param path: 已规范化且确认存在的视频绝对路径
    :param mime_type: 响应 ``Content-Type``
    :param size: 签发时的视频字节数
    """

    path: Path
    mime_type: str
    size: int


class _BackgroundMediaRequestHandler(BaseHTTPRequestHandler):
    """
    把 HTTP 请求转发给当前服务器绑定的背景媒体服务。

    请求处理器不保存文件路径或令牌状态，全部授权判断委托给服务实例，避免线程间状态分叉。
    """

    server: ThreadingHTTPServer

    def do_GET(self) -> None:
        self._serve(include_body=True)

    def do_HEAD(self) -> None:
        self._serve(include_body=False)

    def log_message(self, _format: str, *_args: object) -> None:
        """
        禁止标准库将含令牌的请求路径输出到控制台日志。

        临时 URL 的令牌属于本次运行的访问凭据，不应出现在终端或持久化日志中。
        """

    def _serve(self, *, include_body: bool) -> None:
        service = getattr(self.server, "background_media_service", None)
        if not isinstance(service, BackgroundMediaService):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        entry = service._entry_for_request(self.path)
        if entry is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            actual_size = entry.path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if actual_size <= 0 or actual_size != entry.size:
            self.send_error(HTTPStatus.GONE)
            return

        byte_range = service._parse_range(self.headers.get("Range"), actual_size)
        if byte_range is None and self.headers.get("Range"):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{actual_size}")
            self.end_headers()
            return

        start, end = byte_range if byte_range is not None else (0, actual_size - 1)
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range is not None else HTTPStatus.OK)
        self.send_header("Content-Type", entry.mime_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if byte_range is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{actual_size}")
        self.end_headers()
        if not include_body:
            return

        try:
            with entry.path.open("rb") as media_file:
                media_file.seek(start)
                remaining = length
                while remaining:
                    chunk = media_file.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # WebView 主动停止预加载或切换视频是正常行为。
            return
        except OSError:
            return


class BackgroundMediaService:
    """
    为选中的本地背景视频签发短生命周期回环 URL。

    服务只绑定 ``127.0.0.1``，路径中使用随机令牌，且令牌始终映射到单个已校验的
    视频文件。它不暴露目录浏览、任意路径参数或上传能力；视频通过 Range 读取，避免
    将完整媒体编码为 Data URL 后复制进前端内存。
    """

    video_mime_by_suffix: ClassVar[Mapping[str, str]] = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }
    max_video_bytes: ClassVar[int] = 1024 * 1024 * 1024

    def __init__(self) -> None:
        self._entries: dict[str, _MediaEntry] = {}
        self._lock = RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def open(self, raw_path: str) -> str:
        """
        校验视频并返回仅供当前背景使用的受保护 URL。

        :param raw_path: 用户已保存到背景配置中的本地视频绝对路径
        :return: 带随机令牌、可供 ``<video>`` 使用的本地回环 URL
        :raises ValueError: 路径不是支持的视频文件，或大小不在允许范围时抛出
        """

        path = Path(raw_path).expanduser().resolve(strict=True)
        suffix = path.suffix.lower()
        mime_type = self.video_mime_by_suffix.get(suffix)
        if mime_type is None or not path.is_file():
            raise ValueError("仅支持 MP4 或 WebM 视频文件")
        size = path.stat().st_size
        if size <= 0 or size > self.max_video_bytes:
            raise ValueError("背景视频必须大于 0 且不超过 1 GiB")

        with self._lock:
            self._ensure_server()
            self._entries.clear()
            token = secrets.token_urlsafe(32)
            self._entries[token] = _MediaEntry(path=path, mime_type=mime_type, size=size)
            if self._server is None:
                raise RuntimeError("背景视频服务未启动")
            port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/background/{token}"

    def revoke(self) -> None:
        """
        撤销当前全部背景视频 URL，不影响已经保存的用户配置。

        下次前端请求时会为配置中的当前文件签发新的令牌。
        """

        with self._lock:
            self._entries.clear()

    def close(self) -> None:
        """
        停止回环服务并释放后台线程；允许重复调用。

        调用后所有已签发 URL 立即失效，应用退出不会遗留监听端口或服务线程。
        """

        with self._lock:
            server = self._server
            thread = self._thread
            self._entries.clear()
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _ensure_server(self) -> None:
        if self._server is not None:
            return
        server = ThreadingHTTPServer(("127.0.0.1", 0), _BackgroundMediaRequestHandler)
        server.daemon_threads = True
        server.background_media_service = self  # type: ignore[attr-defined]
        thread = Thread(target=server.serve_forever, name="background-media", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread

    def _entry_for_request(self, raw_target: str) -> _MediaEntry | None:
        parsed = urlsplit(raw_target)
        if parsed.query or parsed.fragment:
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[1] != "background" or not parts[2]:
            return None
        with self._lock:
            return self._entries.get(parts[2])

    @staticmethod
    def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
        if value is None:
            return None
        if not value.startswith("bytes=") or "," in value:
            return None
        start_text, separator, end_text = value[6:].strip().partition("-")
        if not separator:
            return None
        try:
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            elif end_text:
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    return None
                start = max(0, size - suffix_length)
                end = size - 1
            else:
                return None
        except ValueError:
            return None
        if start < 0 or start >= size or end < start:
            return None
        return start, min(end, size - 1)


__all__ = ["BackgroundMediaService"]
