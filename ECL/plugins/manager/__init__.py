# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：集中提供插件查找、注册、启用、卸载和存储操作。
#
# 公开接口：
#   - class PluginManager — 提供插件查找、注册、启用、卸载和存储操作。
# ============================================================

from .contracts import PluginAction, PluginActionResult, PluginCommandError
from .discovery import PluginDiscovery
from .lifecycle import PluginLifecycle
from .packages import PluginPackages
from .registry import PluginRegistry
from .storage import PluginStorage


class PluginManager(PluginRegistry, PluginLifecycle, PluginStorage, PluginDiscovery, PluginPackages):
    """
    提供插件查找、注册、启用、卸载和存储操作。
    """


__all__ = ["PluginAction", "PluginActionResult", "PluginCommandError", "PluginManager"]
