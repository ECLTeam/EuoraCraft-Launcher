# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按宿主提供的协作者装配 MC 能力适配器，条件与原 broker 就地构造一致。
#
# 公开接口：
#   - class DefaultMcAdapterFactory — McAdapterFactory 的 MC 侧默认实现。
# ============================================================

from __future__ import annotations

from pathlib import Path

from ECL.mc.adapters.game_install import RuntimeGameInstall
from ECL.mc.adapters.game_launch import RuntimeGameLaunch
from ECL.mc.adapters.game_queries import RuntimeGameCatalog
from ECL.mc.adapters.game_resources import RuntimeGameResources
from ECL.mc.adapters.instance_profiles import RuntimeInstanceProfiles
from ECL.mc.adapters.modpacks import RuntimeModpacks
from ECL.mc.adapters.resource_queries import RuntimeResourceCatalog
from ECL.plugins.run_store import PluginRunSession
from ECL.plugins.runtime.adapters import McAdapters


class DefaultMcAdapterFactory:
    """
    复刻原 broker 就地构造的依赖条件，逐个装配 MC 适配器。
    """

    def create(
        self,
        session: PluginRunSession,
        *,
        game_operations,
        resources,
        java,
        instance_profiles,
        operations,
        files,
        network,
        data_path: Path,
    ) -> McAdapters:
        """
        按协作者齐备情况装配适配器，缺失的成员保持为空。

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
        game_catalog = RuntimeGameCatalog(session, game_operations) if game_operations is not None else None
        game_install = (
            RuntimeGameInstall(session, game_operations, resources, operations)
            if game_operations is not None and resources is not None and operations is not None
            else None
        )
        game_launch = (
            RuntimeGameLaunch(session, game_operations, resources, operations, java)
            if game_operations is not None and resources is not None and operations is not None
            else None
        )
        resource_catalog = RuntimeResourceCatalog(session, game_operations) if game_operations is not None else None
        game_resources = (
            RuntimeGameResources(resources, game_operations, files, operations)
            if resources is not None and game_operations is not None and files is not None and operations is not None
            else None
        )
        modpacks = (
            RuntimeModpacks(session, game_operations, resources, files, operations, network, data_path)
            if game_operations is not None
            and resources is not None
            and files is not None
            and operations is not None
            and network is not None
            else None
        )
        profiles = (
            RuntimeInstanceProfiles(session, resources, instance_profiles, operations)
            if resources is not None and instance_profiles is not None and operations is not None
            else None
        )
        return McAdapters(
            game_catalog=game_catalog,
            game_install=game_install,
            game_launch=game_launch,
            game_resources=game_resources,
            instance_profiles=profiles,
            modpacks=modpacks,
            resource_catalog=resource_catalog,
        )
