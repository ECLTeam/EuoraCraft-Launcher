# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：执行插件 GET/HEAD 查询，维护原始授权、重定向、有限 DNS 与真实清理归属。
#
# 公开接口：
#   - class PluginNetworkService — 应用唯一网络请求名额与 DNS 工作者所有者。
#   - class NetworkCall — 保存查询或任务的原始准入与真实网络资源。
#   - class RuntimeNetwork — 当前 Worker run 的只读网络门面。
# ============================================================

from __future__ import annotations

import base64
import logging
import socket
import ssl
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
from contextvars import copy_context
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from typing import ClassVar
from urllib.parse import urljoin

import httpcore
import httpx

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.network_targets import NetworkOrigin, NetworkTargetPolicy
from ECL.plugins.runtime.models import NetworkRequest
from ECL.plugins.runtime.network_transport import PinnedNetworkBackend
from ECL.utils import get_logger
from ECL.utils.errors import GameServiceError
from ECL.utils.operation_logging import operation_scope, private_transport_scope


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    """
    一次系统解析只取得 TCP 数字地址，随后不再以域名拨号。
    """
    return tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


class NetworkCall:
    """
    保留原租约和真实 DNS/流，等待方返回不等于底层工作已经结束。
    """

    def __init__(
        self,
        owner: RuntimeNetwork,
        lease: GrantLease,
        deadline: float,
        *,
        additional_leases: tuple[GrantLease, ...] = (),
        cancel_event: Event | None = None,
        wire_limit: int = 262144,
    ) -> None:
        """
        绑定宿主原始准入、取消信号与查询或下载预算，不读取插件自报权限。

        :param owner: 当前运行网络门面
        :param lease: 原网络准入
        :param deadline: 所有跳转共享的总截止
        :param additional_leases: 同时必须有效的原始写准入
        :param cancel_event: 任务服务拥有的真实取消信号
        :param wire_limit: 宿主确定的单跳原始收包上限
        """
        self.owner: RuntimeNetwork = owner
        self.lease: GrantLease = lease
        self.leases: tuple[GrantLease, ...] = (lease, *additional_leases)
        self.cancel_event: Event | None = cancel_event
        self.wire_limit: int = wire_limit
        self.deadline: float = deadline
        self._lock = RLock()
        self._cancelled = Event()
        self._streams: set[httpcore.NetworkStream] = set()
        self._dns_future: Future[tuple[str, ...]] | None = None
        self._is_active = True

    def check(self) -> None:
        """
        逐次检查全部原始准入、真实任务取消和共同截止，不延续失效调用。
        """
        self.owner.session.check_all(self.leases)
        self._check_task_cancelled()
        if self._cancelled.is_set() or self.owner.is_closed:
            raise ProtocolError("插件网络请求已停止", "PLUGIN_SESSION_EXPIRED")
        if monotonic() >= self.deadline:
            raise ProtocolError("插件网络请求超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")

    def accept(self) -> None:
        """
        授权锁内只登记接受，实际有限 I/O 在其后执行，撤销先发生则不接受。
        """
        self.accept_action(lambda: None)

    def accept_action(self, register: Callable[[], None]) -> None:
        """
        将全部原权限、取消和截止核验与有限资源登记放在同一接受点。

        :param register: 仅访问受信内存的登记动作
        :raises ProtocolError: 截止或网络运行状态失效时拒绝
        """

        def accepted() -> None:
            self._accept_locked()
            register()

        self.owner.session.accept_all(self.leases, accepted)

    def _check_task_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise GameServiceError("操作已取消", "OPERATION_CANCELLED")

    def _accept_locked(self) -> None:
        self._check_task_cancelled()
        if self._cancelled.is_set() or self.owner.is_closed:
            raise ProtocolError("插件网络请求已停止", "PLUGIN_SESSION_EXPIRED")
        if monotonic() >= self.deadline:
            raise ProtocolError("插件网络请求超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")

    def timeout(self) -> float:
        """
        返回单次有限 I/O 与总剩余截止的较短预算。

        :return: 至多五秒的剩余预算
        :raises ProtocolError: 总截止已经到达时抛出
        """
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ProtocolError("插件网络请求超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")
        return min(5.0, remaining)

    def attach(self, stream: httpcore.NetworkStream) -> None:
        """
        即使撤销早于连接或 TLS 返回也先登记句柄，随后关闭，不遗失新流。
        """
        with self._lock:
            self._streams.add(stream)
        try:
            self.check()
        except (ProtocolError, PluginGrantError, GameServiceError):
            self.cancel()
            raise

    def detach(self, stream: httpcore.NetworkStream) -> None:
        """
        确认关闭或移交后移除旧流，再检查是否能够归还网络名额。

        :param stream: 已确认不再拥有实际连接的旧流
        """
        with self._lock:
            self._streams.discard(stream)
        self.owner.service.reap(self)

    def set_dns(self, future: Future[tuple[str, ...]]) -> None:
        """
        保存唯一实际解析 future，等待方返回也不能释放其名额。

        :param future: 宿主已接纳的实际解析
        """
        with self._lock:
            self._dns_future = future

    def close_streams(self) -> None:
        """
        全局与请求状态锁之外关闭真实流；失败保留到下次监管。
        """
        with self._lock:
            streams = tuple(self._streams)
        for stream in streams:
            try:
                stream.close()
            except Exception:
                get_logger("PluginNetwork").warning("插件网络读取资源关闭未完成，等待重试")

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            future = self._dns_future
        if future is not None:
            future.cancel()
        self.close_streams()

    def finish(self) -> None:
        self.close_streams()
        with self._lock:
            self._is_active = False
        self.owner.service.reap(self)

    def is_idle(self) -> bool:
        with self._lock:
            return not self._is_active and not self._streams and (self._dns_future is None or self._dns_future.done())

    def retry_cleanup(self) -> None:
        """
        只为已经返回的调用重试清理，不干扰正在正常读取的流。
        """
        with self._lock:
            should_close = not self._is_active
        if should_close:
            self.close_streams()


class PluginNetworkService:
    """
    维护应用所有网络请求与至多四个实际 DNS 工作者，不积累待办队列。

    解析超时后的 future 仍占用请求和 DNS 名额，实际返回后只清理，不继续连接。
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str, int], tuple[str, ...]] = _resolve_addresses,
        dialer: httpcore.NetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """
        创建宿主拥有的网络边界，测试依赖只可由受信 Python 调用方注入。

        :param resolver: 系统解析边界，生产始终为单次 getaddrinfo
        :param dialer: 公开数字拨号后端，生产为 SyncBackend
        :param ssl_context: 受信 TLS 夹具；生产使用不继承环境的验证上下文
        """
        self._resolver = resolver
        self._dialer = dialer if dialer is not None else httpcore.SyncBackend()
        self._ssl_context = ssl_context
        self._lock = RLock()
        self._calls: set[NetworkCall] = set()
        self._is_closed = False
        self._dns_count = 0

    def begin(
        self,
        owner: RuntimeNetwork,
        lease: GrantLease,
        deadline: float,
        *,
        additional_leases: tuple[GrantLease, ...] = (),
        cancel_event: Event | None = None,
        wire_limit: int = 262144,
    ) -> NetworkCall:
        """
        在原始授权接受点保留 run 和宿主名额，不排队重放请求。

        :param owner: 当前 run 门面
        :param lease: 本调用原始租约
        :param deadline: 包含所有跳转的截止时间
        :param additional_leases: 下载同时需要的原始写准入
        :param cancel_event: 任务取消信号，查询不提供
        :param wire_limit: 宿主指定的单跳原始预算
        :return: 持续跟踪至真实结束的调用
        :raises ProtocolError: 已关闭或实际请求名额耗尽时抛出
        """
        call = NetworkCall(
            owner,
            lease,
            deadline,
            additional_leases=additional_leases,
            cancel_event=cancel_event,
            wire_limit=wire_limit,
        )

        def register() -> None:
            with self._lock:
                if self._is_closed or owner.is_closed:
                    raise ProtocolError("插件网络服务已经关闭", "PLUGIN_SESSION_EXPIRED")
                if len(self._calls) >= 8 or sum(item.owner is owner for item in self._calls) >= 2:
                    raise ProtocolError("插件网络请求名额已满", "PLUGIN_RATE_LIMITED")
                self._calls.add(call)

        call.accept_action(register)
        return call

    def resolve(self, call: NetworkCall, origin: NetworkOrigin) -> IPv4Address | IPv6Address:
        """
        有界等待实际解析，任何禁止地址使整跳拒绝，不回退到其他解析或连接。

        :param call: 原始请求与截止
        :param origin: 当前已授权精确站点
        :return: 本跳唯一选中的普通公网地址
        :raises ProtocolError: 解析失败、名额不足、超时或含禁止地址时抛出
        """
        future = self._submit_dns(call, origin)
        dns_deadline = min(call.deadline, monotonic() + 5)
        try:
            while True:
                call.check()
                remaining = dns_deadline - monotonic()
                if remaining <= 0:
                    raise ProtocolError("插件网络解析超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")
                try:
                    results = future.result(timeout=min(0.05, remaining))
                    break
                except TimeoutError:
                    continue
            call.check()
            if not results or len(results) > 16:
                raise ProtocolError("插件网络解析结果无效", "PLUGIN_NETWORK_RESOLUTION_FAILED")
            addresses = tuple(dict.fromkeys(ip_address(value) for value in results if "%" not in value))
            if len(addresses) != len(set(results)) or any(not NetworkTargetPolicy.allows(item) for item in addresses):
                raise ProtocolError("插件网络目标不允许访问", "PLUGIN_NETWORK_TARGET_DENIED")
            return addresses[0]
        except (ProtocolError, PluginGrantError, GameServiceError):
            raise
        except (socket.gaierror, OSError, ValueError):
            raise ProtocolError("插件网络域名解析失败", "PLUGIN_NETWORK_RESOLUTION_FAILED") from None
        finally:
            if not future.done():
                call.cancel()

    def _submit_dns(self, call: NetworkCall, origin: NetworkOrigin) -> Future[tuple[str, ...]]:
        """
        在原始租约接受点占用实际 DNS 名额，回调仅归还资源，不延续业务。
        """
        context = copy_context()
        future: Future[tuple[str, ...]] | None = None

        def submit() -> None:
            nonlocal future
            with self._lock:
                if self._is_closed or call.owner.is_closed:
                    raise ProtocolError("插件网络服务已经关闭", "PLUGIN_SESSION_EXPIRED")
                if self._dns_count >= 4:
                    raise ProtocolError("插件网络解析名额已满", "PLUGIN_RATE_LIMITED")
                future = Future()
                self._dns_count += 1
                call.set_dns(future)

        call.accept_action(submit)
        assert future is not None

        def completed(_: Future[tuple[str, ...]]) -> None:
            with self._lock:
                self._dns_count -= 1
            self.reap(call)

        future.add_done_callback(completed)

        def resolve() -> None:
            # 标准线程池会在解释器退出时无限 join；这里仅保留四个有归属的守护解析工作者。
            # 正在运行的 getaddrinfo 不能取消，future 仍占用名额直到真实返回。
            if not future.set_running_or_notify_cancel():
                return
            try:
                results = context.run(self._resolver, origin.host, origin.port)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(results)

        try:
            Thread(target=resolve, name="plugin_network_dns", daemon=True).start()
        except RuntimeError as exc:
            if not future.done():
                future.set_exception(exc)
        return future

    def pool(
        self, origin: NetworkOrigin, address: IPv4Address | IPv6Address, call: NetworkCall
    ) -> httpcore.ConnectionPool:
        """
        创建单跳独立 HTTP/1 连接，不继承代理、Cookie、账户或证书环境。

        :param origin: 已准入逻辑站点
        :param address: 本跳已验证数字地址
        :param call: 原始调用的授权与资源归属
        :return: 无重试和连接复用的独立池
        """
        context = self._ssl_context if self._ssl_context is not None else httpx.create_ssl_context(trust_env=False)
        return httpcore.ConnectionPool(
            ssl_context=context,
            proxy=None,
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            http1=True,
            http2=False,
            network_backend=PinnedNetworkBackend(origin, address, call, self._dialer, call.wire_limit),
        )

    def reap(self, call: NetworkCall) -> None:
        """
        实际调用、DNS 和全部流均已结束后才释放名额。

        :param call: 需要确认真实结束的调用
        """
        if call.is_idle():
            with self._lock:
                self._calls.discard(call)

    def calls(self, owner: RuntimeNetwork) -> tuple[NetworkCall, ...]:
        """
        取得当前 run 的资源快照，调用方在锁外检查或关闭。

        :param owner: 当前 run 门面
        :return: 只属于该 run 的实际调用
        """
        with self._lock:
            return tuple(item for item in self._calls if item.owner is owner)

    def close(self) -> bool:
        """
        拒绝新接纳并尝试收尾，不无限等待无法强杀的系统解析线程。

        :return: 全部实际请求和解析已经结束时为 True，否则保留 pending
        """
        with self._lock:
            self._is_closed = True
            calls = tuple(self._calls)
        for call in calls:
            call.cancel()
            self.reap(call)
        with self._lock:
            return not self._calls and self._dns_count == 0


class RuntimeNetwork:
    """
    当前 run 的同步只读网络查询，不创建后台业务任务或公开请求句柄。
    """

    returned_headers: ClassVar[frozenset[str]] = frozenset(
        {"content-type", "content-length", "etag", "last-modified", "cache-control"}
    )
    redirects: ClassVar[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

    def __init__(self, session: PluginGrantSession, service: PluginNetworkService) -> None:
        """
        绑定唯一授权会话与应用服务，不允许插件选择网络后端。

        :param session: 当前 run 的授权会话
        :param service: 应用唯一请求和 DNS 归属
        """
        self.session: PluginGrantSession = session
        self.service: PluginNetworkService = service
        self._closed = Event()

    @property
    def is_closed(self) -> bool:
        """
        查询是否已拒绝新网络调用。

        :return: 本 run 网络门面是否关闭
        """
        return self._closed.is_set()

    def handle(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        只接受严格 GET/HEAD，全部跳转与交付使用同一原始租约和总预算。

        :param params: 已经过 JSON 边界的请求
        :param deadline: RPC 原始截止时间
        :return: 完整有界实体和白名单响应头
        :raises ProtocolError: 网络、输入、范围、预算或生命周期不满足时抛出
        """
        deadline = min(deadline, monotonic() + 30)
        request = NetworkRequest.model_validate(params)
        view = self.session.view()
        permission = next((item for item in view.items if item.capability == "network.read"), None)
        lease = self.session.authorize("network.read", permission.resource_refs if permission is not None else ())
        call = self.service.begin(self, lease, deadline)
        logger = get_logger("PluginNetwork")
        with private_transport_scope(), operation_scope("插件网络读取", timed=True) as trace:
            trace.log(logger, f"插件网络读取开始；方法：{request.method}", logging.DEBUG, include_duration=False)
            try:
                try:
                    result = self._request(call, request)
                finally:
                    call.finish()
                call.check()
                trace.log(logger, "插件网络读取成功", logging.DEBUG)
                return result
            except (ProtocolError, PluginGrantError) as exc:
                trace.log(logger, f"插件网络读取未完成；原因：{exc.error_code}", logging.DEBUG)
                raise
            except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException, OSError):
                call.check()
                trace.log(logger, "插件网络读取失败；原因：PLUGIN_NETWORK_FAILED", logging.DEBUG)
                raise ProtocolError("插件网络连接或响应失败", "PLUGIN_NETWORK_FAILED") from None
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                locations = "; ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
                trace.log(logger, f"插件网络读取异常；类型：{type(exc).__name__}；调用位置：{locations}", logging.ERROR)
                raise ProtocolError("插件网络读取失败", "PLUGIN_NETWORK_FAILED") from None

    def _request(self, call: NetworkCall, request: NetworkRequest) -> dict[str, JsonValue]:
        """
        每跳独立解析与连接，跨站永久去掉身份头，先关闭前跳再继续。
        """
        with self.stream(call, request) as (url, response):
            return self._result(call, request.method, url, response)

    @contextmanager
    def stream(self, call: NetworkCall, request: NetworkRequest) -> Iterator[tuple[str, httpcore.Response]]:
        """
        为查询或下载逐跳执行同一受控请求，响应只在上下文内可读取。

        :param call: 原始执行授权、任务取消和实际资源归属
        :param request: 严格验证的 GET/HEAD 请求
        :return: 最终规范 URL 与原始响应；退出关闭每跳连接
        :raises ProtocolError: 站点、跳转、网络或期限不满足时抛出
        """
        origin, url = NetworkOrigin.from_url(request.url)
        headers = {name.lower(): value for name, value in request.headers.items()}
        visited: set[str] = set()
        for hop in range(6):
            call.check()
            if origin.reference not in call.lease.resource_refs:
                raise ProtocolError("插件网络站点未获准", "PLUGIN_RESOURCE_DENIED")
            if url in visited:
                raise ProtocolError("插件网络重定向循环", "PLUGIN_REDIRECT_DENIED")
            visited.add(url)
            address = self.service.resolve(call, origin)
            with self.service.pool(origin, address, call) as pool:
                host = origin.host
                if origin.port != (443 if origin.scheme == "https" else 80):
                    host = f"{host}:{origin.port}"
                wire_headers = {"host": host, "accept-encoding": "identity", "connection": "close"} | headers
                with pool.stream(
                    request.method,
                    url,
                    headers=wire_headers,
                    extensions={"timeout": {key: call.timeout() for key in ("connect", "read", "write", "pool")}},
                ) as response:
                    call.check()
                    if response.status not in self.redirects:
                        yield url, response
                        return
                    next_origin, next_url = self._redirect(origin, url, response, hop)
            call.close_streams()
            if next_origin != origin:
                headers.pop("authorization", None)
                headers.pop("x-api-key", None)
            origin, url = next_origin, next_url
        raise ProtocolError("插件网络重定向超过上限", "PLUGIN_REDIRECT_DENIED")

    @staticmethod
    def _redirect(origin: NetworkOrigin, url: str, response: httpcore.Response, hop: int) -> tuple[NetworkOrigin, str]:
        locations = [value for name, value in response.headers if name.lower() == b"location"]
        if hop >= 5 or len(locations) != 1 or len(locations[0]) > 4096:
            raise ProtocolError("插件网络重定向无效或超过上限", "PLUGIN_REDIRECT_DENIED")
        try:
            location = locations[0].decode("ascii")
            if not location or any(ord(char) <= 32 or ord(char) == 127 for char in location):
                raise ValueError("重定向无效")
            next_origin, next_url = NetworkOrigin.from_url(urljoin(url, location))
            if origin.scheme == "https" and next_origin.scheme != "https":
                raise ValueError("不允许降级")
            return next_origin, next_url
        except (ValueError, UnicodeError):
            raise ProtocolError("插件网络重定向无效", "PLUGIN_REDIRECT_DENIED") from None

    def _response_headers(self, response: httpcore.Response) -> tuple[dict[str, str], list[bytes]]:
        """
        返回完整小型实体，响应头只投影允许字段，不存储 Cookie 或解压实体。
        """
        headers: dict[str, str] = {}
        encodings: list[bytes] = []
        for raw_name, raw_value in response.headers:
            name = raw_name.decode("ascii").lower()
            if name == "content-encoding":
                encodings.append(raw_value.lower().strip())
            if name not in self.returned_headers:
                continue
            value = raw_value.decode("latin-1")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ProtocolError("插件网络响应头无效", "PLUGIN_NETWORK_FAILED")
            if name in headers:
                if name != "cache-control":
                    raise ProtocolError("插件网络响应头重复", "PLUGIN_NETWORK_FAILED")
                value = f"{headers[name]}, {value}"
            headers[name] = value
        if sum(len(name.encode()) + len(value.encode()) for name, value in headers.items()) > 4096:
            raise ProtocolError("插件网络响应头超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
        return headers, encodings

    def _result(self, call: NetworkCall, method: str, url: str, response: httpcore.Response) -> dict[str, JsonValue]:
        """
        返回完整小型实体，无体响应不按描述性的 Content-Length 读取。
        """
        headers, encodings = self._response_headers(response)
        has_body = method != "HEAD" and response.status not in {204, 304}
        body = bytearray()
        if has_body:
            if encodings and encodings != [b"identity"]:
                raise ProtocolError("插件网络响应编码不支持", "PLUGIN_NETWORK_ENCODING_UNSUPPORTED")
            if "content-length" in headers and int(headers["content-length"]) > 32768:
                raise ProtocolError("插件网络响应体超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
            for chunk in response.iter_stream():
                call.check()
                if len(body) + len(chunk) > 32768:
                    raise ProtocolError("插件网络响应体超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
                body.extend(chunk)
        call.check()
        return {
            "status_code": response.status,
            "url": url,
            "headers": dict(headers),
            "body_b64": base64.b64encode(body).decode("ascii"),
        }

    def refresh_permissions(self) -> None:
        """
        只取消本 run 的旧租约，不为既有调用换租约或影响其他插件。
        """
        for call in self.service.calls(self):
            try:
                call.check()
            except (ProtocolError, PluginGrantError, GameServiceError):
                call.cancel()
            call.retry_cleanup()
            self.service.reap(call)

    def close(self) -> None:
        """
        拒绝接纳并尝试关闭本 run 全部真实流，不宣称解析已实际结束。
        """
        self._closed.set()
        for call in self.service.calls(self):
            call.cancel()
            self.service.reap(call)

    def is_idle(self) -> bool:
        """
        重试失败清理，只在真实调用、DNS 和流全部结束后返回空闲。

        :return: 本 run 没有真实网络工作或待清理句柄时为 True
        """
        for call in self.service.calls(self):
            if self.is_closed:
                call.close_streams()
            call.retry_cleanup()
            self.service.reap(call)
        return not self.service.calls(self)
