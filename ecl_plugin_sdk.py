# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为已发布归档插件保留 ecl_plugin_sdk 导入路径。
#
# 公开接口：
#   - class Plugin — 主进程中的兼容命令 SDK。
# ============================================================

from __future__ import annotations

from ECL.plugins.command_sdk import Plugin

__all__ = ["Plugin"]
