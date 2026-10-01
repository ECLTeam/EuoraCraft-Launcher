# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：设置领域 IPC 处理器：配置读写与 Java 扫描。
#
# 公开接口：
#   - class SettingsHandlers — 提供启动器设置和 Java 运行时查询的正式 IPC 边界。
#       - settings_get(body) -> ApiResponse — 按单个分区、多个分区或完整配置读取启动器设置。
#       - settings_set(body) -> ApiResponse — 校验并全量保存一个配置分区。
#       - game_java_scan(body) -> ApiResponse — 扫描系统和用户配置路径中的 Java 运行时。
# ============================================================

from __future__ import annotations

from typing import Any

from anyio import to_thread

from ECL.api.contracts import ApiResponse, success
from ECL.api.models import JavaScanRequest, SettingsQuery, SettingsUpdate

from .bridge import _FrontendState, _ipc_handler, _validate_body


class SettingsHandlers(_FrontendState):
    """
    提供启动器设置和 Java 运行时查询的正式 IPC 边界。
    """

    async def settings_get(self, body: dict[str, Any]) -> ApiResponse:
        """
        按单个分区、多个分区或完整配置读取启动器设置。

        :param body: 符合 ``SettingsQuery`` 的请求数据
        :return: 请求范围内的配置数据
        """
        request, invalid = _validate_body(SettingsQuery, body)
        if invalid is not None:
            return invalid
        config = self._get_effective_config()
        if request.section is not None:
            return success(config.get(request.section))
        if request.sections is not None:
            return success({section: config.get(section) for section in dict.fromkeys(request.sections)})
        return success(config)

    async def settings_set(self, body: dict[str, Any]) -> ApiResponse:
        """
        校验并全量保存一个配置分区。

        :param body: 符合 ``SettingsUpdate`` 的请求数据
        :return: 空的成功响应或稳定校验错误
        """
        request, invalid = _validate_body(SettingsUpdate, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self.config.save_config, request.section, request.data)
        return success()

    @_ipc_handler("JAVA_SCAN_FAILED")
    async def game_java_scan(self, body: dict[str, Any]) -> ApiResponse:
        """
        扫描系统和用户配置路径中的 Java 运行时。

        :param body: 可选包含 ``paths`` 的请求数据
        :return: 可用于启动 Minecraft 的 Java 安装列表
        """
        request, invalid = _validate_body(JavaScanRequest, body)
        if invalid is not None:
            return invalid
        configured_java = (self._get_effective_config().get("game") or {}).get("java_path")
        requested_paths = request.paths
        if requested_paths is None:
            requested_paths = [configured_java] if isinstance(configured_java, str) and configured_java.strip() else []
        installations = await to_thread.run_sync(self.game.scan_java, [str(path) for path in requested_paths])
        return success(installations)


__all__ = ["SettingsHandlers"]
