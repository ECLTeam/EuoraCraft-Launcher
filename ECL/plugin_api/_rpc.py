# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在已认证字节通道上分离请求等待与接收，支持有界双向 RPC。
#
# 公开接口：
#   - class RpcRequest — 调用上下文及本地截止时间，不作为授权主体。
#   - class RpcPeer — 并发响应关联、反向调用和有界关闭。
# ============================================================

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic
from typing import ClassVar, Literal, Protocol

from ECL.plugin_api.contracts import JsonValue, ProtocolError, WireMessage


class _Channel(Protocol):
    def send(self, message: WireMessage, timeout: float = 5.0) -> None: ...
    def receive(self, timeout: float = 5.0) -> WireMessage: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """
    保存接收方本地计算的预算；主体由宿主连接登记提供，不能取自 params。
    """

    method: str
    params: dict[str, JsonValue]
    deadline: float
    mutation_key: str = ""
    request_id: str = ""

    def check_deadline(self) -> None:
        """
        在执行敏感阶段前拒绝已经到期的调用，不表示后台工作已经停止。

        :raises ProtocolError: 等待预算已经耗尽时抛出
        """
        if monotonic() >= self.deadline:
            raise ProtocolError("插件调用等待预算已经耗尽", "PLUGIN_DEADLINE_EXCEEDED")


@dataclass(slots=True)
class _Pending:
    completed: Event = field(default_factory=Event)
    response: dict[str, JsonValue] | None = None
    error: ProtocolError | None = None


class RpcPeer:
    """
    独立接收线程关联响应，有界队列和后台执行线程处理反向请求。

    等待方超时仅删除自己的等待记录，迟到响应被丢弃，不宣称执行线程被取消。
    关闭唤醒等待方，业务处理器仍须遵守预算及授权检查点。
    """

    max_pending: ClassVar[int] = 16
    max_queued: ClassVar[int] = 16
    executor_count: ClassVar[int] = 4
    code_pattern: ClassVar[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")

    def __init__(
        self, channel: _Channel, role: Literal["host", "worker"], handler: Callable[[RpcRequest], dict[str, JsonValue]]
    ) -> None:
        """
        建立尚未开始接收的 RPC 对端，允许调用方先绑定受信处理器状态。

        :param channel: 已完成内核身份和运行会话认证的通道
        :param role: 决定 SDK 生成的单调请求标识空间，不赋予权限
        :param handler: 接收方白名单处理器，不能是任意 FrontendApi 转发
        """
        if role not in {"host", "worker"}:
            raise ValueError("RPC 对端角色无效")
        self._channel = channel
        self._role = role
        self._remote_role = "worker" if role == "host" else "host"
        self._handler = handler
        self._pending: dict[str, _Pending] = {}
        self._requests: Queue[tuple[WireMessage, float]] = Queue(self.max_queued)
        self._lock = RLock()
        self._send_lock = Lock()
        self._closed = Event()
        self._stopped = Event()
        self._threads: list[Thread] = []
        self._sequence = 0
        self._remote_sequence = 0
        self._is_started = False

    def start(self) -> None:
        """
        启动接收与执行线程，只允许一次启动。

        :raises ProtocolError: 已启动或已经关闭时抛出
        """
        with self._lock:
            if self._is_started or self._closed.is_set():
                raise ProtocolError("插件 RPC 会话已使用", "PLUGIN_SESSION_EXPIRED")
            self._is_started = True
            self._threads = [Thread(target=self._receive, name="plugin_rpc_receive", daemon=True)]
            self._threads.extend(
                Thread(target=self._execute, name="plugin_rpc_execute", daemon=True) for _ in range(self.executor_count)
            )
            for thread in self._threads:
                thread.start()

    def call(
        self, method: str, params: dict[str, JsonValue], timeout: float = 5.0, *, mutation_key: str = ""
    ) -> dict[str, JsonValue]:
        """
        发出有界调用，接收线程继续处理反向请求和其他响应。

        :param method: 已定义的方法名
        :param params: JSON 请求，不包含可信主体
        :param timeout: 包含排队、发送和等待的总预算
        :param mutation_key: 本运行内副作用提交去重标识，不决定授权
        :return: 校验后的成功或错误信封，由 SDK 门面解释
        :raises ProtocolError: 未启动、并发超限、断线或超时时抛出
        """
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise ProtocolError("插件调用超时范围无效")
        deadline = monotonic() + timeout
        if not self._send_lock.acquire(timeout=timeout):
            raise ProtocolError("等待插件发送队列超时", "PLUGIN_DEADLINE_EXCEEDED")
        request_id = ""
        pending = _Pending()
        try:
            with self._lock:
                if not self._is_started or self._closed.is_set():
                    raise ProtocolError("插件 RPC 会话已关闭", "PLUGIN_PIPE_CLOSED")
                if len(self._pending) >= self.max_pending:
                    raise ProtocolError("插件并发调用超过上限", "PLUGIN_RATE_LIMITED")
                self._sequence += 1
                request_id = f"{self._role}:{self._sequence}"
                self._pending[request_id] = pending
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ProtocolError("等待插件发送队列超时", "PLUGIN_DEADLINE_EXCEEDED")
            self._channel.send(
                WireMessage("request", params, request_id, method, max(1, int(remaining * 1000)), mutation_key),
                remaining,
            )
        except BaseException as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            if isinstance(exc, OSError | EOFError | RuntimeError):
                self.close()
                raise ProtocolError("插件通道发送失败", "PLUGIN_PIPE_CLOSED") from exc
            raise
        finally:
            self._send_lock.release()
        try:
            if not pending.completed.wait(max(0, deadline - monotonic())):
                raise ProtocolError("等待插件调用超时", "PLUGIN_DEADLINE_EXCEEDED")
            if pending.error is not None:
                raise pending.error
            if pending.response is None:
                raise ProtocolError("插件 RPC 响应丢失", "PLUGIN_PROTOCOL_ERROR")
            return pending.response
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def wait_closed(self, timeout: float | None = None) -> bool:
        """
        等待通信关闭，不等同于业务任务或插件进程已经停止。

        :param timeout: 等待秒数；Worker 主循环可等待宿主退出
        :return: 通信是否已关闭
        """
        return self._closed.wait(timeout)

    def shutdown(self, timeout: float = 1.0) -> None:
        """
        请求对端结束控制循环，然后关闭通信；进程回收仍由宿主 Job 确认。

        :param timeout: 正常停止确认的等待上限
        """
        try:
            if not self._closed.is_set():
                self._channel.send(WireMessage("shutdown", {}), timeout)
                self._stopped.wait(timeout)
        finally:
            self.close()

    def close(self) -> None:
        """
        幂等关闭通道并唤醒所有等待方，后台处理器的实际退出由其自身确认。
        """
        with self._lock:
            if not self._closed.is_set():
                self._closed.set()
                for pending in self._pending.values():
                    pending.error = ProtocolError("插件通信已关闭", "PLUGIN_PIPE_CLOSED")
                    pending.completed.set()
                self._pending.clear()
                self._channel.close()
        deadline = monotonic() + 1
        for thread in self._threads:
            if thread is not current_thread():
                thread.join(max(0, deadline - monotonic()))

    def _receive(self) -> None:
        """
        接收线程从不执行插件回调，避免反向调用等待同一线程造成死锁。
        """
        try:
            while not self._closed.is_set():
                try:
                    message = self._channel.receive(0.2)
                except TimeoutError:
                    continue
                if message.kind == "response":
                    self._accept_response(message)
                elif message.kind == "request":
                    self._accept_request(message)
                elif message.kind == "shutdown":
                    self._channel.send(WireMessage("stopped", {}), 1)
                    return
                elif message.kind == "stopped":
                    self._stopped.set()
                    return
                else:
                    raise ProtocolError("插件 RPC 收到错误控制消息")
        except (OSError, EOFError, ValueError, ProtocolError, RuntimeError):
            # 不可信协议或断线只失效当前通道，等待方收到明确关闭状态。
            return
        finally:
            self.close()

    def _accept_response(self, message: WireMessage) -> None:
        """
        先校验信封再关联当前等待，迟到结果不能命中其他调用。
        """
        response = message.params
        if response.get("ok") is True:
            if set(response) != {"ok", "data"} or not isinstance(response.get("data"), dict):
                raise ProtocolError("插件成功响应格式无效")
        elif response.get("ok") is False:
            error = response.get("error")
            if (
                set(response) != {"ok", "error"}
                or not isinstance(error, dict)
                or set(error) != {"code", "message", "retryable"}
            ):
                raise ProtocolError("插件错误响应格式无效")
            if (
                not isinstance(error["code"], str)
                or self.code_pattern.fullmatch(error["code"]) is None
                or not isinstance(error["message"], str)
                or len(error["message"]) > 512
                or type(error["retryable"]) is not bool
            ):
                raise ProtocolError("插件错误响应字段无效")
        else:
            raise ProtocolError("插件响应没有明确结果")
        with self._lock:
            pending = self._pending.get(message.request_id)
            if pending is not None:
                pending.response = response
                pending.completed.set()

    def _accept_request(self, message: WireMessage) -> None:
        """
        单调标识拒绝重放而无需无限保存历史；身份仍由连接登记决定。
        """
        prefix = self._remote_role + ":"
        suffix = message.request_id.removeprefix(prefix)
        if (
            not message.request_id.startswith(prefix)
            or not suffix.isascii()
            or not suffix.isdecimal()
            or len(suffix) > 16
        ):
            raise ProtocolError("插件请求标识空间无效")
        sequence = int(suffix)
        if sequence <= self._remote_sequence:
            raise ProtocolError("插件请求重放或顺序无效")
        self._remote_sequence = sequence
        try:
            self._requests.put_nowait((message, monotonic() + message.timeout_ms / 1000))
        except Full:
            self._send_error(message, "PLUGIN_RATE_LIMITED")

    def _execute(self) -> None:
        """
        有界工作队列执行白名单处理器，任何未预期错误不携带私有异常文本。
        """
        while not self._closed.is_set():
            try:
                message, deadline = self._requests.get(timeout=0.1)
            except Empty:
                continue
            try:
                request = RpcRequest(message.method, message.params, deadline, message.mutation_key, message.request_id)
                request.check_deadline()
                result = self._handler(request)
                request.check_deadline()
                self._channel.send(WireMessage("response", {"ok": True, "data": result}, message.request_id), 1)
            except ProtocolError as exc:
                self._send_error(message, exc.error_code)
            except (Exception, KeyboardInterrupt, SystemExit):
                # 外部回调边界，不转发插件可控制的异常文本或宿主私有路径。
                self._send_error(message, "PLUGIN_CALLBACK_FAILED")
            finally:
                self._requests.task_done()

    def _send_error(self, message: WireMessage, code: str) -> None:
        """
        只发送稳定错误类别；对端已关闭时不再尝试交付或恢复会话。
        """
        if self.code_pattern.fullmatch(code) is None:
            code = "PLUGIN_CALLBACK_FAILED"
        try:
            self._channel.send(
                WireMessage(
                    "response",
                    {
                        "ok": False,
                        "error": {"code": code, "message": "插件调用未完成", "retryable": False},
                    },
                    message.request_id,
                ),
                1,
            )
        except (OSError, EOFError, ValueError, ProtocolError, RuntimeError):
            self.close()
