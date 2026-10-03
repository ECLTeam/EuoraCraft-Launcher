# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：进程服务：子进程生成、标准输入、停止与清单。
#
# 公开接口：
#   - class ProcessService — 面向插件与游戏实例的通用子进程实例注册表，负责生命周期、输出缓冲与标准输入交互。
#       - spawn(name, type_, args, cwd=…, stdin=…) -> str — 启动一个子进程实例并登记内部元数据。
#       - send_stdin(instance_id, data) -> bool — 向指定实例的标准输入管道写入一行数据。
#       - stop(instance_id, force=…, wait_timeout=…) -> bool — 停止指定实例对应的子进程。
#       - list() -> list[dict[str, Any]] — 返回当前登注册表的实例信息列表。
#       - close() -> None — 终止全部运行中的插件实例并清理读取线程。
# ============================================================

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import TYPE_CHECKING, Any

from ECL.game import InstancesManager
from ECL.utils import get_logger
from ECL.utils.operation_logging import OperationTrace, current_operation, trace_scope

if TYPE_CHECKING:
    from ECL.events.event_bus import EventBus


class ProcessService:
    """
    面向插件与游戏实例的通用子进程实例注册表，负责生命周期、输出缓冲与标准输入交互。

    内部复用 :class:`InstancesManager` 启动子进程并逐行读取输出；本类按实例标识
    维护元数据与最近输出环形缓冲，并通过事件总线推送 ``process:instance_log``
    与 ``process:instances_changed`` 事件供前端实例视图消费。运行中的 Minecraft
    实例通过订阅 ``game:instances_changed`` 事件自动登记，使实例终端同样展示游戏输出。

    :param event_bus: 承载子进程事件的事件总线
    :param instances_manager: 与游戏服务共享的进程管理器，缺省时自行创建
    """

    buffer_limit = 300

    def __init__(self, event_bus: EventBus, instances_manager: InstancesManager | None = None) -> None:
        self._events = event_bus
        self._manager = instances_manager or InstancesManager()
        self._manager.set_log_callback(self._on_log)
        self._manager.set_exit_callback(self._on_exit)
        self._lock = RLock()
        self._meta: dict[str, dict[str, Any]] = {}
        self._buffers: dict[str, deque[str]] = {}
        self._traces: dict[str, OperationTrace] = {}
        self._logger = get_logger("ProcessService")
        self._off_game_changed = event_bus.subscribe("game:instances_changed", self._on_game_changed)

    def _on_game_changed(self, payload: dict[str, Any]) -> None:
        # 登记或清理运行中的 Minecraft 实例，使实例终端也能展示游戏输出。
        action = payload.get("action")
        instance_id = payload.get("instanceId")
        if not instance_id:
            return
        if action == "started":
            with self._lock:
                self._meta[instance_id] = {
                    "name": payload.get("versionId") or "Minecraft",
                    "type": "Minecraft",
                    "stdin": False,
                }
                self._buffers[instance_id] = deque(maxlen=self.buffer_limit)
        else:
            with self._lock:
                self._meta.pop(instance_id, None)
                self._buffers.pop(instance_id, None)
        self._events.emit("process:instances_changed", self.list())

    def spawn(
        self,
        name: str,
        type_: str,
        args: str | list[str],
        cwd: str | None = None,
        stdin: bool = False,
    ) -> str:
        """
        启动一个子进程实例并登记内部元数据。

        :param name: 实例名称，供列表与退出回调辨认
        :param type_: 实例类型（建议使用 ``plugin:<用途>`` 前缀）
        :param args: 可执行文件及参数
        :param cwd: 可选的工作目录
        :param stdin: 是否开启标准输入管道
        :raises ValueError: 名称、类型或参数为空时
        :return: 生成的实例标识
        """
        if not name or not type_ or not args:
            raise ValueError("实例名称、类型与启动参数不能为空")
        self._logger.info("开始创建子进程")
        instance_id, _process = self._manager.create_instance(
            instance_name=name,
            instance_type=type_,
            args=args,
            cwd=cwd,
            std_in=stdin,
        )
        with self._lock:
            self._meta[instance_id] = {"name": name, "type": type_, "stdin": stdin}
            self._buffers[instance_id] = deque(maxlen=self.buffer_limit)
            trace = current_operation()
            if trace is not None:
                self._traces[instance_id] = trace
        self._logger.info("子进程已创建；实例编号：%s；进程号：%s", instance_id, _process.pid)
        self._events.emit("process:instances_changed", self.list())
        return instance_id

    def send_stdin(self, instance_id: str, data: str) -> bool:
        """
        向指定实例的标准输入管道写入一行数据。

        :param instance_id: 目标实例标识
        :param data: 写入的数据
        :return: 实例存在且开启标准输入时返回 True
        """
        with self._lock:
            meta = self._meta.get(instance_id)
            if meta is None or not meta["stdin"]:
                return False
        payload = data if data.endswith("\n") else data + "\n"
        return self._manager.send_stdin(instance_id, payload)

    def stop(self, instance_id: str, force: bool = False, wait_timeout: float | None = None) -> bool:
        """
        停止指定实例对应的子进程。

        :param instance_id: 目标实例标识
        :param force: True 直接杀死，False 发送终止信号
        :param wait_timeout: 等待进程结束的秒数，None 表示不等待
        :return: 进程是否已结束
        """
        self._logger.info("已请求%s子进程；实例编号：%s", "强制结束" if force else "停止", instance_id)
        has_stopped = self._manager.stop_instance(instance_id, force=force, wait_timeout=wait_timeout)
        self._logger.info("子进程停止结果：%s；实例编号：%s", "已结束" if has_stopped else "尚未确认结束", instance_id)
        return has_stopped

    def list(self) -> list[dict[str, Any]]:
        """
        返回当前登注册表的实例信息列表。

        每条信息包含唯一标识、名称、类型、进程号、标准输入支持情况、运行状态，
        以及该实例最近的输出行（最多 ``buffer_limit`` 条），供前端实例视图初始化。

        :return: 实例信息列表
        """
        with self._lock:
            items: list[dict[str, Any]] = []
            for iid, meta in self._meta.items():
                proc = self._manager.instances.get(iid, {}).get("Instance")
                pid = proc.pid if proc is not None else None
                running = proc.poll() is None if proc is not None else False
                items.append(
                    {
                        "id": iid,
                        "name": meta["name"],
                        "type": meta["type"],
                        "pid": pid,
                        "stdin": meta["stdin"],
                        "running": running,
                        "lines": list(self._buffers.get(iid, ()))[: self.buffer_limit],
                    }
                )
            return items

    def close(self) -> None:
        """
        终止全部运行中的插件实例并清理读取线程。

        共享进程管理器中的 Minecraft 实例由游戏服务负责保留，这里只回收插件子进程。
        """
        self._off_game_changed()
        with self._lock:
            plugin_ids = [iid for iid, meta in self._meta.items() if meta.get("type") != "Minecraft"]
        for iid in plugin_ids:
            self._manager.stop_instance(iid, force=True, wait_timeout=1.0)

    def _on_log(self, line: str, instance_id: str) -> None:
        # 处理 InstancesManager 逐行回读的输出，写入缓冲并推送实时事件。
        with self._lock:
            meta = self._meta.get(instance_id)
            if meta is None:
                return
            self._buffers[instance_id].append(line)
            name, type_ = meta["name"], meta["type"]
        self._events.emit(
            "process:instance_log",
            {"instanceId": instance_id, "name": name, "type": type_, "line": line},
        )

    def _on_exit(self, exit_code: int, instance_id: str) -> None:
        # 处理子进程退出事件，回收内部登记并推送实例列表变更。
        with self._lock:
            self._meta.pop(instance_id, None)
            self._buffers.pop(instance_id, None)
            trace = self._traces.pop(instance_id, None)
        with trace_scope(trace):
            self._logger.info("子进程已退出；实例编号：%s；退出码：%s", instance_id, exit_code)
        self._events.emit("process:instances_changed", self.list())


__all__ = ["ProcessService"]
