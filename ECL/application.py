# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：应用装配根：构建服务依赖图并管理关闭顺序。
#
# 公开接口：
#   - class ApplicationState — 保存一次应用运行期间会变化的后端状态。
#   - class ApplicationContext — 显式保存后端依赖图，并统一管理共享资源的生命周期。
#       - close() -> None — 按依赖逆序关闭后端资源，并清空事件订阅。
#   - create_application(runtime_info, *, launch_options=None, on_state_ready=None) -> ApplicationContext
#     — 构造一次应用运行所需的完整后端依赖图。
# ============================================================

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import os
import ssl
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any
from urllib.request import getproxies

import httpx

from ECL.cli import LaunchOptions, apply_launch_overrides
from ECL.common import BuildEnvironment, __version__, __version_type__
from ECL.common.runtime import RuntimeInfo
from ECL.events import EventBus
from ECL.game import InstancesManager
from ECL.java import JavaManager
from ECL.java.models import JavaReferenceConfig
from ECL.plugins import PluginManager
from ECL.services.accounts import AccountManager
from ECL.services.background_media import BackgroundMediaService
from ECL.services.custom_downloads import CustomDownloadService
from ECL.services.dev_channel import DevChannelService
from ECL.services.game import GameService
from ECL.services.game.launch_settings import scan_instance_java_references
from ECL.services.info_card import InfoCardManager
from ECL.services.operations import OperationManager
from ECL.services.processes import ProcessService
from ECL.services.single_instance import SingleInstanceService
from ECL.services.updates import StartupUpdateService
from ECL.services.wardrobe import WardrobeStore
from ECL.utils import ConfigStore, Environment

if TYPE_CHECKING:
    from ECL.services.connector import ConnectorService

logger = logging.getLogger("EuoraCraft-Launcher.Application")


def _apply_ssl_verify(ssl_context: ssl.SSLContext, verify: bool) -> None:
    # 设置 SSL 上下文的证书校验开关，供共享 HTTP 客户端运行时热切换。
    if verify:
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = True
    else:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE


def _network_timeout(value: Any) -> float:
    # 将用户设置限制为合理的 HTTP 总超时范围。
    try:
        return min(120.0, max(1.0, float(value)))
    except (TypeError, ValueError):
        return 15.0


def _network_retries(value: Any) -> int:
    # 将用户设置限制为有限的额外重试次数。
    try:
        return min(5, max(0, int(value)))
    except (TypeError, ValueError):
        return 2


def _http_transport(launcher_config: Mapping[str, Any]) -> httpx.HTTPTransport:
    """按启动器网络配置构造启动器通道的传输层。

    启动器通道（账户登录、元数据等）使用 api_proxy_* 配置，与游戏下载代理
    （proxy_mode/proxy_url，经 ECL_DOWNLOAD_PROXY 下发）相互独立。

    :param launcher_config: launcher 配置分区，提供代理模式与重试次数
    :return: 配置好代理与重试的 HTTP 传输层
    """
    proxy_mode = _proxy_mode(launcher_config, mode_key="api_proxy_mode")
    proxy_url = _resolve_proxy_url(proxy_mode, str(launcher_config.get("api_proxy_url") or "").strip())
    return httpx.HTTPTransport(
        retries=_network_retries(launcher_config.get("request_retries", 2)),
        proxy=httpx.Proxy(url=proxy_url) if proxy_url else None,
    )


def _sync_download_proxy_env(launcher_config: Mapping[str, Any]) -> None:
    """将游戏下载代理同步到进程环境变量 ECL_DOWNLOAD_PROXY。

    下载代理沿用 proxy_mode/proxy_url 配置，与启动器网络代理（api_proxy_*）相互独立；
    主仓库下载类请求与 ECL/game 子模块下载器都从该变量读取代理地址，
    账户登录等启动器通道客户端 trust_env=False 不受影响。

    :param launcher_config: launcher 配置分区
    """
    proxy_mode = _proxy_mode(launcher_config)
    if proxy_mode == "none":
        # httpx 默认读取系统/环境代理，代理异常时所有请求都会失败，
        # 统一置 NO_PROXY=* 让全部网络请求直连，规避坏代理的影响。
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        os.environ.pop("ECL_DOWNLOAD_PROXY", None)
        return
    # 仅清除本应用此前设置的 NO_PROXY=*，不覆盖用户自行配置的排除列表
    if os.environ.get("NO_PROXY") == "*":
        os.environ["NO_PROXY"] = ""
    if os.environ.get("no_proxy") == "*":
        os.environ["no_proxy"] = ""
    proxy_url = _resolve_proxy_url(proxy_mode, str(launcher_config.get("proxy_url") or "").strip())
    if proxy_url:
        os.environ["ECL_DOWNLOAD_PROXY"] = proxy_url
    else:
        os.environ.pop("ECL_DOWNLOAD_PROXY", None)


def _apply_http_network_settings(client: httpx.Client, launcher_config: Mapping[str, Any]) -> None:
    """将代理/超时/重试热应用到已存在的共享客户端，配置变化无需重启启动器。

    :param client: 共享 HTTP 客户端
    :param launcher_config: launcher 配置分区
    """
    request_timeout = _network_timeout(launcher_config.get("request_timeout", 15))
    client.timeout = httpx.Timeout(request_timeout, connect=min(10.0, request_timeout))
    old_transport = getattr(client, "_transport", None)
    # httpx 未提供传输层替换的公开 API；超时走公开 setter，传输层只能整体换新并关闭旧连接池。
    client._transport = _http_transport(launcher_config)
    if old_transport is not None:
        close = getattr(old_transport, "close", None)
        if callable(close):
            close()


class NetworkProxyPolicy:
    """
    启动器网络代理模式的允许值。

    代理配置解析和共享 HTTP 客户端热更新均依赖此策略。
    """

    modes = ("none", "system", "custom")


def _proxy_mode(launcher_config: Mapping[str, Any], mode_key: str = "proxy_mode") -> str:
    # 读取代理模式；proxy_mode 兼容旧版 ignore_proxy 布尔配置（true→直连、false→系统代理）。
    mode = launcher_config.get(mode_key)
    if mode in NetworkProxyPolicy.modes:
        return mode
    if mode_key != "proxy_mode":
        return "none"
    return "none" if launcher_config.get("ignore_proxy", True) else "system"


def _resolve_proxy_url(proxy_mode: str, custom_url: str) -> str | None:
    # 解析当前代理模式下应使用的代理地址，None 表示直连。
    if proxy_mode == "custom":
        return custom_url or None
    if proxy_mode == "system":
        # 读取系统/环境代理，Windows 下同时覆盖 Internet 选项与代理环境变量。
        for key in ("all", "http", "https"):
            url = getproxies().get(key)
            if url and not url.lower().startswith("socks"):
                return url
    return None


def _build_local_player_icon_provider(
    account_manager: AccountManager, http_client: httpx.Client
) -> Callable[[], str | None]:
    # 构造本机玩家完整皮肤的 base64 解析器，供联机头像上传复用。
    cache: dict[str, str | None] = {}
    cache_lock = RLock()

    def resolve() -> str | None:
        account = account_manager.current_account()
        if account is None:
            return None
        account_id = str(account.get("id") or "")
        if not account_id:
            return None
        with cache_lock:
            if account_id in cache:
                return cache[account_id]
            try:
                skin_url = account_manager.texture_urls(account_id).get("skinUrl")
            except Exception:
                skin_url = None
            icon: str | None = None
            if skin_url and skin_url.startswith("data:image/"):
                icon = skin_url.split(",", 1)[1] if "," in skin_url else None
            elif skin_url:
                try:
                    with http_client.stream("GET", skin_url) as response:
                        response.raise_for_status()
                        data = b"".join(response.iter_bytes(64 * 1024))
                    if data:
                        icon = base64.b64encode(data).decode("ascii")
                except Exception:
                    icon = None
            cache[account_id] = icon
            return icon

    return resolve


@dataclass
class ApplicationState:
    """
    保存一次应用运行期间会变化的后端状态。

    :param app_path: 启动器数据与运行文件所在目录
    :param resource_path: 打包资源或源码资源所在目录
    :param data_path: 后端持久化数据目录
    :param is_frozen: 当前是否运行于打包后的可执行文件
    :param launch_options: 命令行启动参数；会话内粘滞覆盖配置，不写入存盘配置
    :param active_window_chrome: 主窗口创建时固定的有效窗口模式
    """

    app_path: Path
    resource_path: Path
    data_path: Path
    is_frozen: bool
    launcher_version: str = __version__
    launcher_version_type: str = __version_type__
    debug: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    launch_options: LaunchOptions | None = None
    active_window_chrome: str = "custom"


@dataclass(frozen=True)
class ApplicationContext:
    """
    显式保存后端依赖图，并统一管理共享资源的生命周期。

    上下文中的服务按字段顺序构造，关闭时按依赖的逆序释放。调用 ``close`` 多次不会
    重复关闭网络客户端或后台任务。
    """

    state: ApplicationState
    events: EventBus
    config: ConfigStore
    environment: Environment
    http: httpx.Client
    accounts: AccountManager
    wardrobe: WardrobeStore
    info_card: InfoCardManager
    game: GameService
    connector: ConnectorService
    plugins: PluginManager
    processes: ProcessService
    operations: OperationManager | None = None
    downloads: CustomDownloadService | None = None
    java: JavaManager | None = None
    background_media: BackgroundMediaService | None = None
    startup_update: StartupUpdateService | None = None
    single_instance: SingleInstanceService | None = None  # 按配置启动的单实例互斥监听
    dev_channel: DevChannelService | None = None  # 按需启动的开发者通道，未开启时为 None
    _closed: bool = field(default=False, init=False, repr=False, compare=False)
    _close_lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def close(self) -> None:
        """
        按依赖逆序关闭后端资源，并清空事件订阅。
        """
        with self._close_lock:
            if self._closed:
                logger.debug("忽略重复的后台服务关闭请求")
                return
            object.__setattr__(self, "_closed", True)
            logger.debug("开始关闭后台服务")
            # 开发者通道先于插件关闭，避免通道继续处理请求时依赖已被释放。
            self.events.emit("launcher:closing", None)
            resources: tuple[Any, ...] = (
                self.single_instance,
                self.dev_channel,
                self.plugins,
                self.processes,
                self.game,
                self.java,
                self.connector,
                self.operations,
                self.accounts,
                self.background_media,
                self.startup_update,
                self.http,
            )
            for resource in resources:
                if resource is None:
                    continue
                try:
                    close = resource.close
                    if inspect.iscoroutinefunction(close):
                        # 账户等资源的异步关闭需要在独立事件循环中完成
                        asyncio.run(close())
                    else:
                        close()
                except Exception:
                    logger.exception("关闭后端资源失败: %s", type(resource).__name__)
            self.events.clear()
            logger.debug("后台服务已关闭")


def create_application(
    runtime_info: RuntimeInfo,
    *,
    launch_options: LaunchOptions | None = None,
    on_state_ready: Callable[[ApplicationState], None] | None = None,
) -> ApplicationContext:
    """
    构造一次应用运行所需的完整后端依赖图。

    初始化中途失败时，本函数会按逆序释放已经创建的资源，再将原始异常抛给启动器。

    :param runtime_info: 运行目录、资源目录和打包状态
    :param launch_options: 命令行启动参数；在环境变量覆盖之后叠加，会话内粘滞
    :param on_state_ready: 配置读取后、服务构造前的可选回调，用于提前应用日志级别
    :return: 负责后端依赖与资源生命周期的应用上下文
    """
    state = ApplicationState(
        app_path=runtime_info["app_path"],
        resource_path=runtime_info["resource_path"],
        data_path=runtime_info["data_path"],
        is_frozen=runtime_info["is_frozen"],
        launch_options=launch_options,
    )
    events = EventBus()
    environment = Environment(state.app_path)
    config = ConfigStore(state.data_path, events)
    state.config = apply_launch_overrides(environment.apply_to_config(config.get_config()), launch_options)
    state.debug = bool((state.config.get("launcher") or {}).get("debug"))
    if on_state_ready is not None:
        on_state_ready(state)
    logger.debug(
        "正在初始化后端服务：数据目录：%s；打包运行：%s；调试模式：%s",
        state.data_path,
        ("是" if state.is_frozen else "否"),
        ("是" if state.debug else "否"),
    )

    created: list[Any] = []
    try:
        logger.debug("正在创建共享 HTTP 客户端")
        launcher_config = state.config.get("launcher") or {}
        # 单实例监听最先创建（关闭时最后释放），写入发现文件供后续启动进程探测。
        single_instance: SingleInstanceService | None = None
        if bool(launcher_config.get("single_instance", True)):
            logger.debug("正在启动单实例监听")
            single_instance = SingleInstanceService(
                events=events,
                data_path=state.data_path,
                launcher_version=state.launcher_version,
            )
            created.append(single_instance)
            single_instance.start()
        disable_ssl_verify = bool(launcher_config.get("disable_ssl_verify", False))
        request_timeout = _network_timeout(launcher_config.get("request_timeout", 15))
        request_retries = _network_retries(launcher_config.get("request_retries", 2))
        ssl_verify_context = ssl.create_default_context()
        _apply_ssl_verify(ssl_verify_context, not disable_ssl_verify)
        _sync_download_proxy_env(launcher_config)
        http = httpx.Client(
            timeout=httpx.Timeout(request_timeout, connect=min(10.0, request_timeout)),
            follow_redirects=True,
            headers={"User-Agent": "EuoraCraft-Launcher"},
            verify=ssl_verify_context,
            # 启动器通道固定不读代理环境变量，账户登录等请求不受下载代理影响
            trust_env=False,
            transport=_http_transport(launcher_config),
        )
        created.append(http)
        logger.debug("共享 HTTP 客户端已创建")

        background_media = BackgroundMediaService()
        created.append(background_media)

        startup_update = StartupUpdateService(
            http,
            events,
            current_version=state.launcher_version,
            version_type=state.launcher_version_type,
        )
        created.append(startup_update)

        logger.info("正在初始化账户服务")
        accounts = AccountManager(
            state.data_path,
            microsoft_client_id=environment.get_value("MICROSOFT_CLIENT_ID"),
            event_bus=events,
            disable_ssl_verify=disable_ssl_verify,
            resource_path=state.resource_path,
            # 离线/插件账户状态与微软、外置令牌统一存放于用户目录下的账户根目录。
            state_dir=Path.home() / ".ECL" / "accounts",
        )
        created.append(accounts)
        logger.info(
            "账户服务初始化完成，Microsoft 登录可用：%s",
            ("是" if accounts.microsoft_login_config()["available"] else "否"),
        )

        wardrobe = WardrobeStore(state.data_path)
        logger.debug(
            "本地衣柜已创建，条目数：%s",
            len(wardrobe.list_items()),
        )
        info_card = InfoCardManager(
            state.data_path,
            http_client=http,
            request_timeout=request_timeout,
            request_retries=request_retries,
        )

        logger.info("正在初始化游戏服务")
        # 共享进程管理器，使实例终端能同时展示插件与 Minecraft 实例的输出。
        shared_instances = InstancesManager()
        operations = OperationManager(state.data_path, events)
        created.append(operations)
        downloads = CustomDownloadService(operations, data_path=state.data_path)
        java = JavaManager(
            state.data_path,
            http,
            operations,
            events,
            config_provider=lambda: JavaReferenceConfig.model_validate(config.get_config("game") or {}),
            reference_provider=scan_instance_java_references,
        )
        created.append(java)
        game = GameService(
            accounts,
            data_path=state.data_path,
            resource_path=state.resource_path,
            curseforge_api_key=environment.get_value("CURSEFORGE_API_KEY")
            or BuildEnvironment.curseforge_api_key
            or None,
            event_bus=events,
            isolation_policy_provider=lambda: (config.get_config("game") or {}).get("instance_isolation_policy"),
            instances_manager=shared_instances,
            operations=operations,
            java_manager=java,
        )
        created.append(game)
        logger.info("游戏服务初始化完成")

        logger.debug("正在初始化联机服务 ConnectorService")
        from ECL.plugins.connector import ConnectorExtensionRegistry
        from ECL.services.connector import ConnectorService
        from ECL.services.connector_nodes import ConnectorNodeConfig

        current_account = accounts.current_account()
        connector_extensions = ConnectorExtensionRegistry()
        connector = ConnectorService(
            player_name=(current_account or {}).get("alias") or "Player",
            extensions=connector_extensions,
            local_player_icon_provider=_build_local_player_icon_provider(accounts, http),
            http_client=http,
            node_settings_provider=lambda: ConnectorNodeConfig.model_validate(config.get_config("connector") or {}),
        )
        logger.debug(
            "联机服务状态：可用：%s；EasyTier可用：%s；EasyTier版本：%s",
            ("是" if connector.available else "否"),
            ("是" if connector.easytier_available else "否"),
            connector.easytier_version,
        )
        created.append(connector)
        logger.debug("联机服务 ConnectorService 已初始化")

        logger.debug("正在初始化子进程实例服务")
        processes = ProcessService(event_bus=events, instances_manager=shared_instances)
        created.append(processes)
        logger.debug("子进程实例服务已初始化")

        plugins = PluginManager(
            events,
            processes=processes,
            instance_compatibility=game.instance_compatibility,
            connector_extensions=connector_extensions,
            launch_hooks=game.launch_hooks,
            http_client=http,
            auth_providers=accounts.plugin_auth_providers,
            crash_extensions=game.crash_extensions,
        )
        created.append(plugins)
        plugins.initialize(
            state.data_path,
            state.resource_path,
            auto_enable=not (launch_options is not None and launch_options.disable_plugins),
        )
        logger.debug("插件管理器已初始化")

        dev_channel: DevChannelService | None = None
        if bool(launcher_config.get("dev_channel", False)):
            logger.info("正在启动开发者通道")
            dev_channel = DevChannelService(
                plugins=plugins,
                events=events,
                data_path=state.data_path,
                launcher_version=__version__,
                debug=state.debug,
                frontend_dist=state.resource_path / "frontend" / "dist",
            )
            dev_channel.start()
            created.append(dev_channel)
    except Exception:
        logger.exception("后端服务初始化失败，正在释放已创建的资源")
        for resource in reversed(created):
            close = getattr(resource, "close", None)
            if callable(close):
                with suppress(Exception):
                    if inspect.iscoroutinefunction(close):
                        # 账户等资源的异步关闭需要在独立事件循环中完成
                        asyncio.run(close())
                    else:
                        close()
        raise

    context = ApplicationContext(
        state=state,
        events=events,
        config=config,
        environment=environment,
        http=http,
        accounts=accounts,
        wardrobe=wardrobe,
        info_card=info_card,
        game=game,
        connector=connector,
        plugins=plugins,
        processes=processes,
        operations=operations,
        downloads=downloads,
        java=java,
        background_media=background_media,
        startup_update=startup_update,
        single_instance=single_instance,
        dev_channel=dev_channel,
    )

    if dev_channel is not None:
        from ECL.api import FrontendApi
        from ECL.api.registry import command_handlers

        dev_channel.install_frontend_handlers(command_handlers(FrontendApi(context)))

    def update_runtime_config(section: str, data: Any) -> None:
        """
        在启动器设置变化后刷新运行状态和日志级别。

        :param section: 被修改的配置分区
        :param data: 分区更新后的配置数据
        """
        if section != "launcher":
            return
        launcher_config = data if isinstance(data, Mapping) else state.config.get("launcher") or {}
        # 重新叠加命令行覆盖，使会话内粘滞的启动参数不被设置界面的任意改动清除。
        state.config = apply_launch_overrides(environment.apply_to_config(config.get_config()), state.launch_options)
        # 调试标记跟随叠加覆盖后的生效配置，与初始化阶段的派生逻辑保持一致。
        state.debug = bool((state.config.get("launcher") or {}).get("debug"))
        _apply_ssl_verify(
            ssl_verify_context,
            not bool((data or {}).get("disable_ssl_verify", False)),
        )
        _sync_download_proxy_env(launcher_config)
        _apply_http_network_settings(http, launcher_config)
        logger.debug("运行配置已刷新：调试模式：%s", ("是" if state.debug else "否"))

    events.subscribe("config:updated", update_runtime_config)
    startup_update.start()
    logger.info("后端服务初始化完成")
    return context


__all__ = ["ApplicationContext", "ApplicationState", "create_application"]
