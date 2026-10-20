# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：第三方插件 API v2 的轻量入口，不导入宿主服务或桌面运行环境。
#
# 公开接口：
#   - class Plugin — 插件生命周期与本地命令声明。
#   - class HostClient — 经受信会话调用宿主白名单接口。
#   - class HostApiError — 宿主接口调用失败。
#   - class ProtocolError — 插件通信协议校验失败。
#   - JsonValue — 插件与宿主之间可传输的 JSON 值类型。
# ============================================================

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugin_api.plugin import HostApiError, HostClient, Plugin

__all__ = ["HostApiError", "HostClient", "JsonValue", "Plugin", "ProtocolError"]
