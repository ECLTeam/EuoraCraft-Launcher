# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：Tauri 宿主适配器：封装宿主侧回调与窗口环境访问。
#
# 公开接口：
#   - class Adapter — PyTauri 前端适配器，负责注册 IPC 命令并启动 Tauri 应用。
#       - run() -> None — 启动 Tauri 前端。
# ============================================================

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from anyio.from_thread import start_blocking_portal
from pytauri import AppHandle, Commands, Manager, RunEvent, RunEventType
from pytauri_plugins.dialog import init as dialog_init
from pytauri_wheel.lib import builder_factory, context_factory

from ECL.api import FrontendApi
from ECL.api.registry import command_handlers
from ECL.application import ApplicationContext
from ECL.utils import get_logger

from .frontend_events import subscribe_all_frontend_events


class Adapter:
    """
    PyTauri 前端适配器，负责注册 IPC 命令并启动 Tauri 应用。

    桥接后端事件总线与前端通道，将后端状态变更持久转发到 Tauri 前端。
    """

    def __init__(self, context: ApplicationContext) -> None:
        launcher = context.state
        self.context = context
        self.logger = get_logger("Adapter")
        self.commands = Commands()
        self.events = context.events
        self.resource_path: Path = launcher.resource_path  # 前端等只读资源目录
        self.config: dict[str, Any] = launcher.config  # 配置
        self._active_window_chrome = "custom"
        self.launcher_version: str = launcher.launcher_version  # 启动器版本
        self.frontend_api_instance = FrontendApi(context)

    def run(self) -> None:
        """
        启动 Tauri 前端。
        """
        self.logger.info("正在初始化前端界面")
        self._register_commands()
        self._register_events()
        tauri_config = self._build_config()
        self.context.state.active_window_chrome = self._active_window_chrome
        with start_blocking_portal("asyncio") as portal:  # 允许异步方法
            self.logger.debug("正在创建 Tauri 窗口")
            context = context_factory(self.resource_path, tauri_config=tauri_config)
            self.logger.debug("Tauri 窗口已创建")
            self.logger.debug("正在构建 Tauri 应用")
            app = builder_factory().build(
                context=context,
                invoke_handler=self.commands.generate_handler(portal),
                plugins=[dialog_init()],
            )
            self.logger.info("前端界面初始化完成")
            self.logger.info("Tauri 主循环已启动，正在等待前端就绪")
            self._startup_focus_attempted = False
            app.run_return(self._focus_main_window_on_ready)
        self.logger.info("前端已退出")

    def _focus_main_window_on_ready(self, app_handle: AppHandle, event: RunEventType) -> None:
        """
        在宿主首次就绪时请求主窗口焦点。

        只尝试一次，避免后续事件或前端刷新抢走用户已经切换到其他程序的焦点。
        系统可能拒绝前台激活；失败时记录原因，但不阻断启动器运行。

        :param app_handle: 当前 Tauri 应用句柄
        :param event: 宿主主循环事件
        """
        if self._startup_focus_attempted or not isinstance(event, RunEvent.Ready):
            return
        self._startup_focus_attempted = True
        try:
            main_window = Manager.get_webview_window(app_handle, "main")
            if main_window is None:
                self.logger.warning("启动时未找到主窗口，无法请求焦点")
                return
            main_window.set_focus()
        except (OSError, RuntimeError):
            self.logger.exception("启动时请求主窗口焦点失败")

    def _build_config(self) -> dict[str, Any]:
        """
        根据启动配置生成主窗口参数，并记录本次运行的有效窗口模式。

        系统边缘模式仅在 Windows 生效；其他平台按纯自绘创建窗口，避免改变
        原有的插件窗口及跨平台外观。

        :return: 传给 Tauri 的应用配置
        """
        tauri_config = self.config.get("tauri", {})
        ui_config = self.config.get("ui")
        theme_config = ui_config.get("theme") if isinstance(ui_config, dict) else None
        preferred_chrome = theme_config.get("window_chrome") if isinstance(theme_config, dict) else None
        if preferred_chrome == "native":
            self._active_window_chrome = "native"
        elif preferred_chrome == "system_shadow" and sys.platform == "win32":
            self._active_window_chrome = "system_shadow"
        else:
            self._active_window_chrome = "custom"
        is_native_chrome = self._active_window_chrome == "native"
        has_system_edge = self._active_window_chrome in {"system_shadow", "native"}
        return {
            "version": self.launcher_version,
            "build": {"frontendDist": tauri_config.get("frontenddist", "frontend/dist")},
            "app": {
                "windows": [
                    {
                        "decorations": is_native_chrome,
                        "transparent": not has_system_edge,
                        "shadow": has_system_edge,
                        "title": tauri_config.get("title", "EuoraCraft Launcher"),
                        "width": tauri_config.get("width", 900),
                        "height": tauri_config.get("height", 600),
                        "minWidth": 960,  # 真神奇，这玩意会在启用窗口边缘阴影的情况下多出几个px
                        "minHeight": 600,
                        "visible": True,
                    }
                ]
            },
        }

    def _register_commands(self) -> None:
        # 将后端命令处理器注册为 Tauri 的 IPC 命令。
        api = self.frontend_api_instance
        logger = getattr(self, "logger", get_logger("Adapter"))
        handlers = command_handlers(api)
        for name, handler in handlers.items():
            self.commands.command(name)(handler)
        logger.debug("IPC 命令注册完成：数量：%d", len(handlers))

    def _register_events(self) -> None:
        # 复用共享事件桥完成「后端事件 → 前端事件」的纯转换，仅保留含副作用的订阅。
        api = self.frontend_api_instance
        bus = self.events
        logger = getattr(self, "logger", get_logger("Adapter"))
        logger.debug("正在注册后端到前端的事件转发")

        subscribe_all_frontend_events(bus, api.emit_to_frontend)

        # 严重错误需要保留待确认的内存副本，供前端轮询补录；窗口侧副作用只在这些订阅里处理。
        bus.subscribe("launcher:error", api.emit_error_to_frontend)
        bus.subscribe(
            "accounts:microsoft_login_status",
            lambda data: api.focus_window() if data.get("focus") else None,
        )
        # 单实例互斥：后续启动进程发来的置前请求经事件总线转为主窗口激活。
        bus.subscribe(
            "launcher:focus_request",
            lambda _payload: api.focus_window(),
        )
        bus.subscribe(
            "plugin:disabled",
            lambda plugin: api.close_plugin_windows(plugin.name),
        )
        bus.subscribe(
            "plugin:unloaded",
            lambda name: api.close_plugin_windows(name),
        )
        logger.debug("后端到前端的事件转发注册完成")
