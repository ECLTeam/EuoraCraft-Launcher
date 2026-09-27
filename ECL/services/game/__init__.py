# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：游戏领域服务门面：GameService 聚合各协调器。
#
# 公开接口：
#   - class GameService — 面向 IPC 边界公开的统一游戏服务门面。
# ============================================================

from .base import GameServiceError, VersionScanError
from .catalog import CatalogCoordinator
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
    CatalogCoordinator,
):
    """
    面向 IPC 边界公开的统一游戏服务门面。

    通过多重继承聚合各领域协调器，将实例、安装、启动、扫描与资源管理的
    能力合并为单一的 ``GameService`` 入口，内部共享基类 ``_GameState``
    提供的运行状态与依赖。
    """


__all__ = ["GameService", "GameServiceError", "VersionScanError"]
