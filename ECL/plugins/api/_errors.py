# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为各平台插件运行边界提供统一错误类型，不暴露路径和认证材料。
#
# 公开接口：
#   - class PluginRuntimeError — 保留稳定业务错误码和可选原生错误值。
# ============================================================

from __future__ import annotations


class PluginRuntimeError(RuntimeError):
    """
    统一表示本地通信、进程身份和资源监管失败。
    """

    def __init__(self, message: str, error_code: str, native_error: int = 0) -> None:
        """
        保存不包含个人目录、请求正文或凭据的失败信息。

        :param message: 安全的业务说明
        :param error_code: 平台之间保持一致的错误类别
        :param native_error: 仅用于原生诊断的错误值，不作为身份
        """
        super().__init__(message)
        self.error_code: str = error_code
        self.native_error: int = native_error
