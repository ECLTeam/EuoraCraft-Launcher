# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件基类：宿主回调钩子（事件/命令/路由/槽位/样式）默认实现。
#
# 公开接口：
#   - class Plugin — 插件基类，所有插件必须继承此类。
#       - on_event(event) -> Callable — 装饰器，将方法注册为事件处理器，实例化时自动订阅。
#       - on_command(name, description=…) -> Callable — 装饰器，将方法注册为命令处理器，实例化时自动注册。
#       - on_setting(key, default, type_=…, description=…) -> Callable — 装饰器，声明设置项，实例化时自动注册（被装饰的函数体可为空）。
#       - on_route(path, title, icon=…) -> Callable — 装饰器，声明侧边栏路由，on_enable 时自动注册。
#       - on_css(file_path) -> Callable — 装饰器，声明 CSS 文件，on_frontend_ready 时自动注入到前端。
#       - on_html(slot_id, file_path) -> Callable — 装饰器，声明 HTML 文件，on_frontend_ready 时自动注入到指定插槽。
#       - on_script(file_path) -> Callable — 装饰器，声明 JS 文件，on_frontend_ready 时自动注入到前端。
#       - on_vue_slot(slot_id, component_name, file_path) -> Callable — 装饰器，声明 Vue SFC 组件，on_frontend_ready 时自动注册到指定插槽。
#       - on_vue_route(path, title, component_name, file_path, icon=…) -> Callable — 装饰器，声明 Vue SFC 路由页面，on_enable 时自动注册。
#       - on_load() -> None — 插件加载时读取资源，此时不应注册路由或命令。
#       - on_enable() -> None — 插件启用时注册路由、命令与设置项。子类覆盖时须调用 super().on_enable()。
#       - on_frontend_ready() -> None — 前端就绪后添加插件的 CSS/HTML/JS。子类覆盖时须调用 super().on_frontend_ready()。
#       - on_disable() -> None — 插件禁用时清理运行中的状态。
#       - on_unload() -> None — 插件卸载时释放资源。
#       - register_route(path, title, icon=…) -> None — 注册前端侧边栏路由。
#       - register_command(name, handler, description=…) -> None — 注册插件命令，前端通过 plugin_call_command 调用。
#       - register_setting(key, default, description=…, type_=…) -> None — 注册设置项，前端设置页根据此定义渲染控件。
#       - get_setting(key) -> Any — 读取插件设置当前值；尚未保存时返回声明的默认值。
#       - register_instance_compatibility(source, title, reader, watch_paths=…) -> None — 注册一个只读实例元数据兼容来源。
#       - register_connector_extension(name, protocols, on_guest_joined, enrich_status, before_leave, on_reset) -> None — 注册 Scaffolding 扩展协议及其联机会话钩子。
#       - register_launch_hook(name, on_prepare, pre_launch, post_launch, on_exit) -> None — 注册启动钩子，可修改 Minecraft 启动参数，并在游戏进程启动或退出时执行回调。
#       - http_request(method, url, params, headers, json, data, timeout) -> PluginHttpResponse — 发起受控的 HTTP 请求。
#       - http_get(url, **kwargs) -> PluginHttpResponse — 受控的 GET 请求，参数与 ``http_request`` 一致。
#       - http_post(url, **kwargs) -> PluginHttpResponse — 受控的 POST 请求，参数与 ``http_request`` 一致。
#       - register_auth_provider(provider_id, title, fields, authenticate, resolve_credentials, description=…) -> None — 注册自定义账户认证提供方。
#       - register_crash_analyzer(name, enrich) -> None — 注册插件崩溃分析回调。
#       - inject_css(css, key=…) -> None — 向宿主前端注入仅作用于当前插件内容的 CSS 样式。
#       - inject_html(slot_id, html, key=…, context_key=…) -> None — 向宿主前端注入 HTML 片段。
#       - inject_script(script) -> None — 向宿主前端注入 JavaScript 脚本。
#       - inject_typescript(script) -> None — 向宿主前端注入 TypeScript 脚本（由前端经 sucrase 转译）。
#       - register_html_file(slot_id, file_path) -> None — 读取插件目录下的 HTML 文件注入到指定插槽。
#       - register_css_file(file_path) -> None — 读取插件目录下的 CSS 文件注入到宿主前端。
#       - register_script_file(file_path) -> None — 读取插件目录下的 JS 文件注入到宿主前端。
#       - register_vue_slot_file(slot_id, component_name, file_path) -> None — 读取插件目录下的 Vue SFC 文件，解析后注册到插槽。
#       - register_vue_route_file(path, title, component_name, file_path, icon=…) -> None — 读取插件目录下的 Vue SFC 文件，解析后注册为独立路由页面。
#       - register_vue_slot(slot_id, component_name, template, script=…, style=…, context_key=…) -> None — 注册 Vue 组件到指定插槽，前端收到后动态创建并挂载。
#       - register_vue_route(path, title, component_name, template, script=…, style=…, icon=…) -> None — 注册 Vue 组件作为独立路由页面。
#       - emit(event, payload=…) -> None — 向全局事件总线发射事件，其他插件或宿主可订阅。
#       - subscribe(event, handler) -> None — 订阅全局事件；由插件管理器统一注册，自动以插件名作为所有者。
#       - load_file(relative_path, encoding=…) -> str | None — 读取插件目录下的文本文件。
#       - resource_path(relative_path) -> Path — 获取插件 resources/ 目录下的文件路径。
#       - load_resource(relative_path, encoding=…) -> str | None — 读取插件 resources/ 目录下的文本文件。
# ============================================================

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ECL.plugins.auth_providers import AuthField
from ECL.plugins.launch_hooks import LaunchContext
from ECL.plugins.network import PluginHttpError, PluginHttpResponse, PluginNetworkPolicy, _action_for_method
from ECL.plugins.permissions import Permission, PermissionAction, PermissionScope
from ECL.utils import get_logger

if TYPE_CHECKING:
    from ECL.plugins.framework import PluginManager


def _register_event(cls, name: str, args: tuple) -> None:
    # 注册事件处理器条目：事件名 + 方法名。
    cls._event_handlers.append((args[0], name))


def _register_command(cls, name: str, args: tuple) -> None:
    # 注册命令条目：命令名 + 方法名 + 描述。
    cls._command_handlers.append((args[0], name, args[1]))


def _register_setting(cls, name: str, args: tuple) -> None:
    # 注册设置项定义条目。
    cls._setting_definitions.append((args[0], args[1], args[2], args[3]))


def _register_route(cls, name: str, args: tuple) -> None:
    # 注册侧边栏路由条目：路径 + 标题 + 图标。
    cls._route_definitions.append((args[0], args[1], args[2]))


def _register_frontend_injection(kind: str) -> Callable[[type, str, tuple], None]:
    # 生成前端注入类装饰器的注册函数，条目直接复用装饰器参数元组。

    def registrar(cls, name: str, args: tuple) -> None:
        cls._frontend_injections.append((kind, args))

    return registrar


class PluginDecoratorRegistry:
    """
    保存插件装饰器类型到注册器的映射。
    """

    registrars: dict[str, Callable[[type, str, tuple], None]] = {
        "event": _register_event,
        "command": _register_command,
        "setting": _register_setting,
        "route": _register_route,
        "css": _register_frontend_injection("css"),
        "html": _register_frontend_injection("html"),
        "script": _register_frontend_injection("script"),
        "vue_slot": _register_frontend_injection("vue_slot"),
        "vue_route": _register_frontend_injection("vue_route"),
    }


def _register_decorated(cls) -> None:
    # 扫描类的 __dict__，将装饰器在函数对象上留下的标记转换为注册表条目。
    for name, value in cls.__dict__.items():
        meta = getattr(value, "_ecl_decorator", None)
        if meta is None:
            continue
        kind, args = meta
        registrar = PluginDecoratorRegistry.registrars.get(kind)
        if registrar is not None:
            registrar(cls, name, args)


class Plugin:
    """
    插件基类，所有插件必须继承此类。
    调用顺序：on_load → on_enable → on_frontend_ready → on_disable → on_unload

    支持装饰器语法：
        @Plugin.on_event("config:updated")
        def on_config_changed(self, section, data): ...

        @Plugin.on_command("hello", description="返回问候语")
        def cmd_hello(self, name="World"): ...

        @Plugin.on_setting(key="max_count", default=10, type_="number", description="最大数量")
        def _setting_max_count(self): ...

        @Plugin.on_route("/my-plugin", "我的插件", icon="puzzle")
        def _route_my_plugin(self): ...

        @Plugin.on_vue_route("/my-plugin", "我的插件", "my-page", "page.vue", icon="puzzle")
        def _vue_route_my_plugin(self): ...

        @Plugin.on_css("style.css")
        def _css_style(self): ...

        @Plugin.on_html("sidebar-bottom", "sidebar.html")
        def _html_sidebar(self): ...

        @Plugin.on_script("counter.js")
        def _js_counter(self): ...

        @Plugin.on_vue_slot("sidebar-bottom", "my-widget", "widget.vue")
        def _vue_slot_widget(self): ...

    子进程能力：framework 提供 ``self.processes``（``ProcessService``）。插件可用
    ``spawn(name, type_, args, cwd=None, stdin=False)`` 启动子进程并得到实例标识，
    用 ``send_stdin(id, data)`` / ``stop(id)`` 交互，实例状态经 ``process:*`` 事件
    实时推送到前端终端实例视图。
    """

    # 每个子类独立持有的装饰器注册表
    # 基类上必须有默认值，因为装饰器在子类体执行期间就被求值（早于 __init_subclass__）
    _event_handlers: list[tuple[str, str]] = []
    _command_handlers: list[tuple[str, str, str]] = []
    _setting_definitions: list[tuple[str, Any, str, str]] = []
    _route_definitions: list[tuple[str, str, str]] = []
    _frontend_injections: list[tuple[str, tuple]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 为每个子类创建独立的注册表，并从当前类 __dict__ 中收集被装饰的方法
        cls._event_handlers = []
        cls._command_handlers = []
        cls._setting_definitions = []
        cls._route_definitions = []
        cls._frontend_injections = []
        _register_decorated(cls)

    def __init__(
        self,
        framework: "PluginManager",
        plugin_dir: Path,
        metadata: dict[str, Any],
        is_system: bool = False,
    ) -> None:
        self.framework = framework  # 所属插件管理器
        # 通用子进程注册服务；未提供时为 None。插件可通过它启动子进程、写 stdin、停止与列出实例。
        self.processes = getattr(framework, "processes", None)
        self.plugin_dir: Path = plugin_dir  # 插件根目录，用于读取 resources/
        self.name: str = metadata["name"]  # 插件唯一名称
        self.title: str = metadata.get("title", self.name)  # 插件显示标题
        self.version: str = metadata.get("version", "0.0.0")  # 插件版本号
        self.description: str = metadata.get("description", "")  # 插件描述
        self.author: str = metadata.get("author", "")  # 插件作者
        self.metadata: dict[str, Any] = metadata  # 插件清单元数据
        self.is_system = is_system  # 是否为系统内置插件
        self.logger = get_logger(f"Plugin.{self.name}")  # 插件日志记录器
        # 插件注册的命令映射，framework 调用时查表
        self._commands: dict[str, Callable] = {}
        # 插件的设置项定义，供前端设置页渲染
        self._settings: dict[str, dict[str, Any]] = {}
        # 消费装饰器注册表，绑定到实例
        self._apply_decorators()

    def _check_permission(self, scope: PermissionScope, action: PermissionAction, resource: str = "*") -> None:
        # 系统插件跳过权限校验，普通插件必须声明对应权限。
        if self.is_system:
            return
        self.framework._permission_manager.check_permission(self.name, Permission(scope, action, resource))

    def _apply_decorators(self) -> None:
        # 将类级别的装饰器注册表绑定到当前实例。
        for event, method_name in self._event_handlers:
            handler = getattr(self, method_name)
            self.subscribe(event, handler)
        for cmd_name, method_name, description in self._command_handlers:
            handler = getattr(self, method_name)
            self.register_command(cmd_name, handler, description)
        for key, default, description, type_ in self._setting_definitions:
            self.register_setting(key, default, description, type_)
        # 路由和前端代码会在插件启用或前端就绪时添加，此处只保存设置
        self._routes_to_register = list(self._route_definitions)
        self._injections_to_apply = list(self._frontend_injections)

    @staticmethod
    def on_event(event: str) -> Callable:
        """
        装饰器，将方法注册为事件处理器，实例化时自动订阅。
        :param event: 事件名称，如 "config:updated"
        """

        def wrapper(func):
            func._ecl_decorator = ("event", (event,))
            return func

        return wrapper

    @staticmethod
    def on_command(name: str, description: str = "") -> Callable:
        """
        装饰器，将方法注册为命令处理器，实例化时自动注册。
        :param name: 命令名，调用时格式为 "插件名:命令名"
        :param description: 命令描述
        """

        def wrapper(func):
            func._ecl_decorator = ("command", (name, description))
            return func

        return wrapper

    @staticmethod
    def on_setting(key: str, default: Any, type_: str = "string", description: str = "") -> Callable:
        """
        装饰器，声明设置项，实例化时自动注册（被装饰的函数体可为空）。
        :param key: 设置键名
        :param default: 默认值
        :param type_: 设置类型 bool | string | number | select
        :param description: 设置描述
        """

        def wrapper(func):
            func._ecl_decorator = ("setting", (key, default, description, type_))
            return func

        return wrapper

    @staticmethod
    def on_route(path: str, title: str, icon: str = "") -> Callable:
        """
        装饰器，声明侧边栏路由，on_enable 时自动注册。
        :param path: 路由路径，如 "/my-plugin"
        :param title: 显示名称
        :param icon: 图标名
        """

        def wrapper(func):
            func._ecl_decorator = ("route", (path, title, icon))
            return func

        return wrapper

    @staticmethod
    def on_css(file_path: str) -> Callable:
        """
        装饰器，声明 CSS 文件，on_frontend_ready 时自动注入到前端。
        :param file_path: 相对于插件目录的 CSS 文件路径，如 "style.css"
        """

        def wrapper(func):
            func._ecl_decorator = ("css", (file_path,))
            return func

        return wrapper

    @staticmethod
    def on_html(slot_id: str, file_path: str) -> Callable:
        """
        装饰器，声明 HTML 文件，on_frontend_ready 时自动注入到指定插槽。
        :param slot_id: 插槽 ID
        :param file_path: 相对于插件目录的 HTML 文件路径，如 "sidebar.html"
        """

        def wrapper(func):
            func._ecl_decorator = ("html", (slot_id, file_path))
            return func

        return wrapper

    @staticmethod
    def on_script(file_path: str) -> Callable:
        """
        装饰器，声明 JS 文件，on_frontend_ready 时自动注入到前端。
        :param file_path: 相对于插件目录的 JS 文件路径，如 "counter.js"
        """

        def wrapper(func):
            func._ecl_decorator = ("script", (file_path,))
            return func

        return wrapper

    @staticmethod
    def on_vue_slot(slot_id: str, component_name: str, file_path: str) -> Callable:
        """
        装饰器，声明 Vue SFC 组件，on_frontend_ready 时自动注册到指定插槽。
        :param slot_id: 插槽 ID
        :param component_name: 全局唯一组件名
        :param file_path: 相对于插件目录的 .vue 文件路径，如 "widget.vue"
        """

        def wrapper(func):
            func._ecl_decorator = ("vue_slot", (slot_id, component_name, file_path))
            return func

        return wrapper

    @staticmethod
    def on_vue_route(path: str, title: str, component_name: str, file_path: str, icon: str = "") -> Callable:
        """
        装饰器，声明 Vue SFC 路由页面，on_enable 时自动注册。
        :param path: 路由路径
        :param title: 侧边栏显示名称
        :param component_name: 全局唯一组件名
        :param file_path: 相对于插件目录的 .vue 文件路径，如 "page.vue"
        :param icon: 图标名
        """

        def wrapper(func):
            func._ecl_decorator = ("vue_route", (path, title, component_name, file_path, icon))
            return func

        return wrapper

    def on_load(self) -> None:
        """
        插件加载时读取资源，此时不应注册路由或命令。
        """

    def on_enable(self) -> None:
        """
        插件启用时注册路由、命令与设置项。子类覆盖时须调用 super().on_enable()。
        """
        for path, title, icon in self._routes_to_register:
            self.register_route(path, title, icon)

    def on_frontend_ready(self) -> None:
        """
        前端就绪后添加插件的 CSS/HTML/JS。子类覆盖时须调用 super().on_frontend_ready()。
        """
        for inj_type, args in self._injections_to_apply:
            if inj_type == "css":
                self.register_css_file(*args)
            elif inj_type == "html":
                self.register_html_file(*args)
            elif inj_type == "script":
                self.register_script_file(*args)
            elif inj_type == "vue_slot":
                self.register_vue_slot_file(*args)
            elif inj_type == "vue_route":
                self.register_vue_route_file(*args)

    def on_disable(self) -> None:
        """
        插件禁用时清理运行中的状态。
        """

    def on_unload(self) -> None:
        """
        插件卸载时释放资源。
        """

    def register_route(self, path: str, title: str, icon: str = "") -> None:
        """
        注册前端侧边栏路由。
        :param path: 路由路径，如 "/crash-reports"
        :param title: 显示名称
        :param icon: 图标名
        """
        self._check_permission(PermissionScope.UI, PermissionAction.WRITE)
        self.framework._register_route(self, path, title, icon)

    def register_command(self, name: str, handler: Callable, description: str = "") -> None:
        """
        注册插件命令，前端通过 plugin_call_command 调用。
        :param name: 命令名，调用时格式为 "插件名:命令名"
        :param handler: 命令处理函数
        :param description: 命令描述
        """
        self._check_permission(PermissionScope.COMMANDS, PermissionAction.EXECUTE, name)
        self._commands[name] = handler

    def register_setting(self, key: str, default: Any, description: str = "", type_: str = "string") -> None:
        """
        注册设置项，前端设置页根据此定义渲染控件。
        :param key: 设置键名
        :param default: 默认值
        :param description: 设置描述
        :param type_: 设置类型 bool | string | number | select
        """
        self._check_permission(PermissionScope.SETTINGS, PermissionAction.WRITE, key)
        self._settings[key] = {
            "key": key,
            "default": default,
            "description": description,
            "type": type_,
        }

    def get_setting(self, key: str) -> Any:
        """
        读取插件设置当前值；尚未保存时返回声明的默认值。

        :param key: 已注册的设置键
        :return: 当前设置值
        """
        self._check_permission(PermissionScope.SETTINGS, PermissionAction.READ, key)
        definition = self._settings.get(key)
        if definition is None:
            raise KeyError(f"插件设置未声明: {key}")
        return self.framework._config_values.get(f"{self.name}.{key}", definition["default"])

    def register_instance_compatibility(
        self,
        source: str,
        title: str,
        reader: Callable,
        watch_paths: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        """
        注册一个只读实例元数据兼容来源。

        ``reader`` 接收 ``InstanceCompatibilityContext`` 并返回
        ``ExternalInstanceMetadata`` 或 ``None``；``watch_paths`` 返回影响扫描
        缓存的外部文件路径。插件禁用或卸载时注册项会由宿主自动清理。

        :param source: 稳定的来源标识
        :param title: 面向用户显示的来源名称
        :param reader: 单实例元数据读取器
        :param watch_paths: 可选的外部监听路径解析器
        """
        self._check_permission(PermissionScope.INSTANCES, PermissionAction.READ, source)
        self.framework.instance_compatibility.register(
            owner=self.name,
            source=source,
            title=title,
            reader=reader,
            watch_paths=watch_paths,
        )

    def register_connector_extension(
        self,
        name: str,
        protocols: Mapping[str, Callable],
        *,
        on_guest_joined: Callable | None = None,
        enrich_status: Callable | None = None,
        before_leave: Callable | None = None,
        on_reset: Callable | None = None,
    ) -> None:
        """注册 Scaffolding 扩展协议及其联机会话钩子。

        ``protocols`` 的处理器接收 ``ConnectorProtocolRequest``；房客加入、
        状态刷新、离开与会话清理钩子接收 ``ConnectorSessionContext``。插件
        禁用或卸载时，宿主会自动移除其全部注册项。

        :param name: 稳定的扩展标识
        :param protocols: 协议名到处理器的映射
        :param on_guest_joined: 房客完成基础连接后的回调
        :param enrich_status: 补充联机状态的回调
        :param before_leave: 断开连接前的回调
        :param on_reset: 会话缓存清理回调
        """
        self._check_permission(PermissionScope.CONNECTOR, PermissionAction.WRITE, name)
        self.framework.connector_extensions.register(
            owner=self.name,
            name=name,
            protocols=protocols,
            on_guest_joined=on_guest_joined,
            enrich_status=enrich_status,
            before_leave=before_leave,
            on_reset=on_reset,
        )

    def register_launch_hook(
        self,
        name: str,
        *,
        on_prepare: Callable[[LaunchContext], Any] | None = None,
        pre_launch: Callable[[LaunchContext], Any] | None = None,
        post_launch: Callable[[LaunchContext], Any] | None = None,
        on_exit: Callable[[LaunchContext], Any] | None = None,
    ) -> None:
        """
        注册启动钩子，可修改 Minecraft 启动参数，并在游戏进程启动或退出时执行回调。

        ``on_prepare`` 可追加/修改 ``jvm_args``、``game_args`` 与 ``env``；
        ``pre_launch`` / ``post_launch`` 在进程创建前后调用；``on_exit``
        在游戏进程退出时调用。插件禁用或卸载时宿主自动撤销全部钩子。

        :param name: 稳定的钩子标识
        :param on_prepare: 启动参数准备阶段回调
        :param pre_launch: 进程创建前回调
        :param post_launch: 进程创建后回调
        :param on_exit: 游戏进程退出回调
        """
        self._check_permission(PermissionScope.LAUNCH, PermissionAction.WRITE, name)
        registry = self.framework.launch_hooks
        if registry is None:
            raise RuntimeError("当前环境未提供启动钩子能力")
        registry.register(
            owner=self.name,
            name=name,
            on_prepare=on_prepare,
            pre_launch=pre_launch,
            post_launch=post_launch,
            on_exit=on_exit,
        )

    def http_request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        timeout: float = 15.0,
    ) -> PluginHttpResponse:
        """
        发起受控的 HTTP 请求。

        只允许 ``http`` / ``https`` URL，并按主机名校验权限：只读方法
        （GET/HEAD/OPTIONS）要求 ``network/read/<host>``，其余方法要求
        ``network/write/<host>``。响应体上限 8 MiB，超出部分被截断标记。

        :param method: HTTP 方法
        :param url: 完整请求地址
        :param params: 查询参数
        :param headers: 附加请求头
        :param json: JSON 请求体
        :param data: 原始请求体
        :param timeout: 请求超时秒数
        :return: 受限的响应视图
        :raises PluginHttpError: URL 非法、权限不足、网络失败或环境未提供客户端
        """
        from urllib.parse import urlparse

        parsed = urlparse(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PluginHttpError(f"仅支持 http/https URL: {url}")
        host = parsed.hostname or parsed.netloc
        action = _action_for_method(str(method))
        self._check_permission(PermissionScope.NETWORK, PermissionAction(action), host)

        client = self.framework.http_client
        if client is None:
            raise PluginHttpError("当前环境未提供网络能力")
        try:
            response = client.request(
                str(method).upper(),
                str(url),
                params=dict(params or {}),
                headers=dict(headers or {}) or None,
                json=json,
                data=data,
                timeout=min(max(float(timeout), 0.1), 120.0),
            )
        except Exception as exc:
            raise PluginHttpError(f"网络请求失败: {exc}") from exc

        body = response.content or b""
        truncated = len(body) > PluginNetworkPolicy.max_http_body_bytes
        content = body[: PluginNetworkPolicy.max_http_body_bytes]
        content_type = str(response.headers.get("content-type", "")).lower()
        text = ""
        content_b64 = ""
        if content_type.startswith("text/") or "json" in content_type or not content:
            text = content.decode("utf-8", errors="replace")
        else:
            import base64

            content_b64 = base64.b64encode(content).decode("ascii")
        return PluginHttpResponse(
            status_code=response.status_code,
            url=str(response.url),
            headers={str(key): str(value) for key, value in response.headers.items()},
            text=text,
            content_b64=content_b64,
            truncated=truncated,
        )

    def http_get(self, url: str, **kwargs: Any) -> PluginHttpResponse:
        """
        受控的 GET 请求，参数与 ``http_request`` 一致。
        """
        return self.http_request("GET", url, **kwargs)

    def http_post(self, url: str, **kwargs: Any) -> PluginHttpResponse:
        """
        受控的 POST 请求，参数与 ``http_request`` 一致。
        """
        return self.http_request("POST", url, **kwargs)

    def register_auth_provider(
        self,
        provider_id: str,
        title: str,
        fields: list[AuthField],
        authenticate: Callable[[Mapping[str, Any]], dict[str, Any]],
        resolve_credentials: Callable[[dict[str, Any]], dict[str, str]],
        description: str = "",
    ) -> None:
        """
        注册自定义账户认证提供方。

        ``authenticate`` 接收表单字段值字典并返回账户信息
        （``id``/``alias``/``uuid`` 必填，``data`` 保存不透明令牌负载）；
        ``resolve_credentials`` 在启动时返回
        ``{"player_name", "uuid", "user_type", "access_token", "auth_server"?}``。
        插件禁用或卸载时，宿主会移除该提供方且已存账户无法再启动。

        :param provider_id: 稳定标识，最终账户 ID 为 ``plugin:<provider_id>:<account_id>``
        :param title: 面向用户的显示名称
        :param fields: 动态登录表单字段
        :param authenticate: 认证回调
        :param resolve_credentials: 启动凭据解析回调
        :param description: 可选描述
        """
        self._check_permission(PermissionScope.ACCOUNTS, PermissionAction.WRITE, provider_id)
        registry = self.framework.auth_providers
        if registry is None:
            raise RuntimeError("当前环境未提供账户扩展能力")
        registry.register(
            owner=self.name,
            provider_id=provider_id,
            title=title,
            fields=fields,
            authenticate=authenticate,
            resolve_credentials=resolve_credentials,
            description=description,
        )

    def register_crash_analyzer(
        self,
        name: str,
        enrich: Callable[..., Any],
    ) -> None:
        """
        注册插件崩溃分析回调。

        ``enrich(context, result)`` 返回的字典浅合并进崩溃分析结果；键
        ``reasons`` 追加到已有原因列表。回调异常会被隔离。插件禁用或卸载时
        宿主自动撤销该回调。

        :param name: 稳定的回调标识
        :param enrich: 接收崩溃报告信息和当前分析结果的回调
        """
        self._check_permission(PermissionScope.CRASH, PermissionAction.WRITE, name)
        registry = self.framework.crash_extensions
        if registry is None:
            raise RuntimeError("当前环境未提供崩溃分析扩展能力")
        registry.register(owner=self.name, name=name, enrich=enrich)

    def inject_css(self, css: str, key: str | None = None) -> None:
        """
        向宿主前端注入仅作用于当前插件内容的 CSS 样式。
        :param css: CSS 样式字符串
        :param key: 更新已有样式时使用的稳定标识；不传则追加
        """
        self._check_permission(PermissionScope.UI, PermissionAction.WRITE)
        self.framework.events.emit("plugin:css_injected", self.name, css, key)

    def inject_html(self, slot_id: str, html: str, key: str | None = None, context_key: str | None = None) -> None:
        """
        向宿主前端注入 HTML 片段。
        :param slot_id: 插槽 ID，对应前端 plugin-slot 组件
        :param html: HTML 字符串
        :param key: 更新已有条目时使用的稳定标识；不传则追加
        :param context_key: 只向匹配的重复插槽添加内容；省略时添加到全部插槽
        """
        self._check_permission(PermissionScope.UI, PermissionAction.WRITE)
        self.framework.events.emit("plugin:html_injected", self.name, slot_id, html, key, context_key)

    def inject_script(self, script: str) -> None:
        """
        向宿主前端注入 JavaScript 脚本。
        :param script: JS 代码字符串
        """
        self._check_permission(PermissionScope.UI, PermissionAction.WRITE)
        self.framework.events.emit("plugin:script_injected", self.name, script)

    def inject_typescript(self, script: str) -> None:
        """
        向宿主前端注入 TypeScript 脚本（由前端经 sucrase 转译）。
        :param script: TS 代码字符串
        """
        self._check_permission(PermissionScope.UI, PermissionAction.WRITE)
        self.framework.events.emit("plugin:typescript_injected", self.name, script)

    def register_html_file(self, slot_id: str, file_path: str) -> None:
        """
        读取插件目录下的 HTML 文件注入到指定插槽。
        :param slot_id: 插槽 ID
        :param file_path: 相对于插件目录的文件路径，如 "sidebar.html"
        """
        content = self.load_file(file_path)
        if content:
            self.inject_html(slot_id, content, file_path)

    def register_css_file(self, file_path: str) -> None:
        """
        读取插件目录下的 CSS 文件注入到宿主前端。
        :param file_path: 相对于插件目录的文件路径，如 "style.css"
        """
        content = self.load_file(file_path)
        if content:
            self.inject_css(content, file_path)

    def register_script_file(self, file_path: str) -> None:
        """
        读取插件目录下的 JS 文件注入到宿主前端。
        :param file_path: 相对于插件目录的文件路径，如 "counter.js"
        """
        content = self.load_file(file_path)
        if content:
            self.inject_script(content)

    def register_vue_slot_file(self, slot_id: str, component_name: str, file_path: str) -> None:
        """
        读取插件目录下的 Vue SFC 文件，解析后注册到插槽。
        :param slot_id: 插槽 ID
        :param component_name: 全局唯一组件名
        :param file_path: 相对于插件目录的 .vue 文件路径，如 "widget.vue"
        """
        content = self.load_file(file_path)
        if content is None:
            return
        parts = self._parse_vue_sfc(content)
        self.register_vue_slot(slot_id, component_name, **parts)

    def register_vue_route_file(
        self, path: str, title: str, component_name: str, file_path: str, icon: str = ""
    ) -> None:
        """
        读取插件目录下的 Vue SFC 文件，解析后注册为独立路由页面。
        :param path: 路由路径
        :param title: 侧边栏显示名称
        :param component_name: 全局唯一组件名
        :param file_path: 相对于插件目录的 .vue 文件路径，如 "page.vue"
        :param icon: 图标名
        """
        content = self.load_file(file_path)
        if content is None:
            return
        parts = self._parse_vue_sfc(content)
        self.register_vue_route(path, title, component_name, icon=icon, **parts)

    def register_vue_slot(
        self,
        slot_id: str,
        component_name: str,
        template: str,
        script: str = "",
        style: str = "",
        context_key: str | None = None,
    ) -> None:
        """
        注册 Vue 组件到指定插槽，前端收到后动态创建并挂载。
        :param slot_id: 插槽 ID
        :param component_name: 全局唯一组件名
        :param template: Vue 模板 HTML
        :param script: 组件选项 JS 代码（data/methods/computed 等，不含 export default）
        :param style: 组件 scoped CSS
        :param context_key: 可选的重复插槽标识
        """
        self.framework.events.emit(
            "plugin:vue_slot_registered", self.name, slot_id, component_name, template, script, style, context_key
        )

    def register_vue_route(
        self,
        path: str,
        title: str,
        component_name: str,
        template: str,
        script: str = "",
        style: str = "",
        icon: str = "",
    ) -> None:
        """
        注册 Vue 组件作为独立路由页面。
        :param path: 路由路径，如 "/crash-reports"
        :param title: 侧边栏显示名称
        :param component_name: Vue 组件名（全局唯一）
        :param template: Vue 模板 HTML
        :param script: 组件选项 JS 代码
        :param style: 组件 scoped CSS
        :param icon: 图标名
        """
        self._check_permission(PermissionScope.UI, PermissionAction.WRITE)
        self.framework._register_vue_route(self, path, title, icon, component_name, template, script, style)

    def emit(self, event: str, payload: Any = None) -> None:
        """
        向全局事件总线发射事件，其他插件或宿主可订阅。
        :param event: 事件名称，格式建议 "模块:动作"，如 "crash:detected"
        :param payload: 事件负载数据
        """
        self._check_permission(PermissionScope.EVENTS, PermissionAction.EMIT, event)
        self.framework.events.emit(event, payload)

    def subscribe(self, event: str, handler: Callable) -> None:
        """
        订阅全局事件；由插件管理器统一注册，自动以插件名作为所有者。
        :param event: 事件名称
        :param handler: 回调函数
        """
        self._check_permission(PermissionScope.EVENTS, PermissionAction.SUBSCRIBE, event)
        self.framework.subscribe_event(self, event, handler)

    def load_file(self, relative_path: str, encoding: str = "utf-8") -> str | None:
        """
        读取插件目录下的文本文件。
        :param relative_path: 相对于插件根目录的文件路径，如 "style.css"
        :param encoding: 文件编码
        :return: 文件内容，读取失败返回 None
        """
        self._check_permission(PermissionScope.FILESYSTEM, PermissionAction.READ, relative_path)
        path = self._resolve_inside_plugin_dir(self.plugin_dir, relative_path)
        if not path.is_file():
            return None
        return path.read_text(encoding=encoding)

    def resource_path(self, relative_path: str) -> Path:
        """
        获取插件 resources/ 目录下的文件路径。

        :param relative_path: 相对于插件根目录的路径
        :return: resources/ 目录下的绝对路径
        """
        return self.plugin_dir / "resources" / relative_path

    def load_resource(self, relative_path: str, encoding: str = "utf-8") -> str | None:
        """
        读取插件 resources/ 目录下的文本文件。
        :param relative_path: 相对路径
        :param encoding: 文件编码
        :return: 文件内容，读取失败返回 None
        """
        self._check_permission(PermissionScope.FILESYSTEM, PermissionAction.READ, relative_path)
        path = self._resolve_inside_plugin_dir(self.plugin_dir / "resources", relative_path)
        if not path.is_file():
            return None
        return path.read_text(encoding=encoding)

    @staticmethod
    def _resolve_inside_plugin_dir(base_dir: Path, relative_path: str) -> Path:
        # 将相对路径解析到基目录内部，拒绝绝对路径与 .. 越界。
        base = base_dir.resolve(strict=False)
        normalized = str(relative_path or "").strip()
        candidate = Path(normalized)
        if not normalized or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"插件文件路径越界: {relative_path}")
        target = (base / candidate).resolve(strict=False)
        if target != base and base not in target.parents:
            raise ValueError(f"插件文件路径越界: {relative_path}")
        return target

    @staticmethod
    def _parse_vue_sfc(content: str) -> dict[str, str]:
        # 解析 .vue 单文件组件内容，拆分为 template/script/style 三部分。
        result: dict[str, str] = {"template": "", "script": "", "style": ""}
        for tag in ("template", "script", "style"):
            match = re.search(rf"<{tag}.*?>(.*?)</{tag}>", content, re.DOTALL)
            if match:
                result[tag] = match.group(1).strip()
        return result
