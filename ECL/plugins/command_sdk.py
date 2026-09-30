# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：兼容归档插件的无参数命令 SDK，并适配为主进程宿主插件。
#
# 公开接口：
#   - class Plugin — 保留旧归档命令声明及调用契约。
#   - class CommandPluginAdapter — 将旧命令 SDK 接入宿主生命周期。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ECL.plugins.plugin import Plugin as HostPlugin

if TYPE_CHECKING:
    from ECL.plugins.manager import PluginManager


class Plugin:
    """
    保留归档命令 SDK 的无参数构造方式，不创建独立解释器。

    仅显式装饰或登记的方法成为命令，普通方法不自动暴露。
    """

    def __init__(self) -> None:
        """
        创建实例命令表，不在不同插件间共享动态注册。
        """
        self._commands: dict[str, Callable[..., object]] = {}

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name or name != name.strip() or ":" in name or len(name) > 128:
            raise ValueError("插件命令名称无效")

    @staticmethod
    def on_command(name: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
        """
        将实例方法标记为可调用命令，保留旧装饰器格式。

        :param name: 插件内部唯一命令名
        :return: 保留处理器的装饰器
        """
        Plugin._validate_name(name)

        def decorate(handler: Callable[..., object]) -> Callable[..., object]:
            handler._ecl_command_name = name
            return handler

        return decorate

    def register_command(self, name: str, handler: Callable[..., object]) -> None:
        """
        在实例化或加载阶段登记命令，不暴露任意对象方法。

        :param name: 插件内部唯一命令名
        :param handler: 可调用命令处理器
        """
        self._validate_name(name)
        if not callable(handler):
            raise ValueError("插件命令处理器必须可调用")
        if not hasattr(self, "_commands"):
            self._commands = {}
        self._commands[name] = handler

    def _registered_commands(self) -> dict[str, Callable[..., object]]:
        handlers: dict[str, Callable[..., object]] = {}
        for method_name in dir(type(self)):
            command_name = getattr(getattr(type(self), method_name), "_ecl_command_name", None)
            if command_name is not None:
                handlers[command_name] = getattr(self, method_name)
        handlers.update(getattr(self, "_commands", {}))
        return handlers

    def command_names(self) -> list[str]:
        """
        返回已登记命令名，供宿主实例适配使用。

        :return: 排序后的命令列表
        """
        return sorted(self._registered_commands())

    def invoke_command(self, name: str, params: dict[str, object]) -> object:
        """
        仅执行已登记命令，保持旧 SDK 的关键字参数约定。

        :param name: 已登记命令名称
        :param params: 命令参数
        :return: 插件返回值
        :raises ValueError: 命令未登记
        """
        handler = self._registered_commands().get(name)
        if handler is None:
            raise ValueError(f"插件命令未注册: {name}")
        return handler(**params)


class CommandPluginAdapter(HostPlugin):
    """
    将旧命令 SDK 实例接入宿主，不改变旧插件的构造参数或承诺其他 SDK 能力。

    命令和钩子都在启动器主进程执行，失败由既有生命周期边界报告。
    """

    def __init__(
        self, legacy: Plugin, framework: PluginManager, plugin_dir: Path, metadata: dict[str, Any], is_system: bool
    ) -> None:
        """
        为无参数 SDK 实例建立宿主插件身份。

        :param legacy: 已构造的旧命令 SDK 实例
        :param framework: 宿主插件管理器
        :param plugin_dir: 已校验归档代码目录
        :param metadata: 归档 JSON 边界元数据
        :param is_system: 是否属于系统插件
        """
        self.legacy = legacy
        super().__init__(framework, plugin_dir, metadata, is_system)

    def _hook(self, name: str) -> None:
        handler = getattr(self.legacy, name, None)
        if callable(handler):
            handler()

    def _invoke(self, name: str, **params: object) -> object:
        return self.legacy.invoke_command(name, params)

    def on_load(self) -> None:
        """
        调用旧实例的加载钩子并收集显式命令。
        """
        self._hook("on_load")
        self._commands = {name: partial(self._invoke, name) for name in self.legacy.command_names()}

    def on_enable(self) -> None:
        """
        将启用通知传给旧实例，不自动暴露其他宿主能力。
        """
        self._hook("on_enable")

    def on_disable(self) -> None:
        """
        将禁用通知传给旧实例，不清空第三方模块缓存。
        """
        self._hook("on_disable")

    def on_unload(self) -> None:
        """
        将卸载通知传给旧实例，由插件自身释放资源。
        """
        self._hook("on_unload")
