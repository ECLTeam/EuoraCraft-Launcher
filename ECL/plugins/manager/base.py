# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件管理器内部共享基类与工具。
#
# 公开接口：无；内部基类由 PluginManager 组合使用。
# ============================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

import httpx

from ECL.java.manager import JavaManager
from ECL.java.queries import JavaQueries
from ECL.mc.adapters.factory import DefaultMcAdapterFactory
from ECL.mc.adapters.resources import InstanceQueries
from ECL.plugins.api.contracts import JsonValue, ProtocolError
from ECL.plugins.auth_providers import (
    AuthField,
    AuthProviderRegistry,
    sanitize_account_payload,
    sanitize_credentials_payload,
    sanitize_private_auth_data,
)
from ECL.plugins.crash_extensions import (
    CrashAnalysisContext,
    CrashAnalysisExtensionRegistry,
    limit_output_characters,
    limit_reasons,
    sanitize_extension_result,
)
from ECL.plugins.dependencies import DependencyResolution, parse_dependency
from ECL.plugins.host_dependencies import HostDependencyPolicy
from ECL.plugins.instance_compat import (
    ExternalInstanceMetadata,
    InstanceCompatibilityContext,
    InstanceCompatibilityRegistry,
    sanitize_external_metadata,
)
from ECL.plugins.launch_hooks import LaunchContext, LaunchHookRegistry, apply_remote_modifications
from ECL.plugins.plugin import Plugin
from ECL.plugins.run_models import PluginLease
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager
from ECL.utils import EventBus, get_logger
from ECL.utils.account_reading import AccountSummary
from ECL.utils.instance_reading import InstanceReadError

from .install_transaction import recover_plugin_install_transactions

if TYPE_CHECKING:
    from ECL.plugins.package_activation import PluginPackageActivationStore
    from ECL.services.game.instance_profile_transactions import InstanceProfileTransactions
    from ECL.services.game.plugin_games import PluginGameCoordinator


class _PluginState:
    """
    保存插件查找、启用和扩展注册所需的共用数据。

    该内部基类以 Mixin 形式经组合被 ``PluginManager`` 继承复用，不作为第二套公开插件 API 暴露。
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        processes: Any = None,
        instance_compatibility: InstanceCompatibilityRegistry | None = None,
        launch_hooks: LaunchHookRegistry | None = None,
        http_client: httpx.Client | None = None,
        auth_providers: AuthProviderRegistry | None = None,
        crash_extensions: CrashAnalysisExtensionRegistry | None = None,
        instance_queries: InstanceQueries | None = None,
        instance_profiles: InstanceProfileTransactions | None = None,
        instance_roots: Callable[[], tuple[Path, ...]] | None = None,
        operations: OperationManager | None = None,
        game_operations: PluginGameCoordinator | None = None,
        java_queries: JavaQueries | None = None,
        java_manager: JavaManager | None = None,
        account_snapshot: Callable[[], tuple[AccountSummary, ...]] | None = None,
        account_skin: Callable[[str], bytes | None] | None = None,
        public_config: Callable[[], JsonValue] | None = None,
    ):
        """
        创建相互隔离的插件状态与命令执行器。

        :param event_bus: 当前应用使用的事件总线
        :param processes: 面向插件的通用子进程注册服务；None 表示当前环境未提供该能力
        :param instance_compatibility: 与游戏服务共享的实例兼容提供者注册表
        :param launch_hooks: 与游戏服务共享的启动钩子注册表
        :param http_client: 应用共享 HTTP 客户端，供插件的受控网络请求使用
        :param auth_providers: 与账户服务共享的自定义认证提供方注册表
        :param crash_extensions: 与游戏服务共用的插件崩溃分析回调列表
        :param instance_profiles: 应用现有的共享实例资料事务资源
        :param instance_queries: 应用现有的无写入实例查询接口
        :param operations: 应用共用的后台任务执行与取消服务
        :param java_queries: 应用已有的 Java 只读查询及受控探测服务
        :param java_manager: 应用唯一 Java 安装与登记服务
        :param account_snapshot: 不执行认证的最小账户摘要
        :param public_config: 当前配置快照，仅在宿主边界投影公开字段
        :param instance_roots: 当前配置中的游戏根目录快照
        :param game_operations: 应用唯一游戏业务入口
        """
        self.logger = get_logger("PluginManager")
        self.events = event_bus or EventBus()
        self.processes = processes  # 插件可经 framework.processes 启动子进程实例
        self.instance_compatibility = instance_compatibility or InstanceCompatibilityRegistry()
        self.launch_hooks = launch_hooks or LaunchHookRegistry()
        self.http_client = http_client
        self._operations = operations
        self._game_operations = game_operations
        self._java_queries = java_queries
        self._java_manager = java_manager
        self._account_snapshot = account_snapshot
        self._account_skin = account_skin
        self._public_config = public_config
        self._instance_profiles = instance_profiles
        self._instance_queries = instance_queries
        self._instance_roots = instance_roots
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
        # 被禁用的插件名集合，持久化到 plugin_state.json
        self._disabled_plugins: set[str] = set()
        self._session_disabled_plugin_ids: frozenset[str] = frozenset()
        # 启动阶段是否自动加载并启用插件；--disable-plugins 安全模式下为 False
        self._startup_auto_enable = True
        self._plugin_state_path: Path | None = None  # plugin_state.json 的路径
        # 候选插件映射，用于在启用被禁用的插件时按需加载
        self._candidate_map: dict[str, dict[str, Any]] = {}
        self._package_store: PluginPackageActivationStore | None = None
        self._package_entries: dict[str, dict[str, Any]] = {}
        self._package_candidates: dict[str, dict[str, Any]] = {}
        self._host_dependency_policy: HostDependencyPolicy | None = None
        self._package_conflicts: set[str] = set()
        self._package_lock = RLock()
        self._action_locks: dict[str, RLock] = {}
        # 插件实例化/启用失败的详细错误信息，供前端展示
        self._plugin_errors: dict[str, str] = {}
        # 前端是否已就绪；就绪后新启用的插件需要单独补调 on_frontend_ready
        self._frontend_ready = False
        self._sidebar_collapsed: bool | None = None  # 侧栏是否折叠
        self.worker_service: PluginWorkerService | None = None

    def _plugin_action_lock(self, name: str) -> RLock:
        """
        为同一插件的安装与生命周期操作提供稳定串行边界，不锁住其他插件。
        """
        with self._package_lock:
            return self._action_locks.setdefault(name, RLock())

    def initialize(
        self,
        data_path: Path,
        resource_path: Path | None = None,
        *,
        auto_enable: bool = True,
        development_mode: bool = False,
        disabled_plugin_ids: frozenset[str] = frozenset(),
    ) -> None:
        """
        从用户和系统目录发现插件，按依赖顺序加载并启用可用插件。

        :param data_path: 启动器数据目录
        :param resource_path: 启动器只读资源目录
        :param auto_enable: 是否在发现后立即加载并启用插件；False 时仅完成发现、
            安装事务恢复与依赖解析（对应命令行 --disable-plugins 安全模式），
            插件保持未加载状态，可由用户在插件页或开发通道按需启用
        :param development_mode: 本次启动是否开启目录插件开发模式；安全模式下不生效
        :param disabled_plugin_ids: 本次运行始终禁止装载和启用的插件，不改变持久偏好
        """
        self._data_path = Path(data_path)
        self._session_disabled_plugin_ids = disabled_plugin_ids
        if type(development_mode) is not bool:
            raise ValueError("插件开发模式配置必须为布尔值")
        self.worker_service = PluginWorkerService(
            self._data_path,
            enable_block_reason=self._session_disable_reason,
            instance_queries=self._instance_queries,
            instance_profiles=self._instance_profiles,
            instance_roots=self._instance_roots,
            mc_factory=DefaultMcAdapterFactory(),
            operations=self._operations,
            game_operations=self._game_operations,
            java_queries=self._java_queries,
            java_manager=self._java_manager,
            account_snapshot=self._account_snapshot,
            account_skin=self._account_skin,
            public_config=self._public_config,
            slot_sink=self._on_worker_slot,
            hook_sink=self._on_worker_hook,
            crash_sink=self._on_worker_crash,
            compat_sink=self._on_worker_compat,
            tool_spawner=self._spawn_worker_tool,
            tool_stopper=self._stop_worker_tool,
            auth_sink=self._on_worker_auth,
            development_changed=lambda: self.events.emit(
                "plugin:status_changed", {"plugin": "", "status": "development"}
            ),
            settings_changed=lambda installation_id: self.events.emit(
                "plugin:settings_updated", {"installation_id": installation_id}
            ),
        )
        self.events.subscribe("config:updated", self._on_instance_config_changed)
        self._resource_path = Path(resource_path) if resource_path is not None else self._data_path
        self._plugin_dir = self._data_path / "plugins" / "sources"
        self._plugin_dir.mkdir(parents=True, exist_ok=True)
        self._plugin_config_dir = self._data_path / "plugins" / "config"
        self._plugin_config_dir.mkdir(parents=True, exist_ok=True)
        self._plugin_state_path = self._data_path / "plugins" / "state.json"
        failed_installs = set(
            recover_plugin_install_transactions(self._data_path, self._plugin_dir, self._plugin_action_lock)
        )
        if failed_installs:
            self.logger.error("插件安装恢复失败，启动时跳过这些用户插件: %s", sorted(failed_installs))
        self.logger.info("正在初始化插件框架")
        self._load_plugin_state()
        self._startup_auto_enable = auto_enable
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
            {candidate["name"] for candidate in user_candidates}, system_plugins, check_dependencies=auto_enable
        )
        candidates = [
            candidate
            for candidate in user_candidates
            if candidate["name"] not in failed_installs
            and candidate["name"] not in package_names
            and candidate["name"] not in system_plugins
        ]
        candidates.extend(system_candidates)
        candidates.extend(self._package_candidates.values())
        self.logger.debug(
            "插件发现完成：候选数量：%d；禁用数量：%d",
            len(candidates),
            len(self._disabled_plugins),
        )
        self._candidate_map = {c["name"]: c for c in candidates}
        if self._session_disabled_plugin_ids - self._candidate_map.keys():
            raise ProtocolError("指定停用的插件不存在", "PLUGIN_NOT_FOUND")
        for candidate in candidates:
            if not candidate["is_system"]:
                self._load_plugin(candidate["plugin_dir"], candidate["metadata_path"], False)
        # 禁用状态只属于当前仍安装的插件；插件目录被删除后不应留下幽灵列表项。
        self._prune_plugin_state(
            set(self._candidate_map) | failed_installs | package_names, non_disableable_plugins=system_plugins
        )
        self._dependency_resolution = self._resolve_candidate_dependencies(candidates)
        if development_mode and auto_enable:
            self.worker_service.development.set_enabled(True)
        self.logger.debug(
            "插件依赖解析完成：加载顺序：%s；错误数量：%d",
            self._dependency_resolution.load_order,
            len(self._dependency_resolution.errors),
        )
        if not auto_enable:
            # 安全模式：只完成发现与依赖解析，不执行插件代码，保留按需启用的能力。
            self.logger.warning("已按启动参数跳过插件的加载与启用，可稍后在插件页手动启用")
        else:
            self.logger.info("正在按依赖顺序加载 %d 个插件候选项", len(candidates))
            self._load_plugins_in_order(candidates, self._dependency_resolution.load_order)
            self.logger.debug("插件加载阶段完成，正在启用已加载插件")
            self._enable_all()
        self.logger.info(
            "插件框架初始化完成，已加载 %d 个插件，已禁用 %d 个插件",
            len(self._plugins),
            len(self._disabled_plugins),
        )

    def _session_disable_reason(self, name: str) -> str | None:
        """
        从会话禁用与必要依赖推导运行阻断原因，不修改持久禁用集合。

        每次按当前候选元数据计算，安装、重载或替换同 ID 插件不能绕过限制。
        访问集合避免依赖环导致无限遍历；可选依赖不传播阻断。
        """
        pending = [name]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in self._session_disabled_plugin_ids:
                return "PLUGIN_DISABLED_FOR_SESSION" if current == name else "PLUGIN_DEPENDENCY_DISABLED_FOR_SESSION"
            if current in visited:
                continue
            visited.add(current)
            dependencies = self._candidate_map.get(current, {}).get("metadata", {}).get("dependencies", {})
            if not isinstance(dependencies, dict):
                continue
            for dependency_name, declaration in dependencies.items():
                requirement = parse_dependency(dependency_name, declaration)
                if requirement is not None and not requirement.optional:
                    pending.append(requirement.name)
        return None

    def _on_worker_hook(self, name: str, phase: str | None, callback_ref: str | None, required: bool) -> None:
        """
        登记或撤销第三方启动钩子，阶段到注册表回调名的映射在此固定。

        :param name: 安装名称
        :param phase: 钩子阶段；None 表示撤销该安装全部钩子
        :param callback_ref: 本运行登记的回调标识
        :param required: 前置失败时是否阻止启动
        """
        if phase is None:
            self.launch_hooks.unregister_owner(name)
            return
        attribute = {
            "prepare": "on_prepare",
            "pre_launch": "pre_launch",
            "post_launch": "post_launch",
            "exit": "on_exit",
        }[phase]
        self.launch_hooks.register(
            owner=name,
            name=f"worker:{phase}",
            required=required,
            **{attribute: self._worker_hook_callable(name, phase, callback_ref)},
        )

    def _on_worker_auth(
        self, name: str, provider_id: str | None, definition: object | None, lease: PluginLease | None
    ) -> None:
        """
        登记或撤销第三方登录提供方，凭据由账户服务按提供方分区保存。

        :param name: 安装名称
        :param provider_id: 提供方标识；None 表示撤销
        :param definition: 已校验的提供方定义
        """
        if provider_id is None or definition is None:
            self.auth_providers.unregister_owner(name)
            return
        if lease is None:
            raise ProtocolError("认证提供方缺少原始许可", "PLUGIN_PERMISSION_DENIED")
        fields = [
            AuthField(key=item.key, label=item.label, type=item.type, required=item.required)
            for item in definition.fields
        ]
        authenticate_ref = definition.authenticate_ref
        resolve_ref = definition.resolve_ref

        def authenticate(values: Mapping[str, object]) -> dict[str, object]:
            if self.worker_service is None:
                raise RuntimeError("插件运行未就绪")
            field_by_key = {item.key: item for item in definition.fields}
            if not values.keys() <= field_by_key.keys():
                raise ValueError("登录表单包含未声明字段")
            fields: dict[str, JsonValue] = {}
            for key, field in field_by_key.items():
                value = values.get(key)
                if value is None and not field.required:
                    continue
                if not isinstance(value, str) or len(value) > 1024 or (field.required and value == ""):
                    raise ValueError("登录表单字段无效")
                fields[key] = value
            payload = {"fields": fields}
            return sanitize_account_payload(
                self.worker_service.invoke_hook(name, authenticate_ref, payload, 5.0, lease=lease)
            )

        def resolve(current: dict[str, object]) -> dict[str, str]:
            if self.worker_service is None:
                raise RuntimeError("插件运行未就绪")
            payload = {"account": {key: str(current.get(key, ""))[:256] for key in ("id", "alias", "uuid")}}
            private_data = current.get("data")
            if private_data is not None:
                payload["account"]["data"] = sanitize_private_auth_data(private_data)
            return sanitize_credentials_payload(
                self.worker_service.invoke_hook(name, resolve_ref, payload, 5.0, lease=lease)
            )

        self.auth_providers.register(
            owner=name,
            provider_id=provider_id,
            title=definition.title,
            fields=fields,
            authenticate=authenticate,
            resolve_credentials=resolve,
            description=definition.description,
        )

    def _spawn_worker_tool(self, owner: str, title: str, argv: list[str], cwd: str) -> str:
        """
        以插件归属启动工具进程，游戏实例不经过这条路径。

        :param owner: 安装名称，作为进程归属标识
        :param title: 面向用户显示的名称
        :param argv: 完整命令行
        :param cwd: 工作目录，固定为插件包目录
        :return: 进程标识
        :raises ProtocolError: 宿主未提供进程服务时抛出
        """
        if self.processes is None:
            raise ProtocolError("当前宿主未提供进程服务", "PLUGIN_API_METHOD_NOT_FOUND")
        return self.processes.spawn(name=title, type_=f"plugin:{owner}", args=argv, cwd=cwd, owner=owner)

    def _stop_worker_tool(self, owner: str, process_ref: str) -> bool:
        """
        只允许停止归属一致的进程，游戏实例与其他插件的工具一律拒绝。

        :param owner: 安装名称
        :param process_ref: 进程标识
        :return: 是否已确认停止
        :raises ProtocolError: 归属不符或宿主未提供进程服务时抛出
        """
        if self.processes is None:
            raise ProtocolError("当前宿主未提供进程服务", "PLUGIN_API_METHOD_NOT_FOUND")
        if not self.processes.owned_by(process_ref, owner):
            raise ProtocolError("只能停止自己的工具进程", "PLUGIN_TOOL_OWNERSHIP_DENIED")
        return self.processes.stop(process_ref, force=True)

    def _on_worker_compat(self, name: str, source: str | None, title: str | None, callback_ref: str | None) -> None:
        """
        登记或撤销第三方实例兼容来源，未获准的实例不会被投递。

        :param name: 安装名称
        :param source: 来源标识；None 表示撤销
        :param title: 面向用户显示的来源名称
        :param callback_ref: 本运行登记的回调标识
        """
        if source is None or callback_ref is None:
            self.instance_compatibility.unregister_owner(name)
            return
        self.instance_compatibility.register(
            owner=name,
            source=source,
            title=title or source,
            reader=self._worker_compat_reader(name, source, callback_ref),
            watch_paths=lambda _options: self._worker_selected_files(name),
        )

    def _worker_selected_files(self, name: str) -> list[Path]:
        """
        把用户选择的外部索引纳入现有扫描缓存的文件修订检查。
        """
        from ECL.plugins.runtime.selected_files import selected_file_paths

        if self.worker_service is None:
            return []
        return selected_file_paths(self.worker_service.settings_view(name))

    def _worker_compat_reader(self, name: str, source: str, callback_ref: str):
        """
        构造转发到插件进程的实例读取器，只投递已获准实例的引用与相对目录。

        :param name: 安装名称
        :param source: 来源标识
        :param callback_ref: 本运行登记的回调标识
        :return: 注册表可调用的读取器
        """

        def read(context: InstanceCompatibilityContext) -> ExternalInstanceMetadata | None:
            if self.worker_service is None:
                return None
            reference = self.worker_service.instance_ref(context.game_path, context.version_id)
            if reference is None or reference not in self.worker_service.available_instance_refs(name):
                return None
            try:
                instance_rel = context.instance_path.relative_to(context.game_path).as_posix()
            except ValueError:
                instance_rel = ""
            payload = {
                "root_ref": reference,
                "instance_rel": instance_rel,
                "version_id": context.version_id,
                "vanilla_name": context.vanilla_name,
                "primary_loader": context.primary_loader,
                "instance_path": str(context.instance_path),
            }
            value = self.worker_service.invoke_hook(name, callback_ref, payload, 5.0)
            return sanitize_external_metadata(value, source)

        return read

    def _on_worker_crash(self, name: str, callback_ref: str | None) -> None:
        """
        登记或撤销第三方崩溃分析回调。

        :param name: 安装名称
        :param callback_ref: 本运行登记的回调标识；None 表示撤销
        """
        if callback_ref is None:
            self.crash_extensions.unregister_owner(name)
            return
        self.crash_extensions.register(name, "worker", self._worker_crash_callable(name, callback_ref))

    def _worker_crash_callable(self, name: str, callback_ref: str):
        """
        构造转发到插件进程的分析回调，只投递有界上下文并净化返回值。

        :param name: 安装名称
        :param callback_ref: 本运行登记的回调标识
        :return: 注册表可调用的分析函数
        """

        def enrich(context: CrashAnalysisContext, _result: dict[str, object]) -> dict[str, object]:
            if self.worker_service is None:
                return {}
            payload = {
                "report_id": context.report_id,
                "version_id": context.version_id,
                "exit_code": context.exit_code,
                "detected_by": list(context.detected_by)[:8],
                "reasons": [
                    {key: str(value)[:512] for key, value in item.items()}
                    for item in context.reasons[:limit_reasons]
                    if isinstance(item, dict)
                ],
                "output": context.output[:limit_output_characters],
            }
            value = self.worker_service.invoke_hook(name, callback_ref, payload, 5.0)
            return sanitize_extension_result(value)

        return enrich

    def _worker_hook_callable(self, name: str, phase: str, callback_ref: str | None):
        """
        构造转发到插件进程的钩子回调，只传有界上下文并限制可修改项。

        :param name: 安装名称
        :param phase: 钩子阶段
        :param callback_ref: 本运行登记的回调标识
        :return: 注册表可调用的钩子函数
        """

        def call(context: LaunchContext) -> None:
            if callback_ref is None or self.worker_service is None:
                return
            payload = {
                "version_id": context.version_id,
                "loader": context.loader,
                "version_isolation": context.version_isolation,
            }
            value = self.worker_service.invoke_hook(name, callback_ref, payload, 5.0)
            if phase == "prepare" and isinstance(value, dict):
                apply_remote_modifications(context, value)

        return call

    def _on_worker_slot(self, name: str, payload: dict[str, object]) -> None:
        """
        接收第三方已获准的插槽内容，与系统插件走同一收集与渲染路径。

        这里只负责转发与清理，不把内容写入磁盘。

        :param name: 安装名称
        :param payload: 插槽内容或清理指令
        """
        kind = payload.get("kind")
        if kind == "clear":
            self.clear_plugin_slots(name)
            return
        if kind == "page":
            self._register_worker_page(name, payload)
            return
        if kind == "html":
            self.events.emit(
                "plugin:html_injected",
                name,
                payload.get("slot_id"),
                payload.get("html"),
                payload.get("key"),
                payload.get("context_key"),
            )
            return
        if kind == "vue":
            self.events.emit(
                "plugin:vue_slot_registered",
                name,
                payload.get("slot_id"),
                payload.get("component_name"),
                payload.get("template"),
                payload.get("script", ""),
                payload.get("style", ""),
                payload.get("context_key"),
            )
            return
        self.logger.warning("插件插槽内容类型无效，已忽略: %s", kind)

    def _register_worker_page(self, name: str, payload: dict[str, object]) -> None:
        """
        把已批准的 Worker 页面交给既有路由收集器，组件身份包含插件名称。

        :param name: 宿主确认的安装名称
        :param payload: Broker 校验后的页面声明
        """
        from ECL.plugins.runtime.models import VuePageDefinition

        page = VuePageDefinition.model_validate({key: value for key, value in payload.items() if key != "kind"})
        component_name = f"{name}__{page.component_name}"
        self._routes = [route for route in self._routes if not (route["plugin"] == name and route["path"] == page.path)]
        self._vue_routes = [
            route for route in self._vue_routes if not (route["plugin"] == name and route["path"] == page.path)
        ]
        nav_payload = page.nav.model_dump(mode="json") if page.nav is not None else None
        self._routes.append(
            {
                "plugin": name,
                "path": page.path,
                "title": page.title,
                "icon": page.icon,
                "component": component_name,
                "type": "vue",
                "locales": page.locales,
                "container": page.container,
                "nav": nav_payload,
            }
        )
        definition = {"plugin": name, "template": page.template, "script": page.script, "style": page.style}
        self._vue_components[component_name] = definition
        self._vue_routes.append(
            {
                **definition,
                "component_name": component_name,
                "path": page.path,
                "title": page.title,
                "icon": page.icon,
                "container": page.container,
                "nav": nav_payload,
            }
        )
        self.events.emit(
            "plugin:vue_route_registered",
            name,
            page.path,
            page.title,
            component_name,
            page.template,
            page.script,
            page.style,
            page.icon,
        )
        self.events.emit("plugin:route_registered", name, page.path, page.title, page.icon)

    def _on_instance_config_changed(self, section: str, value: object) -> None:
        """
        目录配置变化时立即退休被移除的共享资源，不等待页面查询或下一次启用。
        """
        if section != "game" or self.worker_service is None or self._instance_queries is None:
            return
        try:
            self.worker_service.refresh_resources()
        except InstanceReadError:
            self.logger.warning("游戏目录变更后的插件资源同步失败，相关许可保持拒绝")
