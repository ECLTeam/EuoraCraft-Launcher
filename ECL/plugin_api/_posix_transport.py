# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：以私有 Unix socket 和内核对端凭据传输有界 JSON，不接受 TCP 回退。
#
# 公开接口：
#   - class UnixChannel — 独立收发锁、帧预算及系统身份校验。
#   - class UnixServer — 单运行私有目录和单连接监听。
#   - connect_unix — 验证目录和宿主 PID 后连接。
# ============================================================

from __future__ import annotations

import ctypes
import errno
import os
import select
import socket
import stat
import struct
import sys
from pathlib import Path
from threading import Lock
from time import monotonic, sleep

from ECL.plugin_api._errors import PluginRuntimeError
from ECL.plugin_api.contracts import ProtocolPolicy, WireMessage


def _credentials(connection: socket.socket) -> tuple[int, int]:
    """
    查询已经建立的本地连接，不读取协议提供的身份。
    """
    if sys.platform == "linux":
        pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return pid, uid
    if sys.platform == "darwin":
        # Apple sys/un.h：SOL_LOCAL = 0，LOCAL_PEERPID = 2。
        pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
        library = ctypes.CDLL(None, use_errno=True)
        function = library.getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise PluginRuntimeError("不能查询本地连接身份", "PLUGIN_PIPE_AUTH_FAILED", ctypes.get_errno())
        return pid, uid.value
    raise PluginRuntimeError("当前平台没有受支持的本地凭据接口", "PLUGIN_RUNTIME_UNSUPPORTED")


def _private_directory(path: Path) -> os.stat_result:
    """
    拒绝链接、其他所有者及组/其他用户可访问的运行目录。
    """
    information = path.lstat()
    if not stat.S_ISDIR(information.st_mode) or information.st_uid != os.geteuid() or information.st_mode & 0o077:
        raise PluginRuntimeError("插件运行目录不满足私有边界", "PLUGIN_PIPE_AUTH_FAILED")
    return information


class UnixChannel:
    """
    使用长度前缀限制接收大小；空闲轮询超时可以重试，半帧超时关闭通道。
    """

    def __init__(self, connection: socket.socket) -> None:
        """
        接管已连接 socket，检查内核 UID 后再接收任何业务消息。

        :param connection: 单次运行的本地连接
        :raises PluginRuntimeError: 对端身份或通道类型无效
        """
        self._socket = connection
        self._read_lock = Lock()
        self._write_lock = Lock()
        self._close_lock = Lock()
        self._closed = False
        try:
            if connection.family != socket.AF_UNIX:
                raise PluginRuntimeError("插件通信必须为本地通道", "PLUGIN_PIPE_AUTH_FAILED")
            self._peer_pid, uid = _credentials(connection)
            if uid != os.geteuid() or self._peer_pid <= 0:
                raise PluginRuntimeError("本地连接身份不符", "PLUGIN_PIPE_AUTH_FAILED")
            connection.setblocking(False)
        except BaseException:
            connection.close()
            raise

    def peer_pid(self) -> int:
        """
        返回连接建立时由内核绑定的进程身份。

        :return: 真实对端 PID
        """
        return self._peer_pid

    def _ready(self, *, writing: bool, deadline: float) -> None:
        """
        使用独立读写等待，避免修改共享 socket 超时状态。
        """
        if self._closed:
            raise PluginRuntimeError("插件通道已关闭", "PLUGIN_PIPE_CLOSED")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("等待插件消息超时")
        readable, writable, _ = select.select(
            [] if writing else [self._socket], [self._socket] if writing else [], [], remaining
        )
        if not (writable if writing else readable):
            raise TimeoutError("等待插件消息超时")

    def send(self, message: WireMessage, timeout: float = 5.0) -> None:
        """
        在一个发送预算内写完长度与正文，不交错多线程消息。

        :param message: 已校验的 JSON 消息
        :param timeout: 最长等待秒数
        :raises PluginRuntimeError: 超时、断开或预算无效
        """
        if not 0 < timeout <= 120:
            raise PluginRuntimeError("消息等待范围无效", "PLUGIN_INVALID_REQUEST")
        content = message.encode()
        frame = memoryview(struct.pack("!I", len(content)) + content)
        deadline = monotonic() + timeout
        with self._write_lock:
            try:
                while frame:
                    self._ready(writing=True, deadline=deadline)
                    count = self._socket.send(frame)
                    if not count:
                        raise EOFError
                    frame = frame[count:]
            except (TimeoutError, OSError, EOFError, ValueError) as exc:
                self.close()
                code = "PLUGIN_DEADLINE_EXCEEDED" if isinstance(exc, TimeoutError) else "PLUGIN_PIPE_CLOSED"
                raise PluginRuntimeError("插件消息发送未完成", code) from None

    def _read_exact(self, count: int, deadline: float) -> bytes:
        """
        在同一截止内读满已验证的字节数量，EOF 不解释为合法空帧。
        """
        content = bytearray()
        while len(content) < count:
            self._ready(writing=False, deadline=deadline)
            block = self._socket.recv(count - len(content))
            if not block:
                raise EOFError
            content.extend(block)
        return bytes(content)

    def receive(self, timeout: float = 5.0) -> WireMessage:
        """
        读取长度后先核对上限，再分配正文；半帧失败不可继续解析后续消息。

        :param timeout: 包含整个帧的等待预算
        :return: 当前协议消息
        :raises TimeoutError: 尚未收到任何帧时轮询超时
        :raises PluginRuntimeError: 半帧、过长或断开
        """
        if not 0 < timeout <= 120:
            raise PluginRuntimeError("消息等待范围无效", "PLUGIN_INVALID_REQUEST")
        deadline = monotonic() + timeout
        with self._read_lock:
            # 只有开始读帧前的超时可以被 RPC 的空闲轮询重试。
            self._ready(writing=False, deadline=deadline)
            try:
                length = struct.unpack("!I", self._read_exact(4, deadline))[0]
                if not 0 < length <= ProtocolPolicy.max_frame_bytes:
                    raise PluginRuntimeError("插件消息长度超限", "PLUGIN_MESSAGE_TOO_LARGE")
                return WireMessage.decode(self._read_exact(length, deadline))
            except (OSError, EOFError, ValueError) as exc:
                self.close()
                code = "PLUGIN_DEADLINE_EXCEEDED" if isinstance(exc, TimeoutError) else "PLUGIN_PIPE_CLOSED"
                raise PluginRuntimeError("插件消息未完整接收", code) from None
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        """
        幂等关闭并唤醒正在等待的另一方向，不等待插件回调完成。
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                if exc.errno not in {errno.ENOTCONN, errno.EBADF}:
                    raise PluginRuntimeError("本地通道关闭未完成", "PLUGIN_PIPE_CLOSED", exc.errno or 0) from None
            finally:
                self._socket.close()


class UnixServer:
    """
    在固定短路径创建单运行私有目录，端点关闭时只清理自己的对象。
    """

    def __init__(self, address: str) -> None:
        """
        独占创建运行目录，不接管上次运行或外部创建的同名端点。

        :param address: 宿主生成并与运行标识绑定的地址
        :raises PluginRuntimeError: 平台、权限或创建失败
        """
        if sys.platform not in {"linux", "darwin"}:
            raise PluginRuntimeError("当前平台不支持 Unix 插件通道", "PLUGIN_RUNTIME_UNSUPPORTED")
        self.address = address
        self._path = Path(address)
        self._directory = self._path.parent
        self._listener: socket.socket | None = None
        self._identity: tuple[int, int] | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._directory.mkdir(mode=0o700)
        try:
            information = _private_directory(self._directory)
            self._identity = (information.st_dev, information.st_ino)
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(address)
            information = self._path.lstat()
            self._socket_identity = (information.st_dev, information.st_ino)
            self._listener.listen(1)
        except BaseException:
            self.close()
            raise

    def accept(self, timeout: float = 30.0) -> UnixChannel:
        """
        有界接收一个连接，调用方继续校验进程监管归属。

        :param timeout: 包含冻结冷启动的等待上限
        :return: 已校验 UID 的本地通道
        :raises PluginRuntimeError: 超时、重复接收或服务器已关闭
        """
        if self._listener is None or not 0 < timeout <= 120:
            raise PluginRuntimeError("插件监听已关闭", "PLUGIN_PIPE_CLOSED")
        self._listener.settimeout(timeout)
        try:
            connection, _ = self._listener.accept()
            self._listener.close()
            self._listener = None
            return UnixChannel(connection)
        except TimeoutError:
            raise PluginRuntimeError("等待插件连接超时", "PLUGIN_DEADLINE_EXCEEDED") from None

    def close(self) -> None:
        """
        只删除身份仍匹配的 socket 和空运行目录，不递归清理路径。
        """
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        try:
            information = self._directory.lstat()
        except FileNotFoundError:
            return
        if (information.st_dev, information.st_ino) != self._identity:
            return
        try:
            information = self._path.lstat()
            if stat.S_ISSOCK(information.st_mode) and (information.st_dev, information.st_ino) == self._socket_identity:
                self._path.unlink()
        except FileNotFoundError:
            pass
        self._directory.rmdir()


def connect_unix(address: str, host_pid: int, timeout: float = 5.0) -> UnixChannel:
    """
    先检查目录权限，再以系统对端 PID 认证宿主；失败不连接备用地址。

    :param address: 当前运行的唯一端点
    :param host_pid: 受信入口指定的宿主 PID
    :param timeout: 整体建立连接预算
    :return: 由 Worker 关闭的本地通道
    :raises PluginRuntimeError: 所有者、权限、端点或宿主身份无效
    """
    if sys.platform not in {"linux", "darwin"} or not 0 < timeout <= 120:
        raise PluginRuntimeError("当前平台或等待范围无效", "PLUGIN_RUNTIME_UNSUPPORTED")
    information = _private_directory(Path(address).parent)
    identity = (information.st_dev, information.st_ino)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = monotonic() + timeout
    try:
        while True:
            try:
                connection.settimeout(max(0.001, deadline - monotonic()))
                connection.connect(address)
                break
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.ECONNREFUSED} or monotonic() >= deadline:
                    raise PluginRuntimeError("不能连接插件宿主", "PLUGIN_PIPE_CLOSED") from None
                sleep(min(0.025, max(0, deadline - monotonic())))
        information = _private_directory(Path(address).parent)
        if identity != (information.st_dev, information.st_ino):
            raise PluginRuntimeError("运行端点身份已变化", "PLUGIN_PIPE_AUTH_FAILED")
        channel = UnixChannel(connection)
        if channel.peer_pid() != host_pid:
            channel.close()
            raise PluginRuntimeError("本地宿主身份不符", "PLUGIN_PIPE_AUTH_FAILED")
        return channel
    except BaseException:
        connection.close()
        raise
