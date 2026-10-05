# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：持久记录 Java 使用租约，核对实际进程而非界面或等待方状态。
#
# 公开接口：
#   - class RuntimeLease — 关联启动阶段和实际游戏进程的运行时占用。
#   - class JavaLifecycle — 核对、取得及释放持久使用证据。
# ============================================================
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psutil

from ECL.utils import atomic_write_text

from .models import JavaError, RuntimeUsage
from .registry import JavaRegistry


@dataclass(slots=True)
class RuntimeLease:
    """
    保存一次启动的持久占用，实际进程接管后不因启动器关闭而释放。
    """

    lifecycle: JavaLifecycle
    usage: RuntimeUsage
    is_game: bool = False
    is_released: bool = False

    def attach_process(self, process_id: int) -> None:
        """
        用真实进程身份替换启动阶段租约；已退出的进程幂等释放。

        :param process_id: InstancesManager 返回的实际进程编号
        """
        try:
            started_at = psutil.Process(process_id).create_time()
        except psutil.NoSuchProcess:
            self.release()
            return
        with self.lifecycle.registry.locked():
            if self.is_released:
                return
            updated = self.usage.model_copy(
                update={"process_id": process_id, "process_started_at": started_at, "is_game": True}
            )
            self.lifecycle.write(updated)
            self.usage = updated
            self.is_game = True

    def release(self) -> None:
        """
        在实际退出或启动失败时幂等删除占用证据。
        """
        with self.lifecycle.registry.locked():
            if self.is_released:
                return
            self.lifecycle.path(self.usage.lease_id).unlink(missing_ok=True)
            self.is_released = True


class JavaLifecycle:
    """
    将 PID 与进程启动时间一起核对，避免 PID 复用导致错误使用判定。

    无法读取的占用证据保持阻断，不据清单为空推断未被使用。
    """

    registry: JavaRegistry
    usage_path: Path

    def __init__(self, registry: JavaRegistry) -> None:
        """
        归属使用证据目录，不取得共享网络或游戏进程资源。

        :param registry: 唯一的运行时登记清单
        """
        self.registry = registry
        self.usage_path = registry.root_path / ".leases"
        if self.usage_path.is_symlink():
            raise JavaError("Java 使用记录目录已被替换", "JAVA_DIRECTORY_CHANGED")
        self.usage_path.mkdir(exist_ok=True)

    def path(self, lease_id: str) -> Path:
        """
        返回仅由后端生成的使用证据路径。

        :param lease_id: 后端生成的使用证据身份
        """
        if len(lease_id) != 32 or any(character not in "0123456789abcdef" for character in lease_id):
            raise JavaError("Java 使用记录身份无效", "JAVA_USAGE_UNKNOWN")
        return self.usage_path / f"{lease_id}.json"

    def write(self, usage: RuntimeUsage) -> None:
        """
        在 registry.locked 内原子保存使用证据，失败保留上一份证据。

        :param usage: 已确认的进程身份与运行时使用信息
        """
        atomic_write_text(self.path(usage.lease_id), usage.model_dump_json())

    def acquire(self, runtime_id: str) -> RuntimeLease:
        """
        在调用方持有登记互斥时为启动阶段取得持久占用。

        :param runtime_id: 已验证且启用的运行时
        :return: 由退出回调或失败清理释放的租约
        """
        usage = RuntimeUsage(
            lease_id=uuid4().hex,
            runtime_id=runtime_id,
            process_id=os.getpid(),
            process_started_at=psutil.Process().create_time(),
        )
        self.write(usage)
        return RuntimeLease(self, usage)

    def is_in_use(self, runtime_id: str) -> bool:
        """
        在登记互斥下核对当前证据，并清除确定已退出的租约。

        :raises JavaError: 证据或进程身份无法确认时抛出，阻止移除

        :param runtime_id: 已登记的运行时身份
        """
        for file_path in self.usage_path.glob("*.json"):
            try:
                if file_path.is_symlink() or file_path.stat().st_size > 4096:
                    raise ValueError("invalid usage")
                usage = RuntimeUsage.model_validate_json(file_path.read_bytes())
                process = psutil.Process(usage.process_id)
                is_live = process.is_running() and abs(process.create_time() - usage.process_started_at) < 0.01
            except psutil.NoSuchProcess:
                is_live = False
            except (OSError, ValueError, psutil.AccessDenied) as exc:
                raise JavaError("Java 的进程占用状态暂无法确认", "JAVA_USAGE_UNKNOWN") from exc
            if not is_live:
                file_path.unlink(missing_ok=True)
            elif usage.runtime_id == runtime_id:
                return True
        return False
