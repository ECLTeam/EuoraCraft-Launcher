# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：供独立 Worker 内的归档插件声明可由启动器调用的命令。
#
# 公开接口：
#   - class Plugin — 为归档插件提供命令装饰器、注册和调用契约。
# ============================================================

from __future__ import annotations

from collections.abc import Callable


class Plugin:
    """
    为归档插件提供独立于启动器宿主解释器的命令接口。

    插件可在实例化或 `on_load` 中注册命令；只有这些命令会跨进程暴露。
    此接口用于依赖隔离，不限制插件进程的操作系统权限。
    """

    def __init__(self) -> None:
        """
        创建实例级命令表，避免不同插件实例共享动态注册项。
        """
        self._commands: dict[str, Callable[..., object]] = {}

    @staticmethod
    def on_command(name: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
        """
        标记一个实例方法为可由宿主调用的命令。

        :param name: 插件内唯一的命令名称，不包含插件名前缀
        :return: 保留原方法的装饰器
        :raises ValueError: 名称为空、过长或包含命令分隔符时抛出
        """
        Plugin._validate_name(name)

        def decorate(handler: Callable[..., object]) -> Callable[..., object]:
            handler._ecl_command_name = name
            return handler

        return decorate

    def register_command(self, name: str, handler: Callable[..., object]) -> None:
        """
        在实例化或加载阶段动态登记一个命令。

        登记结果只在 Worker 加载完成后公布；后续生命周期阶段不应变更命令表。

        :param name: 插件内唯一的命令名称
        :param handler: 在 Worker 中执行的可调用对象
        :raises ValueError: 名称或处理器无效时抛出
        """
        self._validate_name(name)
        if not callable(handler):
            raise ValueError("插件命令处理器必须可调用")
        if not hasattr(self, "_commands"):
            self._commands = {}
        self._commands[name] = handler

    def command_names(self) -> list[str]:
        """
        返回当前已声明命令名称，供 Worker 握手公布。

        :return: 排序后的命令名称
        """
        return sorted(self._registered_commands())

    def invoke_command(self, name: str, params: dict[str, object]) -> object:
        """
        仅调用已登记的命令，不允许由宿主指定任意插件方法。

        :param name: 插件内的命令名称
        :param params: JSON 对象形式的关键字参数
        :return: 可编码为 JSON 的命令结果
        :raises ValueError: 命令未登记时抛出
        """
        handler = self._registered_commands().get(name)
        if handler is None:
            raise ValueError(f"插件命令未注册: {name}")
        return handler(**params)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name or name != name.strip() or ":" in name or len(name) > 128:
            raise ValueError("插件命令名称无效")

    def _registered_commands(self) -> dict[str, Callable[..., object]]:
        handlers: dict[str, Callable[..., object]] = {}
        for method_name in dir(type(self)):
            method = getattr(type(self), method_name)
            command_name = getattr(method, "_ecl_command_name", None)
            if command_name is not None:
                handlers[command_name] = getattr(self, method_name)
        handlers.update(getattr(self, "_commands", {}))
        return handlers
