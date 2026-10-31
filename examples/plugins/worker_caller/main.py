# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示发现并调用其他插件导出的命令，调用失败时明确报错。
#
# 公开接口：
#   - class Plugin — 调用 worker_provider 的示例调用方。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import HostApiError
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    通过宿主通道调用其他插件的导出命令，不直接持有对方对象。
    """

    def on_load(self) -> None:
        """
        登记本地命令，由宿主按输入 schema 校验后调用。
        """
        self.define_command(
            "call_target",
            self.call_target,
            input_fields=({"name": "text", "type": "string", "max_length": 200},),
            output_fields=({"name": "text", "type": "string"},),
        )

    def call_target(self, text: str) -> dict[str, str]:
        """
        调用 worker_provider 的 echo 命令并返回其结果。

        :param text: 传给目标插件的文本
        :return: 目标插件返回的 text
        :raises HostApiError: 目标未启用或未导出该命令时抛出
        """
        result = self.call_plugin_command("worker_provider", "echo", {"text": text})
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise HostApiError("目标插件返回值无效", "PLUGIN_SCHEMA_INVALID")
        return {"text": result["text"]}
