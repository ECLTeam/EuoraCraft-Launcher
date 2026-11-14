# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：声明 MC 适配器的聚合与工厂协议，内核侧不持有任何 MC 实现。
#
# 公开接口：
#   - class McAdapters — 一次插件运行可用的 MC 能力适配器聚合。
#   - class McAdapterFactory — 由 MC 侧实现、内核仅持有引用的装配协议。
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ECL.plugins.run_store import PluginRunSession

if TYPE_CHECKING:
    from ECL.mc.adapters.game_install import RuntimeGameInstall
    from ECL.mc.adapters.game_launch import RuntimeGameLaunch
    from ECL.mc.adapters.game_queries import RuntimeGameCatalog
    from ECL.mc.adapters.game_resources import RuntimeGameResources
    from ECL.mc.adapters.instance_profiles import RuntimeInstanceProfiles
    from ECL.mc.adapters.java_api import RuntimeJava
    from ECL.mc.adapters.modpacks import RuntimeModpacks
    from ECL.mc.adapters.resource_queries import RuntimeResourceCatalog
    from ECL.mc.adapters.resources import RuntimeResources
    from ECL.plugins.runtime.file_api import RuntimeFiles
    from ECL.plugins.runtime.network_api import RuntimeNetwork
    from ECL.plugins.runtime.operation_adapters import RuntimeOperations
    from ECL.services.game.instance_profile_transactions import InstanceProfileTransactions
    from ECL.services.game.plugin_games import PluginGameCoordinator


class McAdapters:
    """
    一次插件运行可用的 MC 能力适配器聚合；成员按协作者齐备情况可能为空。
    """

    __slots__ = (
        "game_catalog",
        "game_install",
        "game_launch",
        "game_resources",
        "instance_profiles",
        "modpacks",
        "resource_catalog",
    )

    def __init__(
        self,
        *,
        game_catalog: RuntimeGameCatalog | None = None,
        game_install: RuntimeGameInstall | None = None,
        game_launch: RuntimeGameLaunch | None = None,
        game_resources: RuntimeGameResources | None = None,
        instance_profiles: RuntimeInstanceProfiles | None = None,
        modpacks: RuntimeModpacks | None = None,
        resource_catalog: RuntimeResourceCatalog | None = None,
    ) -> None:
        """
        聚合 MC 侧工厂装配的适配器，成员缺失时对应插件方法返回能力不可用。

        :param game_catalog: 游戏与加载器目录查询
        :param game_install: 游戏内容安装
        :param game_launch: 启动链路适配器
        :param game_resources: 游戏资源导入导出
        :param instance_profiles: 实例资料事务
        :param modpacks: 整合包安装与查询
        :param resource_catalog: 游戏资源目录查询
        """
        self.game_catalog = game_catalog
        self.game_install = game_install
        self.game_launch = game_launch
        self.game_resources = game_resources
        self.instance_profiles = instance_profiles
        self.modpacks = modpacks
        self.resource_catalog = resource_catalog


class McAdapterFactory(Protocol):
    """
    在插件运行会话建立后装配 MC 能力适配器。

    内核不实例化也不导入 MC 实现，仅经该协议持有装配入口；
    构造参数与内核服务就绪顺序一致，实现方不得重排。
    """

    def create(
        self,
        session: PluginRunSession,
        *,
        game_operations: PluginGameCoordinator | None,
        resources: RuntimeResources | None,
        java: RuntimeJava | None,
        instance_profiles: InstanceProfileTransactions | None,
        operations: RuntimeOperations | None,
        files: RuntimeFiles | None,
        network: RuntimeNetwork | None,
        data_path: Path,
    ) -> McAdapters:
        """
        按当前运行的协作者装配 MC 适配器。

        :param session: 当前插件运行的唯一授权会话
        :param game_operations: 应用唯一游戏业务入口
        :param resources: 宿主装配的共享实例资源表
        :param java: 宿主装配的 Java 查询及探测资源
        :param instance_profiles: 应用唯一共享实例资料事务
        :param operations: 内核任务控制适配器
        :param files: 内核文件 API 适配器
        :param network: 内核只读网络适配器
        :param data_path: 宿主数据根目录
        :return: MC 适配器聚合
        """
        ...
