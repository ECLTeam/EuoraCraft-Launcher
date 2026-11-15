# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：跨插件事件中枢。插件经各自 broker 发布主题事件，中枢把
#   事件投递给所有已注册 broker 中匹配订阅的接收方；投递不跨进程，
#   实际回调仍由各 broker 自己的回调通道执行。
#
# 公开接口：
#   - class PluginEventHub — 宿主持有的插件事件发布订阅中枢。
# ============================================================

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

from ECL.plugins.contracts import JsonValue, ProtocolError
from ECL.utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable


class PluginEventHub:
    """
    宿主唯一的事件发布订阅中枢，只做匹配与转发，不保存历史事件。

    broker 注册自身并在关闭时注销；发布方主题必须以自身插件名为前缀，
    订阅方可以订阅任意主题。投递按订阅登记时绑定的租约校验，失效订阅
    不投递；接收队列已满时丢弃该事件并继续，不影响发布方。
    """

    max_listeners: int = 256

    def __init__(self) -> None:
        self._lock = RLock()
        self._brokers: list[object] = []

    def register(self, receiver: object) -> None:
        """
        登记一个事件接收 broker；重复登记幂等。

        :param receiver: 已构造完成的 PluginHostBroker
        :raises ProtocolError: 中枢监听 broker 数量超过上限时抛出
        """
        with self._lock:
            if receiver not in self._brokers:
                if len(self._brokers) >= self.max_listeners:
                    raise ProtocolError("事件中枢监听超过上限", "PLUGIN_RATE_LIMITED")
                self._brokers.append(receiver)

    def unregister(self, receiver: object) -> None:
        """
        注销一个事件接收 broker；未登记时无操作。
        """
        with self._lock:
            if receiver in self._brokers:
                self._brokers.remove(receiver)

    def publish(
        self,
        source: str,
        topic: str,
        payload: dict[str, JsonValue],
        deliver: Callable[[object, str, dict[str, JsonValue]], int],
    ) -> int:
        """
        把一次发布投递给全部匹配订阅，返回实际接收的订阅数量。

        :param source: 发布方插件名，仅用于日志
        :param topic: 已通过命名空间校验的完整主题
        :param payload: 已通过大小校验的事件载荷
        :param deliver: 对每个候选 broker 调用的投递函数，返回其接收数量
        :return: 全部 broker 实际接收的订阅总数
        """
        with self._lock:
            candidates = tuple(self._brokers)
        delivered = 0
        for broker in candidates:
            try:
                delivered += deliver(broker, topic, payload)
            except ProtocolError as exc:
                get_logger("PluginEventHub").debug(
                    "插件事件投递跳过失效接收；主题：%s；原因：%s", topic, exc.error_code
                )
        get_logger("PluginEventHub").debug(
            "插件事件已发布；来源：%s；主题：%s；接收：%d", source, topic, delivered
        )
        return delivered

    def close(self) -> None:
        """
        清空全部登记；仅在运行服务关闭时调用。
        """
        with self._lock:
            self._brokers.clear()
