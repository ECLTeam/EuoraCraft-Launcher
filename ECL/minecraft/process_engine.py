# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：以 Core 的 InstancesManager 实现内核进程引擎协议。
#
# 公开接口：
#   - class MinecraftProcessEngine — 进程引擎协议的 MC 域实现。
# ============================================================

from __future__ import annotations

from pathlib import Path

from ECL.minecraft.game import InstancesManager
from ECL.services.process_engine import ProcessHandle


class MinecraftProcessEngine:
    """
    将 Core 的 InstancesManager 适配为内核进程引擎协议。

    与游戏服务共享同一管理器实例时，游戏实例的输出回调与
    生命周期由双方按实例标识自行区分，互不清理对方实例。
    """

    def __init__(self, manager: InstancesManager | None = None) -> None:
        """
        绑定底层进程管理器，缺省时自行创建。

        :param manager: 与游戏服务共享的 InstancesManager
        """
        self._manager = manager or InstancesManager()

    def set_log_callback(self, callback) -> None:
        """
        注册逐行输出回调，直接转发底层管理器。

        :param callback: 输出回调
        """
        self._manager.set_log_callback(callback)

    def set_exit_callback(self, callback) -> None:
        """
        注册退出回调，直接转发底层管理器。

        :param callback: 退出回调
        """
        self._manager.set_exit_callback(callback)

    def create_instance(
        self,
        *,
        instance_name: str,
        instance_type: str,
        args: list[str],
        cwd: Path | None,
        std_in: bool,
    ) -> tuple[str, ProcessHandle]:
        """
        启动一个子进程实例，转发底层管理器。

        :param instance_name: 实例名称
        :param instance_type: 实例类型
        :param args: 可执行文件及参数
        :param cwd: 可选工作目录
        :param std_in: 是否开启标准输入管道
        :return: 实例标识与进程句柄
        """
        return self._manager.create_instance(
            instance_name=instance_name,
            instance_type=instance_type,
            args=args,
            cwd=cwd,
            std_in=std_in,
        )

    def send_stdin(self, instance_id: str, data: str) -> bool:
        """
        向实例的标准输入管道写入一行数据。

        :param instance_id: 目标实例标识
        :param data: 写入的数据
        :return: 实例存在且开启标准输入时为 True
        """
        return self._manager.send_stdin(instance_id, data)

    def stop_instance(self, instance_id: str, *, force: bool, wait_timeout: float | None) -> bool:
        """
        停止实例对应的子进程。

        :param instance_id: 目标实例标识
        :param force: True 直接杀死，False 发送终止信号
        :param wait_timeout: 等待进程结束的秒数，None 不等待
        :return: 进程是否已结束
        """
        return self._manager.stop_instance(instance_id, force=force, wait_timeout=wait_timeout)

    def get_instance_process(self, instance_id: str) -> ProcessHandle | None:
        """
        返回实例的进程句柄，实例不存在时返回 None。

        :param instance_id: 目标实例标识
        :return: 进程句柄或 None
        """
        return self._manager.instances.get(instance_id, {}).get("Instance")
