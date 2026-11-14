# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：内核进程引擎协议，进程服务经此驱动子进程而不依赖具体实现域。
#
# 公开接口：
#   - class ProcessHandle — 子进程句柄的窄视图。
#   - class ProcessEngine — 进程服务依赖的子进程引擎协议。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class ProcessHandle(Protocol):
    """
    子进程句柄的窄视图，只暴露进程号与退出探测。
    """

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...


class ProcessEngine(Protocol):
    """
    进程服务依赖的子进程引擎协议。

    内核不实现进程生成细节，由组合根注入具体实现（当前为 MC 域
    ``MinecraftProcessEngine``）；实现方不得在回调内做重活。
    """

    def set_log_callback(self, callback: Callable[[str, str], None]) -> None:
        """
        注册逐行输出回调，参数为输出行与实例标识。

        :param callback: 输出回调
        """
        ...

    def set_exit_callback(self, callback: Callable[[int, str], None]) -> None:
        """
        注册退出回调，参数为退出码与实例标识。

        :param callback: 退出回调
        """
        ...

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
        启动一个子进程实例。

        :param instance_name: 实例名称
        :param instance_type: 实例类型
        :param args: 可执行文件及参数
        :param cwd: 可选工作目录
        :param std_in: 是否开启标准输入管道
        :return: 实例标识与进程句柄
        """
        ...

    def send_stdin(self, instance_id: str, data: str) -> bool:
        """
        向实例的标准输入管道写入一行数据。

        :param instance_id: 目标实例标识
        :param data: 写入的数据
        :return: 实例存在且开启标准输入时为 True
        """
        ...

    def stop_instance(self, instance_id: str, *, force: bool, wait_timeout: float | None) -> bool:
        """
        停止实例对应的子进程。

        :param instance_id: 目标实例标识
        :param force: True 直接杀死，False 发送终止信号
        :param wait_timeout: 等待进程结束的秒数，None 不等待
        :return: 进程是否已结束
        """
        ...

    def get_instance_process(self, instance_id: str) -> ProcessHandle | None:
        """
        返回实例的进程句柄，实例不存在时返回 None。

        :param instance_id: 目标实例标识
        :return: 进程句柄或 None
        """
        ...
