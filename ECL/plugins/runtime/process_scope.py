# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为 WorkerSupervisor 选择实际平台监管实现及持有身份的接口。
#
# 公开接口：
#   - ProcessReference / PreparedProcess / ProcessScope — 监管资源生命周期契约。
#   - create_process_scope — 创建 Windows Job 或 POSIX 守护范围。
# ============================================================

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Protocol

from ECL.plugin_api._errors import PluginRuntimeError
from ECL.plugins.runtime.executable import ExecutableLaunchSpec


class ProcessReference(Protocol):
    """
    保存系统确认的身份，关闭引用不表示终止执行。
    """

    pid: int
    created_at: int
    image_path: Path

    def is_alive(self) -> bool:
        """
        查询绑定的内核身份，不能改用可能复用的裸 PID。

        :return: 原进程仍在执行时为 True
        """
        ...

    def close(self) -> None:
        """
        幂等释放查询引用，不终止执行。
        """
        ...


class PreparedProcess(Protocol):
    """
    监管已就绪但尚未运行 Worker 的受控启动对象。
    """

    pid: int

    def resume(self) -> None:
        """
        监管建立后才允许 Worker 开始运行。
        """
        ...


class ProcessScope(Protocol):
    """
    单个 Worker 来源的创建、认证和确认退出边界。
    """

    def prepare(self, arguments: tuple[str, ...], cwd_path: Path, environment: dict[str, str]) -> PreparedProcess:
        """
        准备监管资源，保持 Worker 尚未执行的状态。

        :param arguments: 固定发行入口及独立参数
        :param cwd_path: 受信工作目录
        :param environment: 净化后的完整环境
        :return: 调用方显式恢复的启动对象
        """
        ...

    def reference(self, pid: int, executable_path: Path) -> ProcessReference:
        """
        核对本地对端的镜像和监管归属后绑定身份。

        :param pid: 内核返回的真实连接 PID
        :param executable_path: 固定发行入口
        :return: 调用方负责释放的身份引用
        """
        ...

    def is_member(self, reference: ProcessReference) -> bool:
        """
        复核原进程是否仍属于本次监管。

        :param reference: 已绑定的内核对象
        :return: 原身份归属有效时为 True
        """
        ...

    def active_count(self) -> int:
        """
        查询本来源的实际活跃成员数量。

        :return: 未退出的成员数
        """
        ...

    def close(self, timeout: float = 5.0) -> None:
        """
        确认实际清理后释放监管，超时保留责任供重试。

        :param timeout: 本次关闭的有界等待预算
        :raises PluginRuntimeError: 尚未确认清理成功
        """
        ...


def create_process_scope(executable: ExecutableLaunchSpec, run_id: str) -> ProcessScope:
    """
    按当前平台选择实际内核边界，不通过声明或环境变量绕过支持条件。

    :param executable: 当前受信发行入口
    :param run_id: 本次运行身份
    :return: 尚未创建 Worker 的资源范围
    :raises PluginRuntimeError: 平台或处理器未支持
    """
    if sys.platform == "win32" and platform.machine().casefold() in {"amd64", "x86_64"}:
        from ECL.plugins.runtime.windows import WindowsJob

        return WindowsJob()
    if sys.platform in {"linux", "darwin"} and platform.machine().casefold() in {"amd64", "x86_64", "arm64", "aarch64"}:
        from ECL.plugins.runtime.posix import PosixScope

        return PosixScope(executable, run_id)
    raise PluginRuntimeError("当前平台尚不支持插件 Worker", "PLUGIN_RUNTIME_UNSUPPORTED")
