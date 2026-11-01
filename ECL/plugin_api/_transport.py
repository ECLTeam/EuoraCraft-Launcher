# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按实际平台创建本地认证字节通道，不提供 TCP 或对象反序列化回退。
#
# 公开接口：
#   - LocalChannel / LocalServer — Worker 与监管方共用的通信契约。
#   - create_local_server — 为单次运行创建唯一端点。
#   - connect_local — 连接并从系统校验宿主身份。
# ============================================================

from __future__ import annotations

import sys
from typing import Protocol

from ECL.bootstrap import local_worker_endpoint
from ECL.plugin_api._errors import PluginRuntimeError
from ECL.plugin_api.contracts import WireMessage


class LocalChannel(Protocol):
    """
    只传输有界 JSON，身份来自本地传输内核接口。
    """

    def peer_pid(self) -> int:
        """
        从传输内核接口返回真实对端 PID。

        :return: 建立连接时绑定的进程身份
        """
        ...

    def send(self, message: WireMessage, timeout: float = 5.0) -> None:
        """
        在有界预算内发送完整帧，不能交错并发消息。

        :param message: 当前协议消息
        :param timeout: 完整发送的等待上限
        """
        ...

    def receive(self, timeout: float = 5.0) -> WireMessage:
        """
        在有界预算内读取限长帧，半帧失效后关闭连接。

        :param timeout: 本次完整接收的预算
        :return: 已校验的协议消息
        :raises TimeoutError: 尚未开始接收帧的空闲超时
        """
        ...

    def close(self) -> None:
        """
        幂等释放本对象持有的本地通信资源。
        """
        ...


class LocalServer(Protocol):
    """
    一次运行只接纳一个连接，显式释放监听与端点资源。
    """

    address: str

    def accept(self, timeout: float = 30.0) -> LocalChannel:
        """
        只接纳一个本地连接，身份仍由上层监管范围复核。

        :param timeout: 等待连接的上限
        :return: 调用方持有的通道
        """
        ...

    def close(self) -> None:
        """
        幂等释放本对象持有的本地通信资源。
        """
        ...


def create_local_server(run_id: str) -> LocalServer:
    """
    延迟导入对应原生实现，不通过环境变量选择不安全传输。

    :param run_id: 宿主生成的运行标识
    :return: 尚未转交连接的本地服务器
    :raises PluginRuntimeError: 平台不支持或端点创建失败
    """
    address = local_worker_endpoint(run_id)
    if sys.platform == "win32":
        from ECL.plugin_api._windows_transport import PipeServer

        return PipeServer(address)
    if sys.platform in {"linux", "darwin"}:
        from ECL.plugin_api._posix_transport import UnixServer

        return UnixServer(address)
    raise PluginRuntimeError("当前平台尚不支持插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")


def connect_local(address: str, host_pid: int, timeout: float = 5.0) -> LocalChannel:
    """
    从实际本地连接核实宿主 PID，失败不尝试其他通道。

    :param address: 统一入口已校验的运行端点
    :param host_pid: 受信引导给定的宿主 PID
    :param timeout: 建连等待上限
    :return: 调用方拥有的认证通道
    :raises PluginRuntimeError: 平台、连接或身份不满足要求
    """
    if sys.platform == "win32":
        from ECL.plugin_api._windows_transport import connect_pipe

        return connect_pipe(address, host_pid, timeout)
    if sys.platform in {"linux", "darwin"}:
        from ECL.plugin_api._posix_transport import connect_unix

        return connect_unix(address, host_pid, timeout)
    raise PluginRuntimeError("当前平台尚不支持插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")
