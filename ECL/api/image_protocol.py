# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/EuoraCraft-Launcher
#
# 文件作用：ecl-img 自定义协议服务：签发受控图片 URL 并向前端提供原始字节。
#
# 公开接口：
#   - class ImageProtocolService — 维护令牌绑定表，签发协议 URL 并处理协议请求。
# ============================================================

from __future__ import annotations

import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ECL.api.bridge import ImagePolicy
from ECL.utils import get_logger

scheme_name = "ecl-img"
# 令牌只经 URL 传递；长度下限保证 token_urlsafe(24) 的输出形状，上限防异常长路径。
token_pattern_chars = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
token_min_length = 22
token_max_length = 64
# 协议响应不做流式传输；超过远程图片统一上限的文件拒绝读取，避免整段载入内存。
_cors_wildcard = "*"


@dataclass(frozen=True, slots=True)
class ImageHttpResponse:
    """
    协议处理器的纯逻辑响应快照，与 PyTauriX 响应类型解耦以便测试。
    """

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class _ImageTokenBinding:
    """
    一个令牌对应的可读文件与媒体类型；ETag 在请求时按文件状态现算。
    """

    file_path: Path
    mime: str
    expires_at: float


def _is_valid_token(value: str) -> bool:
    # 校验 URL 路径中的令牌形状，避免向绑定表提交异常输入。
    return token_min_length <= len(value) <= token_max_length and set(value) <= token_pattern_chars


def _etag_for(stat_result: Any) -> str:
    # 按文件修改时间与大小生成弱校验 ETag；文件被替换后浏览器缓存自动失效。
    return f'"img-{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def _split_etag_list(header_value: str) -> list[str]:
    # 拆分 If-None-Match 头中的 ETag 列表，兼容 W/ 前缀与多值格式。
    candidates = []
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate:
            candidates.append(candidate)
    return candidates


class ImageProtocolService:
    """
    维护 ``ecl-img`` 协议的令牌绑定表，签发受控图片 URL 并在协议请求中返回原始字节。

    前端只能拿到签发时绑定的文件；协议处理器不接受任何客户端提供的路径，
    读取边界由令牌表的注册入口负责校验。令牌在 TTL 内有效，页面刷新后
    重新调用 ``image_url_resolve`` 换取新 URL 即可。
    """

    scheme = scheme_name
    token_ttl_seconds = 6 * 60 * 60
    max_tokens = 512

    def __init__(self, events: Any | None = None) -> None:
        self.logger = get_logger("ImageProtocol")
        self._bindings: dict[str, _ImageTokenBinding] = {}
        self._lock = RLock()
        if events is not None:
            events.subscribe("launcher:closing", self.close)

    @staticmethod
    def origin() -> str:
        # 返回当前平台的前端可见 Origin；Windows 自定义协议挂在 http 子域下。
        if sys.platform == "win32":
            return f"http://{scheme_name}.localhost"
        return f"{scheme_name}://localhost"

    @staticmethod
    def _mime_for_extension(extension: str) -> str | None:
        return ImagePolicy.mime_by_extension.get(extension.lower())

    def _purge_expired(self, now: float) -> None:
        # 在持锁状态下清理过期令牌；注册路径上调用，避免令牌表无限增长。
        expired = [token for token, binding in self._bindings.items() if binding.expires_at <= now]
        for token in expired:
            self._bindings.pop(token, None)

    def _register(self, file_path: Path, extension: str) -> str:
        mime = self._mime_for_extension(extension)
        if mime is None:
            raise ValueError(f"不支持的图片扩展名: {extension}")
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._purge_expired(now)
            if len(self._bindings) >= self.max_tokens:
                oldest = min(self._bindings.items(), key=lambda item: item[1].expires_at)
                self._bindings.pop(oldest[0], None)
            self._bindings[token] = _ImageTokenBinding(
                file_path=file_path,
                mime=mime,
                expires_at=now + self.token_ttl_seconds,
            )
        return token

    def register_local(self, file_path: Path) -> str:
        """
        为已通过边界校验的本地图片文件签发协议令牌。

        :param file_path: 已确认存在且扩展名受支持的绝对路径
        :return: 含令牌的协议 URL，可直接用于 ``<img src>``
        :raises ValueError: 扩展名不受支持时抛出
        """
        token = self._register(file_path, file_path.suffix.lower())
        filename = quote(file_path.name)
        return f"{self.origin()}/{token}/{filename}"

    def register_remote_cache(self, url_digest: str, extension: str, cache_dir: Path) -> str:
        """
        为已落盘的远程图片缓存文件签发协议令牌。

        :param url_digest: 远程 URL 的稳定摘要（缓存文件名主体）
        :param extension: 带点扩展名，决定媒体类型
        :param cache_dir: 远程图片缓存目录
        :return: 含令牌的协议 URL
        :raises ValueError: 扩展名不受支持时抛出
        """
        cache_file = cache_dir / f"{url_digest}{extension}"
        token = self._register(cache_file, extension)
        return f"{self.origin()}/{token}/{url_digest}{extension}"

    def _serve_locked(self, token: str, method: str, if_none_match: str | None) -> ImageHttpResponse:
        # 校验令牌并读取绑定文件；令牌表锁内只做查找，文件读取移出锁外执行。
        binding = self._bindings.get(token)
        if binding is None or binding.expires_at <= time.monotonic():
            return ImageHttpResponse(
                status=403,
                headers=(("content-type", "text/plain"),),
                body=b"invalid or expired image token",
            )
        if method not in {"GET", "HEAD"}:
            return ImageHttpResponse(status=405, headers=(("allow", "GET, HEAD"),), body=b"")
        common_headers: list[tuple[str, str]] = [
            ("content-type", binding.mime),
            ("cache-control", "no-cache"),
            ("access-control-allow-origin", _cors_wildcard),
        ]
        try:
            stat_result = binding.file_path.stat()
        except OSError:
            self.logger.warning("图片令牌指向的文件不可读取: %s", binding.file_path)
            return ImageHttpResponse(status=404, headers=(("content-type", "text/plain"),), body=b"image missing")
        if not binding.file_path.is_file() or binding.file_path.is_symlink():
            return ImageHttpResponse(status=404, headers=(("content-type", "text/plain"),), body=b"image missing")
        etag = _etag_for(stat_result)
        common_headers.append(("etag", etag))
        if if_none_match is not None and etag in _split_etag_list(if_none_match):
            return ImageHttpResponse(status=304, headers=tuple(common_headers), body=b"")
        if stat_result.st_size > ImagePolicy.max_remote_image_bytes:
            return ImageHttpResponse(status=413, headers=(("content-type", "text/plain"),), body=b"image too large")
        if method == "HEAD":
            common_headers.append(("content-length", str(stat_result.st_size)))
            return ImageHttpResponse(status=200, headers=tuple(common_headers), body=b"")
        try:
            body = binding.file_path.read_bytes()
        except OSError:
            self.logger.warning("图片文件读取失败: %s", binding.file_path)
            return ImageHttpResponse(status=500, headers=(("content-type", "text/plain"),), body=b"image read failed")
        return ImageHttpResponse(status=200, headers=tuple(common_headers), body=body)

    def serve(self, uri: str, method: str, if_none_match: str | None) -> ImageHttpResponse:
        """
        处理一次协议请求；纯逻辑入口，供协议适配器与测试共用。

        :param uri: 完整请求 URI，路径形如 ``/<token>/<filename>``
        :param method: 大写 HTTP 方法
        :param if_none_match: 请求的 If-None-Match 头；缺失时为 None
        :return: 状态码、头字段与正文的响应快照
        """
        path = unquote(urlsplit(uri).path) if uri else ""
        segments = [segment for segment in path.split("/") if segment]
        if not segments or not _is_valid_token(segments[0]):
            return ImageHttpResponse(
                status=403,
                headers=(("content-type", "text/plain"),),
                body=b"invalid or expired image token",
            )
        with self._lock:
            binding = self._bindings.get(segments[0])
            expired = binding is None or binding.expires_at <= time.monotonic()
            if expired:
                self._bindings.pop(segments[0], None)
        if expired:
            return ImageHttpResponse(
                status=403,
                headers=(("content-type", "text/plain"),),
                body=b"invalid or expired image token",
            )
        return self._serve_locked(segments[0], method.upper(), if_none_match)

    def protocol(self) -> Any:
        """
        构建交给 ``Builder.build(protocols=...)`` 的一次性协议描述符。

        处理器同步运行在 PyTauriX 工作线程；纯逻辑 ``serve`` 之外只做
        PyTauriX 响应类型转换与请求头提取。绑定表查找已做线程保护。

        :return: PyTauriX 协议描述符
        """
        from pytaurix import ProtocolResponse, create_protocol

        def handle_request(request: Any) -> ProtocolResponse:
            headers_lower = {name.lower(): value for name, value in request.headers}
            response = self.serve(request.uri, request.method, headers_lower.get("if-none-match"))
            return ProtocolResponse(status=response.status, headers=response.headers, body=response.body)

        return create_protocol(self.scheme, handle_request)

    def close(self, _payload: Any = None) -> None:
        # 启动器关闭时清空绑定表；正在进行的读取持有文件路径引用，不受清空影响。
        with self._lock:
            self._bindings.clear()


__all__ = ["ImageHttpResponse", "ImageProtocolService"]
