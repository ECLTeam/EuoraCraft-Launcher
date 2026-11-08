# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义联机插件可向界面返回的领域错误。
#
# 公开接口：
#   - class ConnectorError — 插件业务接口。
#   - class ConnectorNotAvailableError — 插件业务接口。
# ============================================================

from __future__ import annotations


class ConnectorError(RuntimeError):
    """
    联机插件可以安全返回给自身界面的业务失败。
    """

    error_code = "ONLINEGAME_FAILED"


class ConnectorNotAvailableError(ConnectorError):
    """
    所需平台运行能力当前不可用。
    """

    error_code = "ONLINEGAME_UNAVAILABLE"
