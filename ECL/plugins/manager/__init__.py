# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件管理器门面：组合发现/注册表/生命周期/存储。
#
# 公开接口：
#   - class PluginManager — 面向插件的统一门面，组合若干职责单一的 Mixin 混合类能力。
# ============================================================

from .contracts import PluginAction, PluginActionResult, PluginCommandError
from .discovery import PluginDiscovery
from .lifecycle import PluginLifecycle
from .packages import PluginPackages
from .registry import PluginRegistry
from .storage import PluginStorage


class PluginManager(PluginRegistry, PluginLifecycle, PluginStorage, PluginDiscovery, PluginPackages):
    """
    面向插件的统一门面，组合若干职责单一的 Mixin 混合类能力。
    """


__all__ = ["PluginAction", "PluginActionResult", "PluginCommandError", "PluginManager"]
