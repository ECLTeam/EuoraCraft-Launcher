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

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ECL.utils import get_logger

# 第三方钩子返回的参数修改上限：一次回调不能塞入巨量参数拖垮命令行。
remote_limit_args = 32
remote_limit_arg_length = 512
remote_limit_env = 32
_remote_env_key = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


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


def apply_remote_modifications(context: LaunchContext, payload: Mapping[str, Any]) -> None:
    """
    把第三方钩子返回的参数修改应用到启动上下文，越界整体拒绝。

    只接受追加 JVM 参数、追加游戏参数与覆写环境变量三类修改；任何一项
    越界都不应用，避免半份修改让启动参数处于不可解释状态。

    :param context: 当前启动上下文
    :param payload: 钩子返回的修改描述
    :raises ValueError: 字段类型、数量或长度越界时抛出
    """
    for field_name, limit in (("jvm_args", remote_limit_args), ("game_args", remote_limit_args)):
        value = payload.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list) or len(value) > limit:
            raise ValueError(f"{field_name} 数量无效")
        if any(not isinstance(item, str) or len(item) > remote_limit_arg_length for item in value):
            raise ValueError(f"{field_name} 内容无效")
        getattr(context, field_name).extend(value)
    env = payload.get("env")
    if env is None:
        return
    if not isinstance(env, Mapping) or len(env) > remote_limit_env:
        raise ValueError("env 数量无效")
    for key, value in env.items():
        if not isinstance(key, str) or _remote_env_key.fullmatch(key) is None:
            raise ValueError("env 键无效")
        if not isinstance(value, str) or len(value) > remote_limit_arg_length:
            raise ValueError("env 值无效")
    context.env.update(env)


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
        required: bool = False,
    ) -> None:
        """
        注册或原位更新一个启动钩子。

        :param owner: 插件名
        :param name: 稳定的钩子标识
        :param on_prepare: 游戏启动前的回调，可修改 JVM 参数、游戏参数和环境变量
        :param pre_launch: 进程创建前回调
        :param post_launch: 进程创建后回调
        :param on_exit: 游戏进程退出回调
        :param required: 参数准备或进程创建前失败时是否阻止本次启动
        """
        entry = {
            "owner": owner,
            "name": name,
            "on_prepare": on_prepare,
            "pre_launch": pre_launch,
            "post_launch": post_launch,
            "on_exit": on_exit,
            "required": required,
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
                # 必要前置失败必须阻止创建进程；后置与退出阶段只记录，不影响游戏。
                if hook.get("required") and phase in {"on_prepare", "pre_launch"}:
                    raise

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
