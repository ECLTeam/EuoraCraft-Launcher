# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：第三方插件 API v2 的轻量入口，不导入宿主服务或桌面运行环境。
# ============================================================

from ecl_plugin_api.contracts import JsonValue, ProtocolError

__all__ = ["JsonValue", "ProtocolError"]
