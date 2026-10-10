# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：系统领域 IPC 处理器：运行信息、更新检测、日志与错误上报。
#
# 公开接口：
#   - class SystemHandlers — 处理启动器信息、严重错误、用户协议、日志导出和调试维护请求。
#       - launcher_errors_pending(body) -> dict[str, Any] — 返回尚未被前端确认呈现的严重错误。
#       - launcher_errors_ack(body) -> dict[str, Any] — 确认前端已经接收一批严重错误并释放其内存副本。
#       - system_ping(body) -> dict[str, Any] — 检查连接。
#       - system_memory(body) -> dict[str, Any] — 获取内存信息。
#       - launcher_info(body) -> dict[str, Any] — 获取启动器信息。
#       - launcher_check_update(body) -> dict[str, Any] — 按当前版本通道检查 GitHub Releases 是否有新版本。
#       - launcher_preload_connector(body) -> dict[str, Any] — 预热联机节点缓存，避免首次创建或加入房间等待网络。
#       - info_card_get(body) -> dict[str, Any] — 获取信息卡片。
#       - launcher_update_status(body) -> dict[str, Any] — 返回当前运行形态是否允许自动更新。
#       - launcher_update_download(body) -> dict[str, Any] — 下载当前通道最新版本的安装包并落盘替换计划。
#       - launcher_update_apply(body) -> dict[str, Any] — 拉起引导脚本完成替换，并请求当前启动器退出以重启。
#       - user_agreement_get(body) -> dict[str, Any] — 读取当前用户协议接受状态。
#       - user_agreement_save(body) -> dict[str, Any] — 保存用户已接受协议的本地状态。
#       - user_agreement_clear(body) -> dict[str, Any] — 清除用户协议接受状态，但保留匿名标识供再次确认。
#       - export_logs(body) -> dict[str, Any] — 将当前启动器日志打包为 ZIP 文件。
#       - debug_reset_launcher_data(body) -> dict[str, Any] — 还原启动器设置。
#       - debug_clear_plugins(body) -> dict[str, Any] — 清理插件数据。
#       - debug_devtools_open(body) -> dict[str, Any] — 打开 WebView 开发者工具（F12 调试窗口）。
#       - custom_download_start(body) -> ApiResponse — 提交自定义文件下载。
#       - custom_download_defaults(body) -> ApiResponse — 返回默认下载目录和 UA。
#       - custom_download_retry(body) -> ApiResponse — 重试本会话失败或取消的下载。
# ============================================================

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import psutil
from anyio import to_thread
from pydantic import ValidationError

from ECL.api.contracts import ApiResponse, failure, success
from ECL.host.app_update import AppUpdateError, UpdateApplier
from ECL.host.maintenance import schedule_debug_maintenance
from ECL.services.custom_downloads import CustomDownloadRequest
from ECL.services.updates import UpdateChecker

from .bridge import _FrontendState, _ipc_handler


class SystemHandlers(_FrontendState):
    """
    处理启动器信息、严重错误、用户协议、日志导出和调试维护请求。
    """

    async def launcher_errors_pending(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回尚未被前端确认呈现的严重错误。

        :param body: 必须为空的请求对象
        :return: 当前启动器会话内待呈现的错误事件列表
        """
        if body:
            return {"success": False, "message": "launcher_errors_pending 不接受参数", "errorCode": "INVALID_REQUEST"}
        with self._frontend_event_lock:
            pending = list(self._pending_error_presentations.values())
        return success(pending)

    async def launcher_errors_ack(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        确认前端已经接收一批严重错误并释放其内存副本。

        :param body: 包含一个或多个 ``error_ids`` 的请求对象
        :return: 已确认移除的错误数量
        """
        error_ids = body.get("error_ids")
        if (
            not isinstance(error_ids, list)
            or not error_ids
            or any(not isinstance(item, str) or not item for item in error_ids)
        ):
            return {"success": False, "message": "错误编号列表无效", "errorCode": "INVALID_REQUEST"}
        removed = 0
        with self._frontend_event_lock:
            for error_id in error_ids:
                if self._pending_error_presentations.pop(error_id, None) is not None:
                    removed += 1
        return success({"removed": removed})

    async def system_ping(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        检查连接。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": {"status": "ok", "message": "正常"}}

    @_ipc_handler("CUSTOM_DOWNLOAD_FAILED")
    async def custom_download_start(self, body: dict[str, Any]) -> ApiResponse:
        """
        校验并提交自定义下载，任务在独立线程运行。

        :param body: HTTP(S) 地址、保存方式、请求设置及覆盖意图
        :return: 新下载任务标识
        """
        try:
            request = await to_thread.run_sync(CustomDownloadRequest.model_validate, body)
        except ValidationError as exc:
            # 模型错误可能携带敏感请求头，直接转换为脱敏校验提示。
            return self._invalid_request(exc)
        if self.context.downloads is None:
            return failure("下载服务未初始化", "DOWNLOAD_UNAVAILABLE")
        return success(await to_thread.run_sync(self.context.downloads.submit, request))

    @_ipc_handler("CUSTOM_DOWNLOAD_DEFAULTS_FAILED")
    async def custom_download_defaults(self, body: dict[str, Any]) -> ApiResponse:
        """
        读取本次应用实际数据目录对应的下载初始设置，不创建目录。

        :param body: 不接受参数的请求体
        :return: 默认下载文件夹与 UA
        """
        if body:
            return failure("默认下载设置不接受参数", "INVALID_REQUEST")
        if self.context.downloads is None:
            return failure("下载服务未初始化", "DOWNLOAD_UNAVAILABLE")
        return success(await to_thread.run_sync(self.context.downloads.defaults))

    @_ipc_handler("CUSTOM_DOWNLOAD_RETRY_FAILED")
    async def custom_download_retry(self, body: dict[str, Any]) -> ApiResponse:
        """
        将本会话失败或取消的下载重新登记为新任务。

        :param body: 包含原 operation_id 的请求
        :return: 重试任务标识
        """
        operation_id = body.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return failure("任务标识无效", "INVALID_OPERATION")
        if self.context.downloads is None:
            return failure("下载服务未初始化", "DOWNLOAD_UNAVAILABLE")
        return success(await to_thread.run_sync(self.context.downloads.retry, operation_id))

    @_ipc_handler("SYSTEM_MEMORY_FAILED")
    async def system_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取内存信息。

        :param body: 前端传入的 IPC 请求参数
        """
        mem = psutil.virtual_memory()
        to_mb = 1 / (1024 * 1024)
        return {
            "success": True,
            "data": {
                "totalMb": round(mem.total * to_mb),
                "usedMb": round(mem.used * to_mb),
                "freeMb": round(mem.available * to_mb),
                "percentUsed": mem.percent,
            },
        }

    async def launcher_info(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取启动器信息及本次运行的主窗口模式。

        窗口模式是创建窗口时固定的快照，设置页保存新偏好后不会提前改变它。

        :param body: 前端传入的 IPC 请求参数
        :return: 版本、调试状态及主窗口模式信息
        """
        return {
            "success": True,
            "data": {
                "version": self.launcher.launcher_version or "",
                "version_type": self.launcher.launcher_version_type or "release",
                "debug": bool(self.launcher.debug),
                "active_window_chrome": self.launcher.active_window_chrome,
                "system_shadow_supported": sys.platform == "win32",
            },
        }

    @_ipc_handler("UPDATE_CHECK_FAILED")
    async def launcher_check_update(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        按当前版本通道检查 GitHub Releases 是否有新版本。

        :param body: 必须为空的请求对象
        :return: 包含检测状态、通道与最新版本信息的响应
        """
        if body:
            return {"success": False, "message": "launcher_check_update 不接受参数", "errorCode": "INVALID_REQUEST"}
        checker = UpdateChecker(
            self.http,
            current_version=self.launcher.launcher_version,
            version_type=self.launcher.launcher_version_type,
        )
        result = await to_thread.run_sync(checker.check)
        return success(
            {
                "status": result.status,
                "current_version": result.current_version,
                "channel": result.channel,
                "latest_version": result.latest_version,
                "latest_url": result.latest_url,
                "latest_notes": result.latest_notes,
                "message": result.message,
            }
        )

    @_ipc_handler("CONNECTOR_PRELOAD_FAILED")
    async def launcher_preload_connector(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        请求后端在后台预先读取节点列表，供旧版调用方继续使用。

        主窗口首次就绪时会自动读取；重复请求或前端重载不会再次发起网络任务，
        此命令只确认是否已开始读取，不等待网络返回。
        """
        if body:
            return {
                "success": False,
                "message": "launcher_preload_connector 不接受参数",
                "errorCode": "INVALID_REQUEST",
            }
        self.connector.start_node_preload()
        return success()

    async def info_card_get(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取信息卡片。

        :param body: 前端传入的 IPC 请求参数
        """
        data = await to_thread.run_sync(self.info_card.get_info_card)
        return {"success": True, "data": data}

    def _update_applier(self) -> UpdateApplier:
        # 构造启动器自动更新的执行器，携带当前运行环境与事件总线。
        return UpdateApplier(
            app_path=self.launcher.app_path,
            data_path=self.launcher.data_path,
            is_frozen=self.launcher.is_frozen,
            version_type=self.launcher.launcher_version_type,
            current_version=self.launcher.launcher_version,
            http=self.http,
            event_bus=self.events,
        )

    async def launcher_update_status(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回当前运行形态是否允许自动更新。

        :param body: 必须为空的请求对象
        :return: 包含 ``enabled`` 布尔值的响应
        """
        if body:
            return {"success": False, "message": "launcher_update_status 不接受参数", "errorCode": "INVALID_REQUEST"}
        return {"success": True, "data": {"enabled": self._update_applier().enabled()}}

    @_ipc_handler("APP_UPDATE_DOWNLOAD_FAILED")
    async def launcher_update_download(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        下载当前通道最新版本的安装包并落盘替换计划。

        :param body: 必须为空的请求对象
        :return: 包含目标版本与二进制路径的响应
        """
        if body:
            return {"success": False, "message": "launcher_update_download 不接受参数", "errorCode": "INVALID_REQUEST"}
        applier = self._update_applier()
        if not applier.enabled():
            return {"success": False, "message": "当前通道不支持自动更新", "errorCode": "UPDATE_NOT_SUPPORTED"}
        checker = UpdateChecker(
            self.http,
            current_version=self.launcher.launcher_version,
            version_type=self.launcher.launcher_version_type,
        )
        release = await to_thread.run_sync(checker.latest_release)
        if release is None:
            return {"success": False, "message": "暂时没有可用的更新", "errorCode": "UPDATE_NONE"}
        try:
            staged, new_binary = await to_thread.run_sync(applier.stage, release)
        except AppUpdateError as exc:
            self.logger.warning("自动更新下载失败: %s", exc)
            return {"success": False, "message": str(exc), "errorCode": exc.error_code}
        return success(
            {
                "version": staged.version,
                "target": str(staged.target),
                "new_binary": str(new_binary),
                "downloaded": True,
            }
        )

    async def launcher_update_apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        拉起引导脚本完成替换，并请求当前启动器退出以重启。

        :param body: 必须为空的请求对象
        :return: 是否已触发重启流程的成功响应
        """
        if body:
            return {"success": False, "message": "launcher_update_apply 不接受参数", "errorCode": "INVALID_REQUEST"}
        applier = self._update_applier()
        staged = applier.load_pending()
        if staged is None:
            return {"success": False, "message": "没有待应用的更新", "errorCode": "UPDATE_NO_PENDING"}
        applier.apply(staged)
        self.events.emit("launcher:request_restart", {})
        return success({"version": staged.version, "restarting": True})

    async def user_agreement_get(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        读取当前用户协议接受状态。

        :param body: 必须为空的请求对象
        :return: 接受状态和本地匿名标识
        """
        if body:
            return {"success": False, "message": "user_agreement_get 不接受参数", "errorCode": "INVALID_REQUEST"}
        state = self.config.get_config("user_agreement") or {}
        return {
            "success": True,
            "data": {"accepted": bool(state.get("accepted", False)), "uuid": str(state.get("uuid") or "")},
        }

    async def user_agreement_save(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        保存用户已接受协议的本地状态。

        :param body: 可选包含既有匿名 ``uuid`` 的请求数据
        :return: 保存后的接受状态和匿名标识
        """
        existing = self.config.get_config("user_agreement") or {}
        agreement_id = body.get("uuid") or existing.get("uuid") or uuid4().hex
        if not isinstance(agreement_id, str):
            return {"success": False, "message": "协议标识格式无效", "errorCode": "INVALID_REQUEST"}
        state = {"accepted": True, "uuid": agreement_id}
        self.config.save_config("user_agreement", state)
        return {"success": True, "data": state}

    async def user_agreement_clear(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        清除用户协议接受状态，但保留匿名标识供再次确认。

        :param body: 必须为空的请求对象
        :return: 空的成功响应
        """
        if body:
            return {"success": False, "message": "user_agreement_clear 不接受参数", "errorCode": "INVALID_REQUEST"}
        existing = self.config.get_config("user_agreement") or {}
        self.config.save_config("user_agreement", {"accepted": False, "uuid": str(existing.get("uuid") or "")})
        return {"success": True}

    @_ipc_handler("EXPORT_LOGS_FAILED")
    async def export_logs(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        将当前启动器日志打包为 ZIP 文件。

        :param body: 可选包含目标 ``output_path`` 的请求数据
        :return: 导出文件的绝对路径
        """
        output_path = body.get("output_path")
        if output_path is None:
            output_path = self.data_path / "exports" / "EuoraCraft-logs.zip"
        elif not isinstance(output_path, str) or not output_path.strip() or "\0" in output_path:
            return {"success": False, "message": "日志导出路径无效", "errorCode": "INVALID_PATH"}
        target = Path(output_path).expanduser().resolve(strict=False)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        log_dir = self.data_path / "logs"

        def write_archive() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                if log_dir.is_dir():
                    for path in sorted(log_dir.iterdir()):
                        if path.is_file():
                            archive.write(path, arcname=path.name)
            temporary.replace(target)

        try:
            await to_thread.run_sync(write_archive)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"success": True, "data": {"path": str(target)}}

    def _schedule_debug_maintenance(self, action: str) -> dict[str, Any]:
        # 在调试模式下排队一项启动器数据维护操作。
        if not bool(self.launcher.debug):
            return {"success": False, "message": "此操作仅在启动器调试模式下可用", "errorCode": "DEBUG_MODE_REQUIRED"}
        result = schedule_debug_maintenance(self.data_path, action)
        return success(
            {
                "action": result.action,
                "restart_required": result.restart_required,
                "targets": list(result.targets),
            }
        )

    @_ipc_handler("DEBUG_MAINTENANCE_FAILED")
    async def debug_reset_launcher_data(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        还原启动器设置，并保留已登录账户。

        :param body: 前端传入的 IPC 请求参数
        """
        return self._schedule_debug_maintenance("reset_launcher_data")

    @_ipc_handler("DEBUG_MAINTENANCE_FAILED")
    async def debug_clear_plugins(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        清理插件数据。

        :param body: 前端传入的 IPC 请求参数
        """
        return self._schedule_debug_maintenance("clear_plugins")

    async def debug_devtools_open(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        打开 WebView 开发者工具（F12 调试窗口）。

        :param body: 必须为空的请求对象
        :return: 包含 ``open`` 布尔值的响应
        """
        if self._webview is None:
            return {"success": False, "message": "窗口尚未就绪", "errorCode": "WEBVIEW_NOT_READY"}
        self._webview.open_devtools()
        return success({"open": True})
