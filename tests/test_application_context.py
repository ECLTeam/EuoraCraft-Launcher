# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 application_context 模块的自动化测试。
#
# 公开接口：
#   - class Closable
#       - close() -> None
#   - test_application_context_closes_resources_in_reverse_dependency_order() -> None
#   - test_event_subscription_returns_unsubscribe_callback() -> None
#   - test_event_handler_failure_does_not_block_other_handlers() -> None
#   - test_composition_failure_closes_resources_already_created(monkeypatch, tmp_path) -> None
#   - test_composition_reports_loaded_state_before_services_are_created(monkeypatch, tmp_path) -> None
#   - test_proxy_mode_migrates_legacy_ignore_proxy() -> None
#   - test_resolve_proxy_url_system_mode_reads_system_proxy(monkeypatch) -> None
#   - test_resolve_proxy_url_skips_socks_and_missing_proxy(monkeypatch) -> None
#   - test_resolve_proxy_url_custom_and_none_modes() -> None
#   - test_none_proxy_mode_sets_no_proxy_env(monkeypatch, tmp_path) -> None
#   - test_system_proxy_mode_does_not_force_no_proxy(monkeypatch, tmp_path) -> None
#   - test_composition_applies_launch_options_sticky_overrides(monkeypatch, tmp_path) -> None
# ============================================================

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ECL.application as application_module
import ECL.services.connector as connector_service_module
from ECL.application import ApplicationContext, create_application
from ECL.cli import LaunchOptions
from ECL.events import EventBus


class Closable:
    def __init__(self, name: str, order: list[str]) -> None:
        self.name = name
        self.order = order

    def close(self) -> None:
        self.order.append(self.name)


class _FakeHttp:
    def __init__(self, **_kwargs) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeAccounts:
    plugin_auth_providers = SimpleNamespace()

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def microsoft_login_config(self) -> dict:
        return {"available": False}

    def current_account(self) -> None:
        return None

    def close(self) -> None:
        pass


class _FakeStartupUpdate:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeGame:
    def __init__(self, *_args, **_kwargs) -> None:
        self.instance_compatibility = SimpleNamespace()
        self.launch_hooks = SimpleNamespace()
        self.crash_extensions = SimpleNamespace()

    def close(self) -> None:
        pass


class _FakeConnector:
    available = True
    easytier_available = False
    easytier_version = ""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePluginManager:
    def __init__(self, *_args, **_kwargs) -> None:
        self.init_kwargs: dict[str, Any] = {}

    def initialize(self, *_args, **kwargs) -> None:
        self.init_kwargs = kwargs

    def close(self) -> None:
        pass


class _FakeDevChannel:
    def __init__(self, **_kwargs) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def install_frontend_handlers(self, _handlers: Any) -> None:
        pass

    def close(self) -> None:
        pass


def test_application_context_closes_resources_in_reverse_dependency_order() -> None:
    order: list[str] = []
    events = EventBus()
    events.subscribe("event", lambda: None)
    context = ApplicationContext(
        state=SimpleNamespace(),
        events=events,
        config=SimpleNamespace(),
        environment=SimpleNamespace(),
        http=Closable("http", order),
        accounts=Closable("accounts", order),
        wardrobe=SimpleNamespace(),
        info_card=SimpleNamespace(),
        game=Closable("game", order),
        connector=SimpleNamespace(),
        plugins=Closable("plugins", order),
        processes=Closable("processes", order),
    )

    context.close()
    context.close()

    assert order == ["plugins", "processes", "game", "accounts", "http"]
    assert not events._handlers


def test_event_subscription_returns_unsubscribe_callback() -> None:
    events = EventBus()
    received: list[int] = []
    unsubscribe = events.subscribe("number", received.append)

    events.emit("number", 1)
    unsubscribe()
    events.emit("number", 2)

    assert received == [1]


def test_event_handler_failure_does_not_block_other_handlers() -> None:
    events = EventBus()
    received: list[str] = []

    def fail() -> None:
        raise RuntimeError("boom")

    events.subscribe("event", fail)
    events.subscribe("event", lambda: received.append("ok"))

    events.emit("event")

    assert received == ["ok"]


def test_composition_failure_closes_resources_already_created(monkeypatch, tmp_path: Path) -> None:
    closed: list[str] = []

    class FakeHttp:
        def __init__(self, **_kwargs) -> None:
            pass

        def close(self) -> None:
            closed.append("http")

    class FailingAccounts:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("account initialization failed")

    monkeypatch.setattr(application_module.httpx, "Client", FakeHttp)
    monkeypatch.setattr(application_module, "AccountManager", FailingAccounts)

    with pytest.raises(RuntimeError, match="account initialization failed"):
        create_application(
            {
                "app_path": tmp_path,
                "resource_path": tmp_path,
                "data_path": tmp_path / "ECL_data",
                "is_frozen": False,
            }
        )

    assert closed == ["http"]


def test_composition_reports_loaded_state_before_services_are_created(monkeypatch, tmp_path: Path) -> None:
    observed: list[tuple[bool, bool]] = []

    class FakeConfigStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_config(self) -> dict:
            return {"launcher": {"debug": True}}

    class FakeEnvironment:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def apply_to_config(self, config: dict) -> dict:
            return config

        def get_value(self, _key: str) -> None:
            return None

    class StopComposition:
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("stop after state callback")

    monkeypatch.setattr(application_module, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(application_module, "Environment", FakeEnvironment)
    monkeypatch.setattr(application_module.httpx, "Client", StopComposition)

    with pytest.raises(RuntimeError, match="stop after state callback"):
        create_application(
            {
                "app_path": tmp_path,
                "resource_path": tmp_path,
                "data_path": tmp_path / "ECL_data",
                "is_frozen": False,
            },
            on_state_ready=lambda state: observed.append((state.debug, bool(state.config))),
        )

    assert observed == [(True, True)]


def test_proxy_mode_migrates_legacy_ignore_proxy() -> None:
    proxy_mode = application_module._proxy_mode
    assert proxy_mode({"proxy_mode": "none"}) == "none"
    assert proxy_mode({"proxy_mode": "system"}) == "system"
    assert proxy_mode({"proxy_mode": "custom"}) == "custom"
    assert proxy_mode({"ignore_proxy": True}) == "none"
    assert proxy_mode({"ignore_proxy": False}) == "system"
    assert proxy_mode({}) == "none"
    # 新配置优先于旧字段
    assert proxy_mode({"proxy_mode": "custom", "ignore_proxy": True}) == "custom"


def test_resolve_proxy_url_system_mode_reads_system_proxy(monkeypatch) -> None:
    monkeypatch.setattr(
        application_module,
        "getproxies",
        lambda: {"http": "http://proxy.example:8080", "https": "http://proxy.example:8443"},
    )
    assert application_module._resolve_proxy_url("system", "") == "http://proxy.example:8080"
    # 自定义地址不影响 system 模式
    assert application_module._resolve_proxy_url("system", "http://custom:9") == "http://proxy.example:8080"


def test_resolve_proxy_url_skips_socks_and_missing_proxy(monkeypatch) -> None:
    monkeypatch.setattr(
        application_module,
        "getproxies",
        lambda: {"http": "socks5://127.0.0.1:1080", "https": "http://https-proxy:3128"},
    )
    assert application_module._resolve_proxy_url("system", "") == "http://https-proxy:3128"
    monkeypatch.setattr(application_module, "getproxies", lambda: {})
    assert application_module._resolve_proxy_url("system", "") is None


def test_resolve_proxy_url_custom_and_none_modes() -> None:
    assert application_module._resolve_proxy_url("custom", "http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert application_module._resolve_proxy_url("custom", "") is None
    assert application_module._resolve_proxy_url("none", "") is None
    assert application_module._resolve_proxy_url("none", "http://ignored:1") is None


def test_none_proxy_mode_sets_no_proxy_env(monkeypatch, tmp_path: Path) -> None:
    class FakeConfigStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_config(self) -> dict:
            return {"launcher": {"proxy_mode": "none"}}

    class FakeEnvironment:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def apply_to_config(self, config: dict) -> dict:
            return config

        def get_value(self, _key: str) -> None:
            return None

    class CaptureHttp:
        def __init__(self, **kwargs) -> None:
            self.transport = kwargs.get("transport")
            raise RuntimeError("stop after http client")

    monkeypatch.setattr(application_module, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(application_module, "Environment", FakeEnvironment)
    monkeypatch.setattr(application_module.httpx, "Client", CaptureHttp)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with pytest.raises(RuntimeError, match="stop after http client"):
        create_application(
            {
                "app_path": tmp_path,
                "resource_path": tmp_path,
                "data_path": tmp_path / "ECL_data",
                "is_frozen": False,
            }
        )

    assert os.environ.get("NO_PROXY") == "*"
    assert os.environ.get("no_proxy") == "*"


def test_system_proxy_mode_does_not_force_no_proxy(monkeypatch, tmp_path: Path) -> None:
    class FakeConfigStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_config(self) -> dict:
            return {"launcher": {"proxy_mode": "system"}}

    class FakeEnvironment:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def apply_to_config(self, config: dict) -> dict:
            return config

        def get_value(self, _key: str) -> None:
            return None

    class CaptureHttp:
        def __init__(self, **kwargs) -> None:
            self.transport = kwargs.get("transport")
            raise RuntimeError("stop after http client")

    monkeypatch.setattr(application_module, "ConfigStore", FakeConfigStore)
    monkeypatch.setattr(application_module, "Environment", FakeEnvironment)
    monkeypatch.setattr(application_module.httpx, "Client", CaptureHttp)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    # create_application 在 system 模式会写入游戏下载代理；通过 monkeypatch
    # 注册清理，避免该进程级状态泄漏给后续下载器测试。
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)

    with pytest.raises(RuntimeError, match="stop after http client"):
        create_application(
            {
                "app_path": tmp_path,
                "resource_path": tmp_path,
                "data_path": tmp_path / "ECL_data",
                "is_frozen": False,
            }
        )

    # system 模式不应主动把 NO_PROXY 置为 *（保持系统代理可用）
    assert os.environ.get("NO_PROXY") == ""
    assert os.environ.get("no_proxy") == ""


def test_composition_applies_launch_options_sticky_overrides(monkeypatch, tmp_path: Path) -> None:
    """命令行启动参数应叠加到生效配置、关闭插件自动启用，并在配置更新后保持粘滞。"""
    monkeypatch.setattr(application_module.httpx, "Client", _FakeHttp)
    monkeypatch.setattr(application_module, "AccountManager", _FakeAccounts)
    monkeypatch.setattr(application_module, "StartupUpdateService", _FakeStartupUpdate)
    monkeypatch.setattr(application_module, "GameService", _FakeGame)
    monkeypatch.setattr(application_module, "PluginManager", _FakePluginManager)
    monkeypatch.setattr(application_module, "DevChannelService", _FakeDevChannel)
    monkeypatch.setattr(connector_service_module, "ConnectorService", _FakeConnector)
    # 环境变量声明 debug=false、日志级别 error，命令行参数应优先于二者
    monkeypatch.setenv("ECL_CONFIG_LAUNCHER_DEBUG", "false")
    monkeypatch.setenv("ECL_CONFIG_LAUNCHER_DEBUG_LOG_LEVEL", "error")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    options = LaunchOptions(
        debug=True,
        log_level="debug",
        enable_dev_channel=True,
        disable_plugins=True,
        frontend_dist="http://localhost:5173",
    )
    context = create_application(
        {
            "app_path": tmp_path,
            "resource_path": tmp_path,
            "data_path": tmp_path / "ECL_data",
            "is_frozen": False,
        },
        launch_options=options,
    )
    try:
        assert context.state.launch_options is options
        assert context.state.config["launcher"]["debug"] is True
        assert context.state.config["launcher"]["debug_log_level"] == "debug"
        assert context.state.config["launcher"]["dev_channel"] is True
        assert context.state.config["tauri"]["frontenddist"] == "http://localhost:5173"
        assert context.state.debug is True
        plugins = context.plugins
        assert plugins.init_kwargs["auto_enable"] is False
        assert isinstance(context.dev_channel, _FakeDevChannel)
        assert context.dev_channel.started is True

        # 任意设置变更后，会话级覆盖不应被清除（粘滞语义）
        context.events.emit("config:updated", "launcher", {"debug": False})
        assert context.state.config["launcher"]["debug"] is True
        assert context.state.debug is True
    finally:
        context.close()
