# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件扩展点包。
# ============================================================

from ECL.plugins.auth_providers import AuthField, AuthProvider, AuthProviderRegistry
from ECL.plugins.crash_extensions import CrashAnalysisContext, CrashAnalysisExtensionRegistry
from ECL.plugins.framework import PluginAction, PluginActionResult, PluginManager
from ECL.plugins.instance_compat import (
    ExternalInstanceMetadata,
    InstanceCompatibilityContext,
    InstanceCompatibilityRegistry,
)
from ECL.plugins.launch_hooks import LaunchContext, LaunchHookRegistry
from ECL.plugins.network import PluginHttpError, PluginHttpResponse
from ECL.plugins.plugin import Plugin

__all__ = [
    "AuthField",
    "AuthProvider",
    "AuthProviderRegistry",
    "CrashAnalysisContext",
    "CrashAnalysisExtensionRegistry",
    "ExternalInstanceMetadata",
    "InstanceCompatibilityContext",
    "InstanceCompatibilityRegistry",
    "LaunchContext",
    "LaunchHookRegistry",
    "Plugin",
    "PluginAction",
    "PluginActionResult",
    "PluginHttpError",
    "PluginHttpResponse",
    "PluginManager",
]
