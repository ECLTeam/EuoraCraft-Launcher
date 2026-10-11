# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：使用受控 DACL 的本地命名管道传输 JSON 字节，不启用 Pickle 或 TCP。
#
# 公开接口：
#   - class PipeChannel — 有界传输、另一端身份查询和关闭。
#   - class PipeServer — 仅允许一个连接的本地管道服务器。
#   - connect_pipe() — 连接并核实宿主的真实 PID。
# ============================================================

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol, cast

from ecl_plugin_api._windows import WindowsApi, WindowsRuntimeError
from ecl_plugin_api.contracts import ProtocolPolicy, WireMessage


class _ByteConnection(Protocol):
    def send_bytes(self, value: bytes) -> None: ...
    def recv_bytes(self, maxlength: int) -> bytes: ...
    def close(self) -> None: ...
    def fileno(self) -> int: ...
    def poll(self, timeout: float) -> bool: ...


@dataclass(slots=True)
class _IoResult:
    value: bytes | None = None
    error: Exception | None = None


class PipeChannel:
    """
    只调用标准库管道的 send_bytes/recv_bytes，拒绝对象反序列化。

    发送和接收分别串行；超时关闭通道，未结束的 I/O 持有自己的 C 缓冲直至完成。
    """

    def __init__(self, connection: _ByteConnection, *, is_server: bool) -> None:
        """
        接管一个管道句柄的唯一关闭责任。

        :param connection: 具有 overlapped 句柄的消息管道
        :param is_server: 是否为宿主服务器端
        """
        self._connection = connection
        self._api = WindowsApi()
        self._is_server = is_server
        self._read_lock = Lock()
        self._write_lock = Lock()
        self._close_lock = Lock()
        self._closed = False

    def peer_pid(self) -> int:
        """
        从系统查询另一端 PID，不采用协议消息中的自报值。

        :return: 已连接另一端的 PID
        """
        return self._api.pipe_pid(self._connection.fileno(), is_server=self._is_server)

    def send(self, message: WireMessage, timeout: float = 5.0) -> None:
        """
        在有界等待中发送已经限长的 JSON 消息。

        :param message: 要发送的协议消息
        :param timeout: 最长等待秒数
        :raises WindowsRuntimeError: 对端未读取、关闭或写入失败时抛出
        """
        raw = message.encode()
        with self._write_lock:
            self._io(lambda: self._connection.send_bytes(raw), timeout)

    def receive(self, timeout: float = 5.0) -> WireMessage:
        """
        限制原生读取大小，再校验 JSON，不使用 recv()。

        :param timeout: 最长等待秒数
        :return: 已校验消息
        :raises WindowsRuntimeError: 对端未响应、断线或读取失败时抛出
        """
        with self._read_lock:
            if not self._connection.poll(timeout):
                raise TimeoutError("等待插件消息超时")
            raw = self._io(lambda: self._connection.recv_bytes(ProtocolPolicy.max_frame_bytes), timeout)
        if raw is None:
            raise WindowsRuntimeError("插件管道未返回消息", "PLUGIN_PIPE_CLOSED")
        return WireMessage.decode(raw)

    def _io(self, operation: Callable[[], bytes | None], timeout: float) -> bytes | None:
        """
        将可能等待对端的原生 I/O 置于后台边界，超时后不再复用通道。
        """
        if self._closed or not 0 < timeout <= 120:
            raise WindowsRuntimeError("插件管道已关闭或等待范围无效", "PLUGIN_PIPE_CLOSED")
        result = _IoResult()
        completed = Event()

        def execute() -> None:
            try:
                result.value = operation()
            except (OSError, EOFError, ValueError) as exc:
                result.error = exc
            finally:
                completed.set()

        thread = Thread(target=execute, name="plugin_pipe_io", daemon=True)
        thread.start()
        if not completed.wait(timeout):
            self.close()
            raise WindowsRuntimeError("等待插件管道超时", "PLUGIN_DEADLINE_EXCEEDED")
        if result.error is not None:
            self.close()
            raise WindowsRuntimeError("插件管道已断开", "PLUGIN_PIPE_CLOSED") from result.error
        return result.value

    def close(self) -> None:
        """
        幂等取消未完成 I/O 并关闭句柄；调用方仍须回收关联 Worker。
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            handle = self._connection.fileno()
            self._api.kernel.CancelIoEx(handle, None)
            self._connection.close()


class PipeServer:
    """
    创建只接受一个本地客户端且拒绝远程连接的消息管道。
    """

    def __init__(self, address: str) -> None:
        """
        使用显式 DACL 创建不继承的服务器句柄。

        :param address: 宿主生成的唯一管道地址
        :raises WindowsRuntimeError: 创建失败或平台不支持时抛出
        """
        self._api = WindowsApi()
        self._handle: int | None = None
        self.address: str = address
        import _winapi

        attributes, descriptor = self._api.pipe_security()
        try:
            self._handle = _winapi.CreateNamedPipe(
                address,
                _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
                _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | 8,
                1,
                65536,
                65536,
                0,
                ctypes.addressof(attributes),
            )
        finally:
            self._api.kernel.LocalFree(descriptor)

    def accept(self, timeout: float = 30.0) -> PipeChannel:
        """
        有界等待冻结引导的实际 Worker，不启动其他管道实例。

        :param timeout: 包含冷启动解包的等待上限
        :return: 接管服务器句柄的唯一通道
        :raises WindowsRuntimeError: 等待或连接失败时抛出
        """
        import _winapi
        from multiprocessing.connection import PipeConnection

        if self._handle is None or not 0 < timeout <= 120:
            raise WindowsRuntimeError("插件管道服务器已关闭", "PLUGIN_PIPE_CLOSED")
        overlapped = _winapi.ConnectNamedPipe(self._handle, overlapped=True)
        try:
            if _winapi.WaitForSingleObject(overlapped.event, int(timeout * 1000)) != 0:
                overlapped.cancel()
                raise WindowsRuntimeError("等待插件连接超时", "PLUGIN_DEADLINE_EXCEEDED")
            _, error = overlapped.GetOverlappedResult(True)
            if error:
                raise WindowsRuntimeError("插件管道连接失败", "PLUGIN_PIPE_CLOSED", error)
            connection = cast(_ByteConnection, PipeConnection(self._handle))
            self._handle = None
            return PipeChannel(connection, is_server=True)
        except BaseException:
            overlapped.cancel()
            self.close()
            raise

    def close(self) -> None:
        """
        关闭尚未转交给通道的服务器句柄。
        """
        if self._handle is not None:
            self._api.close(self._handle)
            self._handle = None


def connect_pipe(address: str, host_pid: int, timeout: float = 5.0) -> PipeChannel:
    """
    连接本地宿主并复核服务器 PID，拒绝错误服务器。

    :param address: 公开端点名，不作为身份秘密
    :param host_pid: 受信启动参数中的宿主 PID
    :param timeout: 建立连接的最长等待秒数
    :return: 需显式关闭的 Worker 通道
    :raises WindowsRuntimeError: 平台、连接或宿主身份不符时抛出
    """
    if sys.platform != "win32":
        raise WindowsRuntimeError("当前平台尚不支持插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")
    import _winapi
    from multiprocessing.connection import PipeConnection

    deadline = monotonic() + timeout
    while True:
        try:
            _winapi.WaitNamedPipe(address, max(1, min(1000, int((deadline - monotonic()) * 1000))))
            handle = _winapi.CreateFile(
                address,
                0x12019B,
                0,
                0,
                _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED,
                0,
            )
            break
        except OSError as exc:
            if exc.winerror not in {2, 121, 231} or monotonic() >= deadline:
                raise WindowsRuntimeError("无法连接插件宿主", "PLUGIN_PIPE_CLOSED") from exc
    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        channel = PipeChannel(cast(_ByteConnection, PipeConnection(handle)), is_server=False)
        if channel.peer_pid() != host_pid:
            channel.close()
            raise WindowsRuntimeError("插件宿主身份不符", "PLUGIN_PIPE_AUTH_FAILED")
        return channel
    except BaseException:
        # PipeChannel 接管后已自行关闭；接管前失败才需要释放原始句柄。
        if "channel" not in locals():
            _winapi.CloseHandle(handle)
        raise
