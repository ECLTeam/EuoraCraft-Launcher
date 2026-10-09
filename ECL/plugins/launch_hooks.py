# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件启动回调：允许插件修改启动参数，并响应游戏进程启动和退出。
#
# 公开接口：
#   - class LaunchContext — 保存插件可读取或修改的游戏启动参数。
#   - class LaunchHookRegistry — 按注册顺序维护插件启动钩子，并在四个阶段安全调用。
#       - register(owner, name, on_prepare, pre_launch, post_launch, on_exit) -> None — 注册或原位更新一个启动钩子。
#       - unregister_owner(owner) -> None — 撤销指定插件注册的全部启动钩子。
#       - prepare(context) -> None — 参数准备阶段：允许插件追加 JVM 参数、游戏参数与环境变量。
#       - pre_launch(context) -> None — 进程创建前阶段。
#       - post_launch(context) -> None — 进程创建后阶段。
#       - on_exit(context) -> None — 游戏进程退出阶段。
# ============================================================

"""
插件启动回调：允许插件修改启动参数，并响应游戏进程启动和退出。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ECL.utils import get_logger


@dataclass
class LaunchContext:
    """
    保存插件可读取或修改的游戏启动参数。

    ``jvm_args``、``game_args`` 与 ``env`` 会在命令构建前交给插件修改；
    ``working_directory`` 为空时沿用启动器默认的游戏版本目录。
    """

    version_id: str  # 要启动的游戏版本。
    loader: str  # 当前加载器类型。
    game_path: Path  # 游戏实例根目录。
    game_directory: Path  # 默认工作目录。
    version_isolation: bool  # 是否启用版本隔离。
    jvm_args: list[str] = field(default_factory=list)  # 可由钩子追加的 JVM 参数。
    game_args: list[str] = field(default_factory=list)  # 可由钩子追加的游戏参数。
    env: dict[str, str] = field(default_factory=dict)  # 可由钩子覆写的环境变量。
    working_directory: Path | None = None  # 可选的工作目录覆盖值。


class LaunchHookRegistry:
    """
    按注册顺序维护插件启动钩子，并在四个阶段安全调用。

    单个钩子抛出的异常会被隔离并记录，不会中断其余钩子或启动流程。
    插件禁用、卸载时宿主会调用 ``unregister_owner`` 撤销其全部钩子。
    """

    def __init__(self) -> None:
        self._hooks: list[dict[str, Any]] = []  # 按注册顺序保存的启动钩子。
        self._logger = get_logger("LaunchHookRegistry")  # 扩展点日志器。

    def register(
        self,
        owner: str,
        name: str,
        *,
        on_prepare: Callable[[LaunchContext], Any] | None = None,
        pre_launch: Callable[[LaunchContext], Any] | None = None,
        post_launch: Callable[[LaunchContext], Any] | None = None,
        on_exit: Callable[[LaunchContext], Any] | None = None,
    ) -> None:
        """
        注册或原位更新一个启动钩子。

        :param owner: 插件名
        :param name: 稳定的钩子标识
        :param on_prepare: 游戏启动前的回调，可修改 JVM 参数、游戏参数和环境变量
        :param pre_launch: 进程创建前回调
        :param post_launch: 进程创建后回调
        :param on_exit: 游戏进程退出回调
        """
        entry = {
            "owner": owner,
            "name": name,
            "on_prepare": on_prepare,
            "pre_launch": pre_launch,
            "post_launch": post_launch,
            "on_exit": on_exit,
        }
        for existing in self._hooks:
            if existing["owner"] == owner and existing["name"] == name:
                existing.update(entry)
                return
        self._hooks.append(entry)

    def unregister_owner(self, owner: str) -> None:
        """
        撤销指定插件注册的全部启动钩子。
        """
        self._hooks = [hook for hook in self._hooks if hook["owner"] != owner]

    def _call(self, phase: str, context: LaunchContext) -> None:
        # 调用当前阶段的钩子，并隔离单个插件的异常。
        for hook in list(self._hooks):
            callback = hook.get(phase)
            if callback is None:
                continue
            try:
                callback(context)
            except Exception:
                self._logger.exception("插件启动钩子 %s 失败：插件：%s；钩子：%s", phase, hook["owner"], hook["name"])

    def prepare(self, context: LaunchContext) -> None:
        """
        参数准备阶段：允许插件追加 JVM 参数、游戏参数与环境变量。
        """
        self._call("on_prepare", context)

    def pre_launch(self, context: LaunchContext) -> None:
        """
        进程创建前阶段。
        """
        self._call("pre_launch", context)

    def post_launch(self, context: LaunchContext) -> None:
        """
        进程创建后阶段。
        """
        self._call("post_launch", context)

    def on_exit(self, context: LaunchContext) -> None:
        """
        游戏进程退出阶段。
        """
        self._call("on_exit", context)
