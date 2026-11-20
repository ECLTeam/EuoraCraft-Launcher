# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/EuoraCraft-Launcher
#
# 文件作用：ecl-media 自定义协议服务：签发受控背景视频 URL 并按 Range 提供字节区间。
#
# 公开接口：
#   - class MediaProtocolService — 维护令牌绑定表，签发协议 URL 并处理媒体协议请求。
# ============================================================

from __future__ import annotations

import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

from ECL.utils import get_logger

if TYPE_CHECKING:
    from pytaurix import RangeRequest

scheme_name = "ecl-media"
# 令牌只经 URL 传递；长度下限保证 token_urlsafe(24) 的输出形状，上限防异常长路径。
token_pattern_chars = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
token_min_length = 22
token_max_length = 64
_cors_wildcard = "*"
# 前端断言与媒体协商所需的非安全清单响应头，经 expose-headers 放行。
_expose_headers = "etag, accept-ranges, content-range"


@dataclass(frozen=True, slots=True)
class MediaHttpResponse:
    """
    协议处理器的纯逻辑响应快照，与 PyTauriX 响应类型解耦以便测试。
    """

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class _MediaTokenBinding:
    """
    一个令牌对应的可读视频与媒体类型；签发时记录大小，响应时按文件实际状态校验。
    """

    file_path: Path
    mime: str
    size: int
    expires_at: float


def _is_valid_token(value: str) -> bool:
    # 校验 URL 路径中的令牌形状，避免向绑定表提交异常输入。
    return token_min_length <= len(value) <= token_max_length and set(value) <= token_pattern_chars


class MediaProtocolService:
    """
    维护 ``ecl-media`` 协议的令牌绑定表，签发受控视频 URL 并按 Range 返回字节区间。

    与 ``ecl-img`` 相同的安全模型：协议处理器不接受客户端路径，读取边界由注册入口
    校验扩展名与大小。令牌按路径复用，同一文件重复签发返回已有地址；令牌总数受
    ``max_tokens`` 限制，超出时淘汰最早过期的绑定。区间交付委托 PyTauriX 的
    ``file_range_response``，WebView 按 Range 逐块请求，内存无压力。
    """

    scheme = scheme_name
    token_ttl_seconds = 6 * 60 * 60
    max_tokens = 32
    video_mime_by_suffix: dict[str, str] = {".mp4": "video/mp4", ".webm": "video/webm"}
    max_video_bytes = 1024 * 1024 * 1024

    def __init__(self, events: Any | None = None) -> None:
        self.logger = get_logger("MediaProtocol")
        self._bindings: dict[str, _MediaTokenBinding] = {}
        self._lock = RLock()
        if events is not None:
            events.subscribe("launcher:closing", self.close)

    @staticmethod
    def origin() -> str:
        # 返回当前平台的前端可见 Origin；Windows 自定义协议挂在 http 子域下。
        if sys.platform == "win32":
            return f"http://{scheme_name}.localhost"
        return f"{scheme_name}://localhost"

    def _purge_expired(self, now: float) -> None:
        # 在持锁状态下清理过期令牌；注册路径上调用，避免令牌表无限增长。
        expired = [token for token, binding in self._bindings.items() if binding.expires_at <= now]
        for token in expired:
            self._bindings.pop(token, None)

    def register_local(self, raw_path: str) -> str:
        """
        校验视频文件并签发协议 URL；同一路径复用未过期令牌。

        背景切换、画廊预览等场景可能同时持有多个视频地址，因此不再整体失效旧令牌。
        令牌总数达到上限时淘汰最早过期的绑定，仍在使用的地址可能因此失效，调用方
        需重新签发。

        :param raw_path: 用户已保存到背景配置或文件集中的本地视频绝对路径
        :return: 含令牌的协议 URL，可直接用于 ``<video src>``
        :raises ValueError: 扩展名不受支持、文件缺失或大小越界时抛出
        """
        path = Path(raw_path).expanduser().resolve(strict=True)
        suffix = path.suffix.lower()
        mime = self.video_mime_by_suffix.get(suffix)
        if mime is None or not path.is_file() or path.is_symlink():
            raise ValueError("仅支持 MP4 或 WebM 视频文件")
        size = path.stat().st_size
        if size <= 0 or size > self.max_video_bytes:
            raise ValueError("背景视频必须大于 0 且不超过 1 GiB")

        now = time.monotonic()
        with self._lock:
            self._purge_expired(now)
            for token, binding in self._bindings.items():
                if binding.file_path == path:
                    return f"{self.origin()}/{token}/{quote(path.name)}"
            if len(self._bindings) >= self.max_tokens:
                oldest = min(self._bindings, key=lambda key: self._bindings[key].expires_at)
                self._bindings.pop(oldest, None)
            token = secrets.token_urlsafe(24)
            self._bindings[token] = _MediaTokenBinding(
                file_path=path,
                mime=mime,
                size=size,
                expires_at=now + self.token_ttl_seconds,
            )
        return f"{self.origin()}/{token}/{quote(path.name)}"

    def revoke(self) -> None:
        """
        清空当前全部视频令牌；不影响已保存的用户配置。
        """
        with self._lock:
            self._bindings.clear()

    def _preflight_response(self, requested_headers: str | None) -> MediaHttpResponse:
        # If-Range 等非安全清单请求头触发 OPTIONS 预检；放行协商头避免 fetch 网络级失败。
        allow = requested_headers or "range, if-range"
        return MediaHttpResponse(
            status=204,
            headers=(
                ("access-control-allow-origin", _cors_wildcard),
                ("access-control-allow-headers", allow),
                ("access-control-allow-methods", "GET, HEAD, OPTIONS"),
                ("access-control-max-age", "600"),
            ),
            body=b"",
        )

    def _media_response(
        self,
        binding: _MediaTokenBinding,
        range_request: RangeRequest | None,
        if_range: str | None,
    ) -> MediaHttpResponse:
        # 委托 PyTauriX 助手构建 200/206 响应，再补上跨源与缓存控制头。
        from pytaurix import file_range_response

        media = file_range_response(binding.file_path, binding.mime, range=range_request, if_range=if_range)
        headers = [
            ("content-type", binding.mime),
            ("accept-ranges", "bytes"),
            ("cache-control", "no-store"),
            ("access-control-allow-origin", _cors_wildcard),
            ("access-control-expose-headers", _expose_headers),
        ]
        if media.status == 206:
            content_range = next(value for name, value in media.headers if name == "content-range")
            headers.append(("content-range", content_range))
        headers.append(("etag", next(value for name, value in media.headers if name == "etag")))
        return MediaHttpResponse(status=media.status, headers=tuple(headers), body=media.body)

    def _unsatisfiable_response(self, size: int) -> MediaHttpResponse:
        # 区间不可满足（起点越界等）按 HTTP 语义回 416 并带当前长度。
        return MediaHttpResponse(
            status=416,
            headers=(
                ("content-range", f"bytes */{size}"),
                ("access-control-allow-origin", _cors_wildcard),
            ),
            body=b"",
        )

    def _serve_locked(self, token: str, method: str, headers: dict[str, str]) -> MediaHttpResponse:
        # 校验令牌与文件状态；区间解析与读取都在绑定表锁之外完成。
        binding = self._bindings.get(token)
        if binding is None or binding.expires_at <= time.monotonic():
            return MediaHttpResponse(
                status=403,
                headers=(("content-type", "text/plain"),),
                body=b"invalid or expired media token",
            )
        if method == "OPTIONS":
            return self._preflight_response(headers.get("access-control-request-headers"))
        if method not in {"GET", "HEAD"}:
            return MediaHttpResponse(status=405, headers=(("allow", "GET, HEAD, OPTIONS"),), body=b"")
        try:
            stat_result = binding.file_path.stat()
        except OSError:
            self.logger.warning("视频令牌指向的文件不可读取: %s", binding.file_path)
            return MediaHttpResponse(status=404, headers=(("content-type", "text/plain"),), body=b"video missing")
        actual_size = stat_result.st_size
        if not binding.file_path.is_file() or binding.file_path.is_symlink():
            return MediaHttpResponse(status=404, headers=(("content-type", "text/plain"),), body=b"video missing")
        if actual_size <= 0 or actual_size != binding.size:
            return MediaHttpResponse(
                status=410,
                headers=(("content-type", "text/plain"),),
                body=b"video size changed",
            )
        if method == "HEAD":
            return MediaHttpResponse(
                status=200,
                headers=(
                    ("content-type", binding.mime),
                    ("content-length", str(actual_size)),
                    ("accept-ranges", "bytes"),
                    ("cache-control", "no-store"),
                    ("access-control-allow-origin", _cors_wildcard),
                ),
                body=b"",
            )
        from pytaurix import parse_range

        raw_range = headers.get("range")
        range_request = parse_range(raw_range) if raw_range else None
        if range_request is not None and range_request.resolve(actual_size) is None:
            return self._unsatisfiable_response(actual_size)
        return self._media_response(binding, range_request, headers.get("if-range"))

    def serve(self, uri: str, method: str, headers: dict[str, str] | None = None) -> MediaHttpResponse:
        """
        处理一次协议请求；纯逻辑入口，供协议适配器与测试共用。

        :param uri: 完整请求 URI，路径形如 ``/<token>/<filename>``
        :param method: 大写 HTTP 方法
        :param headers: 请求头小写键字典；缺失时视为空
        :return: 状态码、头字段与正文的响应快照
        """
        path = unquote(urlsplit(uri).path) if uri else ""
        segments = [segment for segment in path.split("/") if segment]
        if not segments or not _is_valid_token(segments[0]):
            return MediaHttpResponse(
                status=403,
                headers=(("content-type", "text/plain"),),
                body=b"invalid or expired media token",
            )
        with self._lock:
            binding = self._bindings.get(segments[0])
            expired = binding is None or binding.expires_at <= time.monotonic()
            if expired:
                self._bindings.pop(segments[0], None)
        if expired:
            return MediaHttpResponse(
                status=403,
                headers=(("content-type", "text/plain"),),
                body=b"invalid or expired media token",
            )
        return self._serve_locked(segments[0], method.upper(), headers or {})

    def protocol(self) -> Any:
        """
        构建交给 ``Builder.build(protocols=...)`` 的一次性协议描述符。

        处理器同步运行在 PyTauriX 工作线程；文件区间读取不占用 GUI 线程。
        请求头小写化后传入纯逻辑 ``serve``，Range 协商在 ``file_range_response``
        内完成。

        :return: PyTauriX 协议描述符
        """
        from pytaurix import ProtocolResponse, create_protocol

        def handle_request(request: Any) -> ProtocolResponse:
            headers_lower = {name.lower(): value for name, value in request.headers}
            response = self.serve(request.uri, request.method, headers_lower)
            return ProtocolResponse(status=response.status, headers=response.headers, body=response.body)

        return create_protocol(self.scheme, handle_request)

    def close(self, _payload: Any = None) -> None:
        # 启动器关闭时清空绑定表；正在进行的读取持有文件路径引用，不受清空影响。
        with self._lock:
            self._bindings.clear()


__all__ = ["MediaHttpResponse", "MediaProtocolService"]
