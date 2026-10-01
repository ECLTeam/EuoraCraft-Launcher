# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：联机领域 IPC 处理器：房间创建/加入、节点与 NAT 检测、玩家管理。
#
# 公开接口：
#   - class ConnectorHandlers — 联机功能的 IPC 命令处理器。
#       - connector_status(body) -> ApiResponse — 查询联机服务的当前状态。
#       - connector_host_port(body) -> ApiResponse — 在联机服务中对外开放并托管指定端口。
#       - connector_host_instance(body) -> ApiResponse — 在联机服务中托管一个指定的本地游戏实例。
#       - connector_join(body) -> ApiResponse — 通过房间码加入他人托管的联机房间。
#       - connector_leave(body) -> ApiResponse — 从当前联机房间退出。
#       - connector_kick(body) -> ApiResponse — 从当前房间踢出指定的参与机器。
#       - connector_match_instances(body) -> ApiResponse — 匹配可用的联机实例，当前返回空占位。
#       - connector_easytier_status(body) -> ApiResponse — 查询 EasyTier 组网状态。
#       - connector_easytier_download(body) -> ApiResponse — 请求 EasyTier 组网组件下载，返回其当前状态。
#       - connector_detect_ports(body) -> ApiResponse — 探测本机 Java 进程开放的候选端口。
#       - connector_search_mc_port(body) -> ApiResponse — 在候选端口中搜索确认 Minecraft 服务端口。
#       - connector_nat_type(body) -> ApiResponse — 查询本机网络的 NAT 类型。
#       - connector_nodes_get(body) -> ApiResponse — 读取下一次连接的节点策略。
#       - connector_nodes_set(body) -> ApiResponse — 校验并保存节点策略。
# ============================================================

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import threading
from typing import Any

from ECL.api.contracts import ApiResponse, failure, success
from ECL.api.models import InstanceTarget, KickRequest, PortRequest, PortsRequest, RoomCodeRequest
from ECL.services.connector import ConnectorError, ConnectorNatError, ConnectorNotAvailableError
from ECL.services.connector_nodes import ConnectorNodeSettings

from .bridge import _FrontendState, _ipc_handler


def _run_in_daemon(func, *args):
    # 在守护线程中执行阻塞调用，避免窗口关闭后进程等待该线程退出。
    future = concurrent.futures.Future()

    def runner() -> None:
        try:
            future.set_result(func(*args))
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=runner, daemon=True, name="ECL-connector").start()
    return asyncio.wrap_future(future)


def _connector_guard(error_code: str):
    # 为联机 IPC 处理器统一映射 Connector 异常到稳定失败响应。

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, body, *args, **kwargs):
            try:
                return await func(self, body, *args, **kwargs)
            except ConnectorNotAvailableError as exc:
                return failure(str(exc), "CONNECTOR_NOT_AVAILABLE")
            except ConnectorError as exc:
                return failure(str(exc), error_code)

        return wrapper

    return decorator


class ConnectorHandlers(_FrontendState):
    """
    联机功能的 IPC 命令处理器。
    """

    @_ipc_handler("CONNECTOR_NODE_SETTINGS_FAILED")
    async def connector_nodes_get(self, body: dict[str, Any]) -> ApiResponse:
        """
        读取下一次连接的节点策略，不改变活动房间。

        :param body: 空请求体
        :return: 已校验的节点设置
        """
        return success(ConnectorNodeSettings.model_validate(self.config.get_config("connector") or {}).model_dump())

    @_ipc_handler("CONNECTOR_NODE_SETTINGS_FAILED")
    async def connector_nodes_set(self, body: dict[str, Any]) -> ApiResponse:
        """
        原子持久化节点策略，下一次创建或加入房间时应用。

        :param body: 节点策略和 URI 列表
        :return: 规范化后的设置
        """
        settings = ConnectorNodeSettings.model_validate(body).model_dump()
        await asyncio.to_thread(self.config.save_config, "connector", settings)
        return success(settings)

    @_ipc_handler("CONNECTOR_NOT_AVAILABLE")
    async def connector_status(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询联机服务的当前状态。

        ``get_status`` 可能同步等待玩家列表（最长 5s），放到线程池执行，
        避免前端 2s 一次的轮询阻塞 IPC 事件循环。
        """
        return success(await _run_in_daemon(self.connector.get_status))

    @_ipc_handler("CONNECTOR_HOST_PORT_FAILED")
    @_connector_guard("CONNECTOR_HOST_PORT_FAILED")
    async def connector_host_port(self, body: dict[str, Any]) -> ApiResponse:
        """
        在联机服务中对外开放并托管指定端口。
        """
        port = PortRequest.model_validate(body).port
        result = await _run_in_daemon(self.connector.host_port, port)
        return success(result)

    @_ipc_handler("CONNECTOR_HOST_INSTANCE_FAILED")
    @_connector_guard("CONNECTOR_HOST_INSTANCE_FAILED")
    async def connector_host_instance(self, body: dict[str, Any]) -> ApiResponse:
        """
        在联机服务中托管一个指定的本地游戏实例。
        """
        target = InstanceTarget.model_validate(body)
        result = await _run_in_daemon(self.connector.host_instance, target.game_path, target.version_id)
        return success(result)

    @_ipc_handler("CONNECTOR_JOIN_FAILED")
    @_connector_guard("CONNECTOR_JOIN_FAILED")
    async def connector_join(self, body: dict[str, Any]) -> ApiResponse:
        """
        通过房间码加入他人托管的联机房间。
        """
        code = RoomCodeRequest.model_validate(body).code
        result = await _run_in_daemon(self.connector.join, code)
        return success(result)

    @_ipc_handler("CONNECTOR_LEAVE_FAILED")
    @_connector_guard("CONNECTOR_LEAVE_FAILED")
    async def connector_leave(self, body: dict[str, Any]) -> ApiResponse:
        """
        从当前联机房间退出。
        """
        result = self.connector.leave()
        return success(result)

    @_ipc_handler("CONNECTOR_KICK_FAILED")
    @_connector_guard("CONNECTOR_KICK_FAILED")
    async def connector_kick(self, body: dict[str, Any]) -> ApiResponse:
        """
        从当前房间踢出指定的参与机器。
        """
        machine_id = KickRequest.model_validate(body).machine_id
        result = self.connector.kick(machine_id)
        return success(result)

    @_ipc_handler("CONNECTOR_MATCH_FAILED")
    async def connector_match_instances(self, body: dict[str, Any]) -> ApiResponse:
        """
        匹配可用的联机实例，当前返回空占位。
        """
        return success({"mods": [], "instances": []})

    @_ipc_handler("CONNECTOR_EASYTIER_STATUS_FAILED")
    async def connector_easytier_status(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询 EasyTier 组网状态。
        """
        return success(self.connector.get_easytier_status())

    @_ipc_handler("CONNECTOR_EASYTIER_DOWNLOAD_FAILED")
    async def connector_easytier_download(self, body: dict[str, Any]) -> ApiResponse:
        """
        请求 EasyTier 组网组件下载，返回其当前状态。
        """
        return success(self.connector.get_easytier_status())

    @_ipc_handler("CONNECTOR_SCAN_PORTS_FAILED")
    async def connector_detect_ports(self, body: dict[str, Any]) -> ApiResponse:
        """
        探测本机 Java 进程开放的候选端口。
        """
        return success(self.connector.detect_ports())

    @_ipc_handler("CONNECTOR_SCAN_PORTS_FAILED")
    async def connector_search_mc_port(self, body: dict[str, Any]) -> ApiResponse:
        """
        在候选端口中搜索确认 Minecraft 服务端口。
        """
        ports = PortsRequest.model_validate(body).ports
        return success(self.connector.search_mc_port(ports))

    @_ipc_handler("CONNECTOR_NAT_TYPE_FAILED")
    async def connector_nat_type(self, body: dict[str, Any]) -> ApiResponse:
        """
        在线程中探测本机 NAT，保留领域错误码并避免阻塞 IPC 事件循环。

        :param body: 无需参数的 IPC 请求体
        :return: NAT 探测结果或保留 NAT 领域错误码的失败响应
        """
        try:
            result = await _run_in_daemon(self.connector.get_nat_type)
        except ConnectorNatError as exc:
            return failure(str(exc), exc.error_code)
        return success(result)
