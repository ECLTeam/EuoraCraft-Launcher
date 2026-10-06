# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：进程内事件总线：订阅/发布/按属主注销。
#
# 公开接口：
#   - class EventBus — 进程内同步事件总线，只负责事件订阅与分发。
#       - subscribe(event, handler, owner=…) -> Unsubscribe — 订阅事件并返回幂等的取消订阅函数。
#       - emit(event, *args, **kwargs) -> None — 同步分发事件，并隔离单个处理器抛出的异常。
#       - unsubscribe(event, handler) -> None — 移除事件上的指定处理器；处理器不存在时不执行操作。
#       - remove_handlers_by_owner(owner) -> None — 移除指定组件注册的全部事件处理器。
#       - clear() -> None — 清空当前事件总线的全部订阅。
# ============================================================

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from threading import RLock
from typing import Any

EventHandler = Callable[..., None]
Unsubscribe = Callable[[], None]


class EventBus:
    """
    进程内同步事件总线，只负责事件订阅与分发。

    事件总线不保存业务服务，也不提供服务定位功能。每个应用上下文拥有一个独立实例，
    并通过构造参数传递给需要发布或订阅事件的组件。
    """

    def __init__(self) -> None:
        """
        创建一组相互隔离的事件订阅表。
        """
        self.logger = logging.getLogger("EuoraCraft-Launcher.EventBus")
        # 所有订阅都保存在事件总线实例内，避免测试和多窗口之间共享全局状态。
        self._handlers: dict[str, list[tuple[EventHandler, str | None]]] = defaultdict(list)
        # 处理器可以在回调期间取消订阅，因此使用可重入锁保护订阅表快照。
        self._lock = RLock()

    def subscribe(self, event: str, handler: EventHandler, owner: str | None = None) -> Unsubscribe:
        """
        订阅事件并返回幂等的取消订阅函数。

        :param event: 事件名称，如 ``config:updated``
        :param handler: 事件触发时同步调用的处理器
        :param owner: 可选的订阅者标识，用于批量移除同一组件的处理器
        :return: 取消本次订阅的函数
        """
        with self._lock:
            self._handlers[event].append((handler, owner))

        def unsubscribe() -> None:
            self.unsubscribe(event, handler)

        return unsubscribe

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """
        同步分发事件，并隔离单个处理器抛出的异常。

        :param event: 要分发的事件名称
        :param args: 传递给处理器的位置参数
        :param kwargs: 传递给处理器的关键字参数
        """
        with self._lock:
            handlers = tuple(self._handlers.get(event, ()))
        for handler, _ in handlers:
            try:
                handler(*args, **kwargs)
            except Exception:
                self.logger.exception("事件 %s 的处理函数执行失败", event)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        """
        移除事件上的指定处理器；处理器不存在时不执行操作。

        :param event: 已订阅的事件名称
        :param handler: 需要移除的处理器
        """
        with self._lock:
            handlers = self._handlers.get(event)
            if not handlers:
                return
            self._handlers[event] = [(registered, owner) for registered, owner in handlers if registered != handler]
            if not self._handlers[event]:
                self._handlers.pop(event, None)

    def remove_handlers_by_owner(self, owner: str) -> None:
        """
        移除指定组件注册的全部事件处理器。

        :param owner: 订阅时记录的组件标识
        """
        with self._lock:
            for event, handlers in tuple(self._handlers.items()):
                remaining = [
                    (handler, registered_owner) for handler, registered_owner in handlers if registered_owner != owner
                ]
                if remaining:
                    self._handlers[event] = remaining
                else:
                    self._handlers.pop(event, None)

    def clear(self) -> None:
        """
        清空当前事件总线的全部订阅。
        """
        with self._lock:
            self._handlers.clear()
