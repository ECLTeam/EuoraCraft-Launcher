# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件管理器内部共享基类与工具。
# ============================================================

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

import httpx

from ECL.events import EventBus
from ECL.plugins.auth_providers import AuthProviderRegistry
from ECL.plugins.connector import ConnectorExtensionRegistry
from ECL.plugins.crash_extensions import CrashAnalysisExtensionRegistry
from ECL.plugins.dependencies import DependencyResolution
from ECL.plugins.instance_compat import InstanceCompatibilityRegistry
from ECL.plugins.launch_hooks import LaunchHookRegistry
from ECL.plugins.permissions import PermissionManager
from ECL.plugins.plugin import Plugin
from ECL.utils import get_logger

from .install_transaction import recover_plugin_install_transactions

if TYPE_CHECKING:
    from ECL.plugins.package_activation import PluginPackageActivationStore


class _PluginState:
    """
    保存插件发现、生命周期与扩展点注册所共享的状态。

    该内部基类以 Mixin 形式经组合被 ``PluginManager`` 继承复用，不作为第二套公开插件 API 暴露。
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        processes: Any = None,
        instance_compatibility: InstanceCompatibilityRegistry | None = None,
        connector_extensions: ConnectorExtensionRegistry | None = None,
        launch_hooks: LaunchHookRegistry | None = None,
        http_client: httpx.Client | None = None,
        auth_providers: AuthProviderRegistry | None = None,
        crash_extensions: CrashAnalysisExtensionRegistry | None = None,
    ):
        """
        创建相互隔离的插件状态与命令执行器。

        :param event_bus: 当前应用上下文拥有的事件总线
        :param processes: 面向插件的通用子进程注册服务；None 表示当前环境未提供该能力
        :param instance_compatibility: 与游戏服务共享的实例兼容提供者注册表
        :param connector_extensions: 与联机服务共享的扩展协议注册表
        :param launch_hooks: 与游戏服务共享的启动钩子注册表
        :param http_client: 应用共享 HTTP 客户端，供插件的受控网络请求使用
        :param auth_providers: 与账户服务共享的自定义认证提供方注册表
        :param crash_extensions: 与游戏服务共享的崩溃分析富化注册表
        """
        self.logger = get_logger("PluginManager")
        self.events = event_bus or EventBus()
        self.processes = processes  # 插件可经 framework.processes 启动子进程实例
        self.instance_compatibility = instance_compatibility or InstanceCompatibilityRegistry()
        self.connector_extensions = connector_extensions or ConnectorExtensionRegistry()
        self.launch_hooks = launch_hooks or LaunchHookRegistry()
        self.http_client = http_client
        self.auth_providers = auth_providers or AuthProviderRegistry()
        self.crash_extensions = crash_extensions or CrashAnalysisExtensionRegistry()
        self._command_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="plugin_cmd"
        )  # 插件命令执行的线程池
        self._plugins: dict[str, Plugin] = {}  # name → Plugin 实例
        self._status: dict[str, str] = {}  # name → unloaded | loaded | enabled | disabled
        self._routes: list[dict[str, str]] = []  # 所有插件注册的路由
        # 插件配置值，key 为 "插件名.设置键"
        self._config_values: dict[str, Any] = {}
        # 插件配置文件的路径映射
        self._config_paths: dict[str, Path] = {}
        # 同一插件可以向一个插槽追加多个条目；带 key 的 HTML 和同名 Vue 组件会原位更新
        self._slots: dict[str, list[dict[str, str]]] = {}
        self._vue_slots: dict[str, list[dict[str, str]]] = {}
        # Vue 组件路由：与 _routes 平行的独立列表
        self._vue_routes: list[dict[str, Any]] = []
        # 已注册的 Vue 组件（去重），component_name → {plugin, template, script, style}
        self._vue_components: dict[str, dict[str, Any]] = {}
        self._event_handlers_registered = False  # 框架自身的事件处理器是否已登记
        self._dependency_resolution: DependencyResolution = DependencyResolution()  # 插件依赖解析结果
        self._permission_manager = PermissionManager()  # 插件权限管理器
        # 被禁用的插件名集合，持久化到 plugin_state.json
        self._disabled_plugins: set[str] = set()
        self._plugin_state_path: Path | None = None  # plugin_state.json 的路径
        # 候选插件映射，用于在启用被禁用的插件时按需加载
        self._candidate_map: dict[str, dict[str, Any]] = {}
        self._package_store: PluginPackageActivationStore | None = None
        self._package_entries: dict[str, dict[str, Any]] = {}
        self._package_conflicts: set[str] = set()
        self._package_lock = RLock()
        # 插件实例化/启用失败的详细错误信息，供前端展示
        self._plugin_errors: dict[str, str] = {}
        # 前端是否已就绪；就绪后新启用的插件需要单独补调 on_frontend_ready
        self._frontend_ready = False
        self._sidebar_collapsed: bool | None = None  # 侧栏是否折叠

    def initialize(self, data_path: Path, resource_path: Path | None = None) -> None:
        """
        从用户和系统目录发现插件，按依赖顺序加载并启用可用插件。

        :param data_path: 启动器数据目录
        :param resource_path: 启动器只读资源目录
        """
        self._data_path = Path(data_path)
        self._resource_path = Path(resource_path) if resource_path is not None else self._data_path
        self._plugin_dir = self._data_path / "plugins"
        self._plugin_dir.mkdir(parents=True, exist_ok=True)
        self._plugin_config_dir = self._data_path / "plugin_config"
        self._plugin_config_dir.mkdir(parents=True, exist_ok=True)
        self._plugin_state_path = self._data_path / "plugin_state.json"
        failed_installs = set(recover_plugin_install_transactions(self._data_path, self._plugin_dir))
        if failed_installs:
            self.logger.error("插件安装恢复失败，启动时跳过这些用户插件: %s", sorted(failed_installs))
        self.logger.info(
            "正在初始化插件框架: user_dir=%s, system_dir=%s",
            self._plugin_dir,
            self._resource_path / "resources" / "system_plugins",
        )
        self._load_plugin_state()
        # 订阅 HTML 注入事件，收集插槽内容
        if not self._event_handlers_registered:
            self.events.subscribe("plugin:html_injected", self._on_html_injected)
            # 订阅 Vue 组件注册事件，收集 Vue 插槽和路由
            self.events.subscribe("plugin:vue_slot_registered", self._on_vue_slot_registered)
            self._event_handlers_registered = True

        user_candidates = self._collect_candidates(self._plugin_dir, is_system=False)
        system_candidates = self._collect_candidates(
            self._resource_path / "resources" / "system_plugins", is_system=True
        )
        system_plugins = {candidate["name"] for candidate in system_candidates}
        package_names = self._restore_package_plugins(
            {candidate["name"] for candidate in user_candidates}, system_plugins
        )
        candidates = [
            candidate
            for candidate in user_candidates
            if candidate["name"] not in failed_installs and candidate["name"] not in package_names
        ]
        candidates.extend(system_candidates)
        self.logger.debug(
            "插件发现完成: candidates=%d, disabled=%d, user_dir=%s",
            len(candidates),
            len(self._disabled_plugins),
            self._plugin_dir,
        )
        self._candidate_map = {c["name"]: c for c in candidates}
        # 禁用状态只属于当前仍安装的插件；插件目录被删除后不应留下幽灵列表项。
        self._prune_plugin_state(
            set(self._candidate_map) | failed_installs | package_names, non_disableable_plugins=system_plugins
        )
        self._dependency_resolution = self._resolve_candidate_dependencies(candidates)
        self.logger.debug(
            "插件依赖解析完成: load_order=%s, errors=%d",
            self._dependency_resolution.load_order,
            len(self._dependency_resolution.errors),
        )
        self.logger.info("正在按依赖顺序加载 %d 个插件候选项", len(candidates))
        self._load_plugins_in_order(candidates, self._dependency_resolution.load_order)
        self.logger.debug("插件加载阶段完成，正在启用已加载插件")
        self._enable_all()
        self.logger.info(
            "插件框架初始化完成，已加载 %d 个插件，已禁用 %d 个插件",
            len(self._plugins),
            len(self._disabled_plugins),
        )
