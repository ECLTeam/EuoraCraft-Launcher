# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示导出命令供其他插件调用，不读取宿主数据。
#
# 公开接口：
#   - class Plugin — 导出 echo 命令的示例提供方。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    只导出无副作用的回显命令，调用方拿不到任何宿主资源。
    """

    def on_load(self) -> None:
        """
        登记导出命令；导出即表示允许其他已启用插件调用。
        """
        self.define_command(
            "echo",
            self.echo,
            input_fields=({"name": "text", "type": "string", "max_length": 200},),
            output_fields=({"name": "text", "type": "string"},),
            exported=True,
        )

    def echo(self, text: str) -> dict[str, str]:
        """
        原样返回调用方文本。

        :param text: 已通过输入 schema 校验的文本
        :return: 含 text 的输出
        """
        return {"text": text}
