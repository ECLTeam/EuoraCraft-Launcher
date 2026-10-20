# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：处理前端对插件列表、启用状态、设置、路由和插槽的请求。
#
# 公开接口：
#   - class PluginHandlers — 处理插件的启用、禁用、路由、插槽和命令调用请求。
#       - plugin_permission_resources(body, webview_window) — 只为原生主窗口发现可审阅实例资源。
#       - plugin_permissions_get/update — 查询和保存本安装的内容及精确范围授权。
#       - plugin_runtime_info — 查询实际 Worker 和清理状态。
#       - plugin_list(body) -> dict[str, Any] — 获取插件列表。
#       - plugin_info(body) -> dict[str, Any] — 获取插件信息。
#       - plugin_enable(body) -> dict[str, Any] — 启用插件。
#       - plugin_disable(body) -> dict[str, Any] — 禁用插件。
#       - plugin_unload(body) -> dict[str, Any] — 卸载并删除用户插件。
#       - plugin_reload(body) -> dict[str, Any] — 重新加载插件。
#       - plugin_install(body) -> dict[str, Any] — 安装插件。
#       - plugin_package_inspect(body) -> dict[str, Any] — 只读预检本地归档及当前目标依赖。
#       - plugin_get_routes(body) -> dict[str, Any] — 获取插件路由。
#       - plugin_get_slots(body) -> dict[str, Any] — 获取插件插槽。
#       - plugin_get_vue_slots(body) -> dict[str, Any] — 获取插件 Vue 插槽。
#       - plugin_get_vue_components(body) -> dict[str, Any] — 获取插件 Vue 组件。
#       - plugin_call_command(body) -> dict[str, Any] — 调用插件命令。
#       - plugin_get_settings(body) -> dict[str, Any] — 获取插件设置。
#       - plugin_update_setting(body) -> dict[str, Any] — 更新插件设置。
#       - plugin_notify_sidebar_state(body) -> dict[str, Any] — 通知插件侧栏的折叠状态。
# ============================================================

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from time import monotonic
from typing import Any
from uuid import uuid4

from ECL.api.bridge import WebviewWindow
from ECL.api.contracts import failure
from ECL.api.plugin_models import PluginPermissionQuery, PluginPermissionResources, PluginPermissionUpdate
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.framework import PluginActionResult, PluginCommandError
from ECL.plugins.grant_models import PermissionManagementCaller
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.package_archive import PluginPackageError
from ECL.utils.instance_reading import InstanceReadError

from .bridge import _FrontendState


class PluginHandlers(_FrontendState):
    """
    处理插件的启用、禁用、路由、插槽和命令调用请求。
    """

    def _permission_caller(self, webview_window: WebviewWindow | None) -> PermissionManagementCaller:
        """
        只接受原生注入且已登记的主窗口，未知入口和客户端字段都不能获得管理资格。
        """
        if (
            webview_window is None
            or webview_window.label() != "main"
            or self._window_metadata.get("main", {}).get("windowType") != "main"
        ):
            raise PluginGrantError("此入口不能管理插件授权", "PLUGIN_MANAGEMENT_CALLER_DENIED")
        return PermissionManagementCaller.main_webview

    async def plugin_permissions_get(
        self, body: dict[str, Any], webview_window: WebviewWindow = None
    ) -> dict[str, Any]:
        """
        向可信主窗口返回安装权限，查询不会自动允许或执行插件。

        :param body: 已安装插件身份
        :param webview_window: 原生调用窗口，不来自请求体
        :return: 当前授权视图
        """
        try:
            self._permission_caller(webview_window)
            query = PluginPermissionQuery.model_validate(body)
            service = self.plugins.worker_service
            name = service.name_for_installation(query.installation_id)
            return {"success": True, "data": await asyncio.to_thread(service.permissions, name)}
        except (PluginGrantError, ProtocolError, InstanceReadError) as exc:
            return failure(str(exc), exc.error_code)

    async def plugin_permissions_update(
        self, body: dict[str, Any], webview_window: WebviewWindow = None
    ) -> dict[str, Any]:
        """
        保存主窗口审阅决定并返回独立的运行清理状态，不把落盘成功当成进程停止。

        :param body: 安装身份、内容、revision 与实际决定
        :param webview_window: 原生调用窗口
        :return: 持久化结果和清理状态
        """
        try:
            caller = self._permission_caller(webview_window)
            request = PluginPermissionUpdate.model_validate(body)
            service = self.plugins.worker_service
            name = service.name_for_installation(request.installation_id)
            result = await asyncio.to_thread(
                service.update_permissions,
                name,
                request.content_digest,
                request.expected_revision,
                tuple(request.decisions),
                caller,
                request.resource_revision,
            )
            self.plugins.events.emit("plugin:status_changed", {"plugin": name})
            return {"success": True, "data": result}
        except (PluginGrantError, ProtocolError, InstanceReadError) as exc:
            return failure(str(exc), exc.error_code)

    async def plugin_permission_resources(
        self, body: dict[str, Any], webview_window: WebviewWindow = None
    ) -> dict[str, Any]:
        """
        向原生登记的主窗口返回资源候选，发现不会替用户授予权限。

        :param body: 安装、内容与受限候选查询
        :param webview_window: 原生注入的调用窗口
        :return: 不含完整路径的候选分页
        """
        try:
            self._permission_caller(webview_window)
            request = PluginPermissionResources.model_validate(body)
            service = self.plugins.worker_service
            name = service.name_for_installation(request.installation_id)
            result = await asyncio.to_thread(
                service.permission_resources,
                name,
                request.content_digest,
                request.parent_ref,
                request.search,
                request.cursor,
                request.limit,
            )
            return {"success": True, "data": result}
        except (PluginGrantError, ProtocolError, InstanceReadError) as exc:
            return failure(str(exc), exc.error_code)

    async def plugin_runtime_info(self, body: dict[str, Any], webview_window: WebviewWindow = None) -> dict[str, Any]:
        """
        查询本安装实际 Worker 状态，只提供给可信主界面。

        :param body: 当前安装身份
        :param webview_window: 原生调用窗口
        :return: 当前运行及清理状态
        """
        try:
            self._permission_caller(webview_window)
            query = PluginPermissionQuery.model_validate(body)
            service = self.plugins.worker_service
            return {
                "success": True,
                "data": await asyncio.to_thread(
                    service.runtime_info, service.name_for_installation(query.installation_id)
                ),
            }
        except (PluginGrantError, ProtocolError) as exc:
            return failure(str(exc), exc.error_code)

    async def _run_plugin_lifecycle(self, name: str, action: Callable[[str], PluginActionResult]) -> PluginActionResult:
        """
        安装包操作需要读取和校验磁盘文件，因此放到工作线程中执行。
        """
        return await asyncio.to_thread(action, name)

    async def plugin_list(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件列表。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": await asyncio.to_thread(self.plugins.list_plugins)}

    async def plugin_info(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件信息。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        metadata = self.plugins.get_plugin_metadata(plugin_name)
        if metadata is None:
            return {"success": False, "message": f"插件不存在: {plugin_name}", "errorCode": "PLUGIN_NOT_FOUND"}
        return {"success": True, "data": metadata}

    async def plugin_enable(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        启用插件。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        result = await self._run_plugin_lifecycle(plugin_name, self.plugins.enable)
        if not result.success:
            return failure(result.message or f"启用插件失败: {plugin_name}", "PLUGIN_ENABLE_FAILED")
        return {"success": True}

    async def plugin_disable(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        禁用插件。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        result = await self._run_plugin_lifecycle(plugin_name, self.plugins.disable)
        if not result.success:
            return failure(result.message or f"禁用插件失败: {plugin_name}", "PLUGIN_DISABLE_FAILED")
        return {"success": True}

    async def plugin_unload(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        卸载并删除用户插件。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        result = await self._run_plugin_lifecycle(plugin_name, self.plugins.uninstall)
        if not result.success:
            return failure(result.message or f"卸载插件失败: {plugin_name}", "PLUGIN_UNLOAD_FAILED")
        return {"success": True}

    async def plugin_reload(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        重新加载插件。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        result = await self._run_plugin_lifecycle(plugin_name, self.plugins.reload)
        if not result.success:
            return failure(result.message or f"重载插件失败: {plugin_name}", "PLUGIN_RELOAD_FAILED")
        return {"success": True}

    async def plugin_install(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        安装插件。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_path = body.get("plugin_path")
        if not isinstance(plugin_path, str) or not plugin_path.strip():
            return failure("插件路径无效", "PLUGIN_INSTALL_FAILED")
        if plugin_path.lower().endswith(".eclplugin"):
            result = await asyncio.to_thread(
                self.plugins.install,
                plugin_path,
                confirm_unverified_source=body.get("confirm_unverified_source") is True,
                allow_network=body.get("allow_network") is True,
            )
        else:
            result = await asyncio.to_thread(
                self.plugins.install, plugin_path, allow_network=body.get("allow_network") is True
            )
        if not result.success:
            return failure(result.message or "安装插件失败", "PLUGIN_INSTALL_FAILED")
        return {"success": True, "data": {"status": result.status, "message": result.message}}

    async def plugin_package_inspect(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        在用户确认安装前校验归档并返回当前目标的来源与依赖摘要。

        :param body: 包含本地 `.eclplugin` 路径的 IPC 请求
        :return: 可供确认界面展示的预检信息
        """
        plugin_path = body.get("plugin_path")
        if not isinstance(plugin_path, str) or not plugin_path.strip():
            return failure("插件包路径无效", "PLUGIN_PACKAGE_INSPECT_FAILED")
        try:
            preflight = await asyncio.to_thread(self.plugins.inspect_package, plugin_path)
        except (OSError, PluginPackageError, PluginDependencyError) as exc:
            return failure(str(exc), "PLUGIN_PACKAGE_INSPECT_FAILED")
        return {"success": True, "data": asdict(preflight)}

    async def plugin_get_routes(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件路由。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.plugins.get_routes()}

    async def plugin_get_slots(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件插槽。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.plugins.get_slots()}

    async def plugin_get_vue_slots(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件 Vue 插槽。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.plugins.get_vue_slots()}

    async def plugin_get_vue_components(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件 Vue 组件。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.plugins.get_vue_components()}

    async def plugin_call_command(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        调用插件命令。

        :param body: 前端传入的 IPC 请求参数
        """
        command = body.get("command")
        try:
            result = await asyncio.to_thread(self.plugins.call_command, command, body.get("params", {}))
        except PluginCommandError as exc:
            return {"success": False, "message": str(exc), "errorCode": "PLUGIN_COMMAND_FAILED"}
        return {"success": True, "data": result}

    async def plugin_get_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件设置。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        return {"success": True, "data": await asyncio.to_thread(self.plugins.get_settings, plugin_name)}

    async def plugin_update_setting(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        更新插件设置。

        :param body: 前端传入的 IPC 请求参数
        """
        plugin_name = body.get("plugin_name")
        service = self.plugins.worker_service
        if service is not None and self.plugins.get_plugin(plugin_name) is None:
            try:
                result = await asyncio.to_thread(
                    service.broker(plugin_name).handle,
                    RpcRequest(
                        "settings.set",
                        {
                            "values": {body.get("key"): body.get("value")},
                            "expected_revision": body.get("expected_revision"),
                        },
                        monotonic() + 5,
                        uuid4().hex,
                    ),
                )
                return {"success": True, "data": result}
            except (PluginGrantError, ProtocolError) as exc:
                return failure(str(exc), exc.error_code)
        result = self.plugins.update_setting(plugin_name, body.get("key"), body.get("value"))
        return {"success": True} if result.success else failure(result.message, "PLUGIN_SETTING_UPDATE_FAILED")

    async def plugin_notify_sidebar_state(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        通知插件侧栏的折叠状态。

        :param body: 前端传入的 IPC 请求参数
        """
        collapsed = body.get("collapsed")
        if not isinstance(collapsed, bool):
            return failure("侧栏状态必须是布尔值", "INVALID_SIDEBAR_STATE")
        self.plugins.set_sidebar_state(collapsed)
        return {"success": True}
