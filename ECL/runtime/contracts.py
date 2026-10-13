# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义进程模式、无头任务和结果协议，不依赖桌面或业务服务。
#
# 公开接口：
#   - class RunMode — 进程的界面模式。
#   - class HeadlessTask — 支持的有限任务。
#   - class HeadlessExitCode — 无头执行的退出码。
#   - class HeadlessRuntimeError — 携带稳定错误码的运行失败。
#   - class HeadlessOptions — 无头执行参数。
#   - class DependencyProbe — 单项依赖探测结果。
#   - class RuntimeProbe — 白名单运行环境信息。
#   - class HeadlessResult — 一次任务的最终结果。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path


class RunMode(StrEnum):
    """
    区分桌面进程和不初始化 UI 的任务进程。
    """

    desktop = "desktop"
    headless = "headless"


class HeadlessTask(StrEnum):
    """
    列出由发行物提供的有限任务，不接受任意脚本或模块入口。
    """

    runtime_check = "runtime-check"


class HeadlessExitCode(IntEnum):
    """
    定义无头任务的退出状态，不依赖桌面退出码。
    """

    success = 0
    failed = 1
    invalid_arguments = 2
    cancelled = 130


class HeadlessRuntimeError(Exception):
    """
    表达预期的无头执行失败，消息只包含可公开的行为说明。
    """

    code: str

    def __init__(self, code: str, message: str) -> None:
        """
        保存稳定错误码和不包含凭据或路径的失败说明。

        :param code: 供调用者判断失败原因的错误码
        :param message: 可公开的中文说明
        """
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class HeadlessOptions:
    """
    保存一次无头执行参数，不读取或修改用户设置。
    """

    task: HeadlessTask
    data_path: Path | None = None
    result_file: Path | None = None
    is_debug: bool = False
    log_level: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyProbe:
    """
    保存一项已成功导入的宿主依赖的公开版本。
    """

    name: str
    version: str


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    """
    保存不包含路径、配置、账户或环境变量的运行环境摘要。
    """

    launcher_version: str
    python_implementation: str
    python_version: str
    platform: str
    architecture: str
    abi: str
    is_frozen: bool
    has_ui_modules: bool
    dependencies: tuple[DependencyProbe, ...]


@dataclass(frozen=True, slots=True)
class HeadlessResult:
    """
    保存任务与输出完成后供调用者读取的最终结果。
    """

    task: HeadlessTask
    success: bool
    error_code: str | None = None
    message: str | None = None
    probe: RuntimeProbe | None = None
    protocol_version: int = 1
