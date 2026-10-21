# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过 HTTPcore 公开后端固定实际数字地址，保留 TLS 身份并限制每次收发。
#
# 公开接口：
#   - class NetworkCallGuard — 当前请求的授权、截止与真实流归属协议。
#   - class PinnedNetworkBackend — 只拨选定地址的 HTTP/1 网络后端。
# ============================================================

from __future__ import annotations

import socket
import ssl
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from threading import RLock
from typing import Protocol

import httpcore

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.network_targets import NetworkOrigin


class NetworkCallGuard(Protocol):
    """
    将每次有限 I/O 接受和真实流归属连接到同一原始权限租约。
    """

    def accept(self) -> None:
        """
        原子接受一次有限动作，不在授权锁中执行 I/O。
        """
        ...

    def check(self) -> None:
        """
        拒绝已取消、过期或撤销的请求。
        """
        ...

    def timeout(self) -> float:
        """
        返回当前剩余预算与五秒阶段上限中的较小值。
        """
        ...

    def attach(self, stream: httpcore.NetworkStream) -> None:
        """
        先登记真实流，再处理已经发生的取消。
        """
        ...

    def detach(self, stream: httpcore.NetworkStream) -> None:
        """
        仅在确认关闭或所有权转给新 TLS 流后解除旧流。
        """
        ...


@dataclass(slots=True)
class _WireBudget:
    received: int = 0


class _GuardedStream(httpcore.NetworkStream):
    """
    逐次检查租约与收包预算，关闭失败时仍由原请求持有。
    """

    def __init__(
        self,
        stream: httpcore.NetworkStream,
        guard: NetworkCallGuard,
        origin: NetworkOrigin,
        address: IPv4Address | IPv6Address,
        budget: _WireBudget,
    ) -> None:
        self._stream = stream
        self._guard = guard
        self._origin = origin
        self._address = address
        self._budget = budget
        self._lock = RLock()
        self._is_closed = False
        self._is_closing = False

    def verify_peer(self) -> None:
        """
        在 TLS 与 HTTP 发送之前核实已连接端点，异常不泄漏地址。
        """
        peer = self._stream.get_extra_info("server_addr")
        if not isinstance(peer, tuple) or len(peer) < 2:
            raise ProtocolError("网络连接端点无法确认", "PLUGIN_NETWORK_TARGET_DENIED")
        try:
            is_matching = (
                "%" not in str(peer[0]) and ip_address(peer[0]) == self._address and peer[1] == self._origin.port
            )
        except ValueError:
            is_matching = False
        if not is_matching:
            raise ProtocolError("网络连接端点不匹配", "PLUGIN_NETWORK_TARGET_DENIED")

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """
        限制每次实际收包和每跳原始 HTTP 字节，包括头、临时响应与尾部。
        """
        self._guard.accept()
        remaining = 262144 - self._budget.received
        if remaining <= 0:
            raise ProtocolError("网络响应超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
        chunk = self._stream.read(min(max_bytes, 8192, remaining), self._guard.timeout())
        self._budget.received += len(chunk)
        self._guard.check()
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """
        接受一次有界发送；撤销不被描述成撤回已发送字节。
        """
        self._guard.accept()
        self._stream.write(buffer, self._guard.timeout())
        self._guard.check()

    def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.NetworkStream:
        """
        TLS 使用原 DNS 身份，返回后先登记新真实流再移交旧流归属。
        """
        if (
            server_hostname != self._origin.host
            or not ssl_context.check_hostname
            or ssl_context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise ProtocolError("网络 TLS 身份配置无效", "PLUGIN_NETWORK_FAILED")
        self._guard.accept()
        stream = self._stream.start_tls(ssl_context, server_hostname, self._guard.timeout())
        secured = _GuardedStream(stream, self._guard, self._origin, self._address, self._budget)
        try:
            self._guard.attach(secured)
        finally:
            # wrap_socket 已转移底层句柄；旧 close 的成功不能代替新 TLS 流的关闭。
            self._guard.detach(self)
            with self._lock:
                self._is_closed = True
        secured.verify_peer()
        self._guard.check()
        return secured

    def close(self) -> None:
        """
        尝试唤醒阻塞读取并关闭，失败保留请求归属以供监管重试。
        """
        with self._lock:
            if self._is_closed or self._is_closing:
                return
            self._is_closing = True
        try:
            native_socket = self._stream.get_extra_info("socket")
            if isinstance(native_socket, socket.socket):
                # 未连接、已关闭或 TLS 已移交时仍必须执行实际 close。
                with suppress(OSError):
                    native_socket.shutdown(socket.SHUT_RDWR)
            self._stream.close()
        except Exception:
            with self._lock:
                self._is_closing = False
            raise
        with self._lock:
            self._is_closed = True
            self._is_closing = False
        self._guard.detach(self)

    def get_extra_info(self, info: str) -> object:
        """
        仅向库转交公开流信息，不增加 RPC 可访问句柄。
        """
        return self._stream.get_extra_info(info)


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """
    每跳只连接一个已验证数字地址，不做重试或再次 DNS 域名解析。
    """

    def __init__(
        self,
        origin: NetworkOrigin,
        address: IPv4Address | IPv6Address,
        guard: NetworkCallGuard,
        dialer: httpcore.NetworkBackend,
    ) -> None:
        """
        绑定宿主已检查的站点、数字地址和当前调用的关闭归属。

        :param origin: 原逻辑站点，保留给 TLS 与 Host
        :param address: 本跳唯一已准入地址
        :param guard: 当前请求原始租约及资源归属
        :param dialer: 宿主拥有的公开数字地址拨号后端
        """
        self._origin = origin
        self._address = address
        self._guard = guard
        self._dialer = dialer

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.NetworkStream:
        """
        原子接受有限连接，返回即登记与验 peer，未获准前不发送 HTTP。

        :param host: 库提供的原逻辑 DNS 域名
        :param port: 本跳逻辑端口
        :param timeout: 库的阶段预算，实际使用更严格的请求剩余预算
        :param local_address: 本接口不接受源地址覆盖
        :param socket_options: 本接口不接受自定义 socket 选项
        :return: 有真实归属和收发检查的流
        :raises ProtocolError: 站点、peer、租约或期限不满足时抛出
        """
        if (host, port) != (self._origin.host, self._origin.port) or local_address is not None or socket_options:
            raise ProtocolError("网络连接目标无效", "PLUGIN_NETWORK_TARGET_DENIED")
        self._guard.accept()
        raw = self._dialer.connect_tcp(str(self._address), port, timeout=self._guard.timeout())
        stream = _GuardedStream(raw, self._guard, self._origin, self._address, _WireBudget())
        self._guard.attach(stream)
        stream.verify_peer()
        self._guard.check()
        return stream
