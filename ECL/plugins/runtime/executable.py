# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：解析宿主实际发行入口并构造独立 Worker 环境，不下载解释器。
#
# 公开接口：
#   - class ExecutableLaunchSpec — 源码或冻结发行物的受信启动描述。
# ============================================================

from __future__ import annotations

import builtins
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ecl_plugin_api._windows import WindowsRuntimeError


@dataclass(frozen=True, slots=True)
class ExecutableLaunchSpec:
    """
    保存经过核实的原始执行文件，不使用解包目录猜测文件名。
    """

    executable_path: Path
    project_path: Path | None
    builder_kind: str
    allowed_environment: ClassVar[frozenset[str]] = frozenset(
        {"systemroot", "windir", "systemdrive", "temp", "tmp", "path", "pathext", "comspec", "number_of_processors"}
    )

    @classmethod
    def current(cls) -> ExecutableLaunchSpec:
        """
        从当前受信运行时解析源码、PyInstaller 或 Nuitka 启动入口。

        :return: 可以再次执行的宿主描述
        :raises WindowsRuntimeError: 冻结文件不可确定或不存在时抛出
        """
        original_binary = getattr(builtins, "__nuitka_binary_exe", None)
        if original_binary:
            return cls._frozen(Path(original_binary), "nuitka")
        if getattr(sys, "frozen", False):
            return cls._frozen(Path(sys.executable), "pyinstaller")
        if "__compiled__" in globals() or getattr(builtins, "__compiled__", None):
            raise WindowsRuntimeError("无法确认 Nuitka 原始执行文件", "WORKER_EXECUTABLE_UNRESOLVED")
        project_path = Path(__file__).resolve().parents[3]
        # Windows venv 的入口是重定向器；直接复用当前 VM 的受信 CPython，避免再创建控制台代理。
        executable_path = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        if not executable_path.is_file() or not (project_path / "main.py").is_file():
            raise WindowsRuntimeError("无法确认源码启动入口", "WORKER_EXECUTABLE_UNRESOLVED")
        return cls(executable_path, project_path, "source")

    @classmethod
    def _frozen(cls, executable_path: Path, kind: str) -> ExecutableLaunchSpec:
        """
        拒绝不存在的构建器入口，不回退其他解释器或猜测目录。
        """
        if not executable_path.is_absolute() or not executable_path.is_file():
            raise WindowsRuntimeError("无法确认冻结启动入口", "WORKER_EXECUTABLE_UNRESOLVED")
        return cls(executable_path.resolve(), None, kind)

    def arguments(self, address: str, host_pid: int, run_id: str) -> tuple[str, ...]:
        """
        构造公开启动参数，不在命令行传插件路径或认证秘密。

        :param address: 本运行会话的公开管道端点
        :param host_pid: 宿主真实 PID
        :param run_id: 宿主生成的运行会话，不赋予业务权限
        :return: 不经过 shell 的参数序列
        """
        prefix = (str(self.executable_path),)
        if self.project_path is not None:
            prefix += ("-S", str(self.project_path / "main.py"))
        return (
            *prefix,
            "--internal-plugin-worker",
            "--pipe-address",
            address,
            "--host-pid",
            str(host_pid),
            "--run-id",
            run_id,
        )

    def environment(self) -> dict[str, str]:
        """
        建立最小环境并隔离冻结引导角色，不沿用账户或开发通道凭据。

        :return: 用于 CreateProcess 的完整环境
        """
        result = {key: value for key, value in os.environ.items() if key.casefold() in self.allowed_environment}
        if self.builder_kind == "pyinstaller":
            result["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        return result

    def working_directory(self) -> Path:
        """
        返回受信工作目录，不以插件目录作为解释器搜索根。

        :return: 项目或发行文件目录
        """
        return self.project_path or self.executable_path.parent
