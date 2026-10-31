# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：第三方 API v2 的轻量宿主门面和本地回调注册，不取得宿主 Python 对象。
#
# 公开接口：
#   - class HostApiError — Host API 明确返回的稳定失败类别。
#   - class HostClient — 只通过已绑定 RPC 连接访问宿主服务。
#   - class Plugin — Worker 内的插件基类及生命周期。
#       - define_command — 登记本插件命令。
#       - define_panel — 登记由宿主渲染的声明式面板。
# ============================================================

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from threading import RLock
from typing import ClassVar
from uuid import uuid4

from ECL.plugin_api._rpc import RpcPeer
from ECL.plugin_api.contracts import JsonValue, ProtocolError


class HostApiError(ProtocolError):
    """
    表示宿主明确拒绝或业务失败，不能将失败解释成空成功。
    """


class HostClient:
    """
    仅通过当前运行连接调用白名单 Host API，不包含配置、账户、事件总线或桌面对象。
    """

    def __init__(self, peer: RpcPeer) -> None:
        """
        绑定受信 Worker 引导创建的连接，插件不得另开宿主连接恢复旧会话。

        :param peer: 绑定当前插件运行身份的 RPC 对端
        """
        self._peer = peer

    def call(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        timeout: float = 5.0,
        *,
        mutation_key: str | None = None,
    ) -> dict[str, JsonValue]:
        """
        调用宿主方法并将明确错误转换为异常，不重试有副作用的提交。

        等待超时不证明宿主没有提交；任务或提交回执须通过相应查询确认。

        :param method: 已定义的 Host API 名称，未实现方法明确失败
        :param params: 有界 JSON 输入，不决定身份、授权或资源所有者
        :param timeout: 等待总预算，不能扩大宿主的方法上限
        :param mutation_key: 本运行内的提交去重键；None 生成新键，不自动重发
        :return: 校验后的宿主结果
        :raises HostApiError: 宿主明确拒绝调用时抛出
        :raises ProtocolError: 超时、断线或协议失败时抛出
        """
        reply = self._peer.call(
            method, params if params is not None else {}, timeout, mutation_key=mutation_key or uuid4().hex
        )
        if reply["ok"] is True:
            result = reply["data"]
            assert isinstance(result, dict)
            return result
        error = reply["error"]
        assert isinstance(error, dict) and isinstance(error["code"], str)
        raise HostApiError("插件宿主调用未获准或未完成", error["code"])

    async def call_async(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        timeout: float = 5.0,
        *,
        mutation_key: str | None = None,
    ) -> dict[str, JsonValue]:
        """
        在后台等待同步 RPC，不阻塞插件自己的异步回调循环。

        协程取消只取消等待方，已经发送的宿主操作仍须查询实际结果。

        :param method: 已定义的 Host API 名称
        :param params: JSON 方法参数
        :param timeout: 调用等待总预算
        :param mutation_key: 本运行内的去重键
        :return: 校验后的宿主结果
        :raises HostApiError: 宿主明确拒绝时抛出
        """
        return await asyncio.to_thread(self.call, method, params, timeout, mutation_key=mutation_key)


class Plugin:
    """
    在独立 Worker 中运行的 v2 插件基类，不兼容旧宿主对象构造参数。

    生命周期可覆写为同步或 async 方法；回调函数只留在本 Worker，跨进程只传标识。
    """

    max_callbacks: ClassVar[int] = 128

    def __init__(self, host: HostClient) -> None:
        """
        保存当前运行的宿主门面并建立有界本地回调表。

        :param host: 当前运行的 RPC 宿主门面，不是 Launcher 或 PluginManager
        """
        self.host: HostClient = host
        self._callbacks: dict[str, Callable[..., JsonValue | Awaitable[JsonValue]]] = {}
        self._callback_lock = RLock()

    def register_callback(self, callback: Callable[..., JsonValue | Awaitable[JsonValue]]) -> str:
        """
        保留本地回调并返回不携带函数对象的标识。

        :param callback: 本 Worker 内的同步或异步回调
        :return: 可以在 Host API 定义中引用的回调标识
        :raises ProtocolError: 回调无效或超过本运行上限时抛出
        """
        if not callable(callback):
            raise ProtocolError("插件回调必须可以调用", "PLUGIN_SCHEMA_INVALID")
        with self._callback_lock:
            if len(self._callbacks) >= self.max_callbacks:
                raise ProtocolError("插件回调数量超过上限", "PLUGIN_RATE_LIMITED")
            reference = uuid4().hex
            self._callbacks[reference] = callback
            return reference

    def define_command(
        self,
        name: str,
        callback: Callable[..., JsonValue | Awaitable[JsonValue]],
        *,
        description: str = "",
        input_fields: tuple[dict[str, JsonValue], ...] = (),
        output_fields: tuple[dict[str, JsonValue], ...] = (),
        exported: bool = False,
    ) -> str:
        """
        向宿主提交命令定义，实际授权与输入输出 schema 由宿主复核。

        等待失败时保留本地回调，避免迟到提交指向已经删除的回调；停止 Worker
        后所有本运行注册由宿主清理，不自动重试提交。

        :param name: 自己命名空间内的命令名
        :param callback: 留在本 Worker 的处理器
        :param description: 命令说明
        :param input_fields: 输入基础字段定义
        :param output_fields: 输出基础字段定义
        :param exported: 是否允许明确授权的其他插件调用
        :return: 宿主登记的命令引用
        :raises HostApiError: 未授权、未实现或定义冲突时抛出
        """
        reference = self.register_callback(callback)
        result = self.host.call(
            "commands.register",
            {
                "name": name,
                "description": description,
                "input_fields": list(input_fields),
                "output_fields": list(output_fields),
                "callback_ref": reference,
                "exported": exported,
            },
        )
        command_ref = result.get("command_ref")
        if not isinstance(command_ref, str) or not command_ref:
            raise ProtocolError("宿主命令登记结果无效")
        return command_ref

    def define_panel(
        self,
        key: str,
        title: str,
        nodes: tuple[dict[str, JsonValue], ...],
        *,
        description: str = "",
        data_callback: Callable[[], JsonValue | Awaitable[JsonValue]] | None = None,
    ) -> str:
        """
        向宿主提交一个声明式面板，界面由宿主渲染而不是插件注入脚本。

        面板中的设置节点必须在调用前通过 settings.define 定义；按钮只引用
        本插件已登记的命令名。数据回调留在本 Worker，只在宿主拉取时执行。

        :param key: 本插件命名空间内的面板标识
        :param title: 面板标题
        :param nodes: 受限节点树，类型与上限由宿主复核
        :param description: 面板说明
        :param data_callback: 可选的列表与任务数据回调，无参且返回 PanelData 结构
        :return: 宿主登记的面板引用
        :raises HostApiError: 未授权、节点越界或标识冲突时抛出
        """
        panel: dict[str, JsonValue] = {
            "key": key,
            "title": title,
            "description": description,
            "nodes": list(nodes),
        }
        if data_callback is not None:
            panel["data_callback_ref"] = self.register_callback(data_callback)
        result = self.host.call("panels.register", {"panels": [panel]})
        references = result.get("panel_refs")
        if not isinstance(references, list) or not references or not isinstance(references[0], str):
            raise ProtocolError("宿主面板登记结果无效")
        return references[0]

    def _invoke_callback(self, reference: str, arguments: dict[str, JsonValue]) -> JsonValue:
        """
        只调用本实例登记的函数，异步结果在回调线程内等待，不阻塞 RPC 接收线程。
        """
        with self._callback_lock:
            callback = self._callbacks.get(reference)
        if callback is None:
            raise ProtocolError("插件回调已经失效", "PLUGIN_CALLBACK_EXPIRED")
        result = callback(**arguments)
        if inspect.isawaitable(result):
            result = asyncio.run(self._await_result(result))
        return result

    @staticmethod
    async def _await_result(result: Awaitable[JsonValue]) -> JsonValue:
        return await result

    def on_load(self) -> None:
        """
        可覆写的加载钩子，默认没有注册或业务副作用。
        """

    def on_enable(self) -> None:
        """
        可覆写的启用钩子，默认不调用宿主服务。
        """

    def on_frontend_ready(self) -> None:
        """
        可覆写的可信前端就绪通知，不获得 DOM、Vue 或宿主 IPC 对象。
        """

    def on_disable(self) -> None:
        """
        可覆写的停止准备钩子，返回不代表全部后台工作已经停止。
        """

    def on_unload(self) -> None:
        """
        可覆写的卸载通知；宿主仍须实际回收 Job 和运行所有资源。
        """
