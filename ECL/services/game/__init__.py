# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将实例、安装、启动和资源管理等游戏操作集中到 GameService。
#
# 公开接口：
#   - class GameService — 提供实例、安装、启动、扫描和资源管理操作。
# ============================================================

from .base import GameServiceError, VersionScanError
from .install import InstallCoordinator
from .instance_options import InstanceOptionsCoordinator
from .launch import LaunchCoordinator
from .modpack import ModpackCoordinator
from .mods import ModCoordinator
from .profiles import ProfileCoordinator
from .resources import ResourceCoordinator
from .scan import ScanCoordinator
from .schematics import SchematicCoordinator
from .screenshots import ScreenshotCoordinator
from .servers import ServerCoordinator
from .workspace import WorkspaceCoordinator
from .worlds import WorldCoordinator


class GameService(
    ProfileCoordinator,
    WorkspaceCoordinator,
    WorldCoordinator,
    InstanceOptionsCoordinator,
    ScreenshotCoordinator,
    ServerCoordinator,
    ResourceCoordinator,
    ModpackCoordinator,
    ModCoordinator,
    SchematicCoordinator,
    LaunchCoordinator,
    InstallCoordinator,
    ScanCoordinator,
):
    """
    提供实例、安装、启动、扫描和资源管理操作。

    各项操作由对应的协调器实现；它们通过 ``_GameState`` 共享运行状态和服务。
    """


__all__ = ["GameService", "GameServiceError", "VersionScanError"]
