# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示启动前追加参数与响应退出，需要 launch.hooks 授权。
#
# 公开接口：
#   - class Plugin — 登记 prepare 与 exit 两个启动钩子的示例插件。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    只追加固定参数，不读取实例目录，也不改动游戏命令的其他部分。
    """

    def on_load(self) -> None:
        """
        登记两个阶段：准备阶段追加参数，退出阶段只记录。
        """
        self.register_launch_hook("prepare", self.prepare)
        self.register_launch_hook("exit", self.on_game_exit)

    def prepare(self, context: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        追加一条 JVM 参数与一个环境变量，值取自宿主给出的版本标识。

        :param context: 有界启动上下文，含 version_id、loader 与隔离标记
        :return: 仅含追加项的参数修改
        """
        version = context.get("version_id")
        return {
            "jvm_args": ["-Ddemo.launch=1"],
            "env": {"ECL_DEMO_HOOK": version if isinstance(version, str) else "unknown"},
        }

    def on_game_exit(self, context: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        游戏退出时不做任何修改，只返回空结果。

        :param context: 有界启动上下文
        :return: 空字典
        """
        return {}
