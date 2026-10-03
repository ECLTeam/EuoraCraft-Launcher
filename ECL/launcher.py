# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：启动器编排：装配应用上下文、运行主循环并给出退出码。
#
# 公开接口：
#   - class LauncherExitCode
#   - class EuoraCraftLauncher — 编排一次桌面应用的完整运行周期。
#       - __init__(options=None) — 应用命令行启动参数并初始化日志系统。
#       - run() -> LauncherExitCode — 初始化后端并运行前端事件循环。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading
from enum import IntEnum
from pathlib import Path
from typing import Any

from ECL.adapters import Adapter
from ECL.application import ApplicationContext, ApplicationState, create_application
from ECL.cli import LaunchOptions
from ECL.common import __version__, __version_type__, get_runtime_info
from ECL.services.app_update import clear_stale_pending_update
from ECL.services.maintenance import apply_pending_debug_maintenance
from ECL.services.single_instance import probe_running_instance
from ECL.utils import configure_logging
from ECL.utils.logging import resolve_log_level


class LauncherExitCode(IntEnum):
    SUCCESS = 0
    STARTUP_FAILED = 2
    FRONTEND_FAILED = 3


class EuoraCraftLauncher:
    """
    编排一次桌面应用的完整运行周期。

    串联日志系统、后端应用上下文与前端适配器；业务依赖由
    :func:`create_application` 构造，本类仅负责初始化、运行与关闭的调度。
    """

    # 类级默认：允许测试经 object.__new__ 构造实例而不触发单实例移交语义。
    _handed_over = False

    def __init__(self, options: LaunchOptions | None = None) -> None:
        """
        收集运行环境信息并初始化日志系统，同时填充启动器的运行状态字段。

        命令行 --data-dir 在此阶段生效：日志目录等所有数据路径都随之重定向。
        初始化日志系统之前会先探测单实例：同一数据目录已有主实例运行时，
        本进程请求其窗口置前并跳过日志等一切初始化，随后在 run() 中退出。
        """
        self.options = options if options is not None else LaunchOptions()  # 本次运行的命令行启动参数
        self.runtime_info = get_runtime_info(data_path_override=self.options.data_dir)
        self.app_path: Path = self.runtime_info["app_path"]  # 启动器数据与运行文件所在目录
        self.resource_path: Path = self.runtime_info["resource_path"]  # 打包资源或源码资源所在目录
        self.data_path: Path = self.runtime_info["data_path"]  # 后端持久化数据目录
        self.launcher_version = __version__  # 启动器完整版本号（含预发布与构建日期）
        self.launcher_version_type = __version_type__  # 启动器版本类型（alpha/beta/rc/release；dev 表示源码启动）
        self.is_frozen = self.runtime_info["is_frozen"]  # 是否运行于打包后的可执行文件
        self.debug = False  # 是否启用调试模式（调试工具与标签显示）
        self.debug_log_level = "info"  # 控制台日志级别
        self.config: dict[str, Any] = {}  # 已应用环境变量的启动配置
        self.context: ApplicationContext | None = None  # 已构造的后端应用上下文
        self._shutdown_complete = False  # 关闭流程是否已完成（用于幂等）
        # 单实例探测必须先于日志系统：移交成功时不创建日志文件，避免与主实例竞争轮转句柄。
        self._handed_over = probe_running_instance(self.data_path, self.options.forwarded_argv)
        if self._handed_over:
            self.logging = None  # 移交进程不初始化日志系统
            self.logger = logging.getLogger("EuoraCraft_Launcher")  # 无处理器的兜底日志器
            print("EuoraCraft Launcher启动器已在运行，已请求激活已运行的窗口。")
            return
        self.logging = configure_logging(self.data_path)  # 日志系统实例
        self.logger = self.logging.get_logger("EuoraCraft_Launcher")  # 启动器专用日志器
        self.logger.debug(
            "启动器运行环境：程序目录：%s；资源目录：%s；打包运行：%s；Python版本：%s",
            self.app_path,
            self.resource_path,
            ("是" if self.is_frozen else "否"),
            sys.version.split()[0],
        )

    def run(self) -> LauncherExitCode:
        """
        初始化后端并运行前端事件循环。

        :return: 本次运行的结果退出码（LauncherExitCode 枚举值）
        """
        if self._handed_over:
            # 同一数据目录已有主实例：置前请求已送达，本进程立即退出。
            return LauncherExitCode.SUCCESS
        self.logger.info("正在启动 EuoraCraft Launcher V%s", self.launcher_version)
        try:
            self._initialize()
        except Exception:
            self.logger.exception("启动器初始化失败")
            self._shutdown()
            return LauncherExitCode.STARTUP_FAILED

        try:
            self.logger.info("启动前端")
            Adapter(self._require_context()).run()
        except Exception:
            self.logger.exception("前端界面运行失败")
            return LauncherExitCode.FRONTEND_FAILED
        finally:
            self._shutdown()
        return LauncherExitCode.SUCCESS

    def _initialize(self) -> None:
        # 执行启动维护任务并构造后端应用上下文。
        self.logger.info("正在初始化")
        if sys.platform not in {"win32", "linux", "darwin"}:
            raise RuntimeError(f"不支持的系统环境: {sys.platform}")
        if not self.is_frozen:
            self.logger.warning("当前处于开发环境")

        try:
            maintenance_results = apply_pending_debug_maintenance(self.data_path)
        except OSError:
            self.logger.exception("执行待处理的调试维护操作失败")
        else:
            self.logger.debug("待处理的调试维护任务数量: %d", len(maintenance_results))
            for result in maintenance_results:
                archived = "、".join(result.archived_targets) or "无"
                self.logger.warning(
                    "已执行调试维护操作 %s，已备份目标: %s，备份位置: %s",
                    result.action,
                    archived,
                    result.backup_path or "无",
                )

        cleared = False
        try:
            cleared = clear_stale_pending_update(self.data_path)
        except OSError:
            self.logger.exception("清理待应用的启动器更新标记失败")
        if cleared:
            self.logger.info("已清理遗留的启动器更新标记")

        self.context = create_application(
            self.runtime_info,
            launch_options=self.options,
            on_state_ready=self._apply_bootstrap_state,
        )
        self.config = self.context.state.config
        self.debug = self.context.state.debug
        self.debug_log_level = self._config_log_level()
        self._apply_log_level()
        self.logging.install_frontend_handler(self.context.events)
        self.context.events.subscribe("config:updated", self._on_config_updated)
        self.context.events.subscribe("launcher:request_restart", lambda _data: self._schedule_update_restart())
        self.logger.debug(
            "后端服务已就绪：账户服务：%s；游戏服务：%s；插件服务：%s",
            type(self.context.accounts).__name__,
            type(self.context.game).__name__,
            type(self.context.plugins).__name__,
        )
        self.logger.info("后端初始化完成")

    def _apply_bootstrap_state(self, state: ApplicationState) -> None:
        # 在服务和插件构造前应用启动配置，使配置的日志级别立即进入终端。
        self.config = state.config
        self.debug = state.debug
        self.debug_log_level = self._config_log_level()
        self._apply_log_level()
        self.logger.debug("启动阶段日志级别已应用: %s", self.debug_log_level)

    def _config_log_level(self) -> str:
        # 从启动器配置读取控制台日志级别名称，缺失或非法时回退为 info。
        launcher_config = self.config.get("launcher") if isinstance(self.config, dict) else None
        raw = (launcher_config or {}).get("debug_log_level")
        return raw if isinstance(raw, str) and raw else "info"

    def _apply_log_level(self) -> None:
        # 开启 Debug 模式时强制最高诊断级别，否则采用独立配置的日志级别。
        self.logging.set_level(resolve_log_level("debug" if self.debug else self.debug_log_level))

    def _require_context(self) -> ApplicationContext:
        # 返回已经初始化的应用上下文。
        if self.context is None:
            raise RuntimeError("后端尚未初始化")
        return self.context

    def _shutdown(self) -> None:
        # 幂等关闭后端上下文，并在最后刷新日志处理器。
        if self._shutdown_complete:
            self.logger.debug("忽略重复的启动器关闭请求")
            return
        self._shutdown_complete = True
        self.logger.debug("开始关闭启动器后端")
        if self.context is not None:
            self.context.close()
            self.context = None
        self.logger.debug("启动器后端已关闭")
        if self.logging is not None:
            self.logging.shutdown()

    def _on_config_updated(self, section: str, data: Any) -> None:
        # 同步启动器镜像状态，并即时应用配置的控制台日志级别。
        if section != "launcher" or self.context is None:
            return
        self.config = self.context.state.config
        self.debug = self.context.state.debug
        self.debug_log_level = self._config_log_level()
        self._apply_log_level()
        self.logger.debug("控制台日志级别已更新: %s", self.debug_log_level)

    def _schedule_update_restart(self) -> None:
        # 延迟触发一次自动更新的重启：先让 IPC 响应与前端进度刷新完成。
        delay = 1.0
        timer = threading.Timer(delay, self._restart_for_update)
        timer.daemon = True  # 不阻塞进程退出兜底
        timer.start()

    def _restart_for_update(self) -> None:
        # 优雅关闭后端后结束当前进程，交由更新引导脚本完成替换并重启。
        self.logger.info("收到自动更新重启请求，正在退出以完成替换")
        try:
            self._shutdown()
        finally:
            os._exit(0)


__all__ = ["EuoraCraftLauncher", "LauncherExitCode"]
