# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示开发模式换代与统一设置保留，不操作游戏、账户或任意用户文件。
#
# 公开接口：
#   - class Plugin — 提供可观察的版本命令与统一配置面板。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    修改代码版本即可观察 Worker 换代，配置由宿主统一维护。
    """

    code_version: str = "v1"

    def on_load(self) -> None:
        """
        注册完整设置定义及只读结果命令，重载不清空已有用户值。
        """
        self.host.call(
            "settings.define",
            {
                "definitions": [
                    {"key": "greeting", "type": "string", "default": "Hello", "description": "问候内容"},
                    {"key": "notifications", "type": "bool", "default": True, "description": "允许提醒"},
                    {"key": "interval", "type": "number", "default": 10, "description": "间隔秒数"},
                    {
                        "key": "mode",
                        "type": "select",
                        "default": "quiet",
                        "options": ["quiet", "normal"],
                        "description": "提示方式",
                    },
                ]
            },
        )
        self.define_command("status", self.status, output_fields=({"name": "message", "type": "string"},))
        self.define_panel(
            "development",
            "开发示例",
            (
                {"type": "text", "text": f"当前代码版本：{self.code_version}"},
                {"type": "setting", "key": "greeting"},
                {"type": "button", "label": "读取当前版本与配置", "command": "status"},
            ),
        )

    def status(self) -> dict[str, str]:
        """
        将实际运行版本与持久配置一起返回，供命令和真实 Worker 测试观察。

        :return: 当前版本及问候文本
        """
        view = self.host.call("settings.get", {"keys": ["greeting"]})
        values = view.get("values")
        if not isinstance(values, dict) or not isinstance(values.get("greeting"), str):
            raise ValueError("插件设置响应无效")
        return {"message": f"{self.code_version}: {values['greeting']}"}
