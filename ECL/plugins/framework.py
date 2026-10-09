# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：读取 plugin.json，设置插件权限，并管理插件的启用和关闭。
# ============================================================

from ECL.plugins.manager import PluginAction, PluginActionResult, PluginCommandError, PluginManager

__all__ = ["PluginAction", "PluginActionResult", "PluginCommandError", "PluginManager"]
