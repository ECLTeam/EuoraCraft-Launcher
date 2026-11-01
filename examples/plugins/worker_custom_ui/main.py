# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示包内离线独立前端通过自身命令和设置工作。
#
# 公开接口：
#   - class Plugin — 专属原生窗口及自身 echo 命令示例。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    自定义前端不进入主窗口；未批准独立执行许可时不能打开。
    """

    def on_load(self) -> None:
        """
        面板只调用自身窗口命令，单文件 HTML 必须与已审阅快照一致。
        """
        self.host.call("settings.define", {"definitions": [{"key": "message", "type": "string", "default": "Hello"}]})
        self.define_command(
            "open",
            self.open_window,
            output_fields=tuple(
                {"name": key, "type": kind, "required": key != "error_code"}
                for key, kind in (
                    ("window_ref", "string"),
                    ("ready", "bool"),
                    ("closing", "bool"),
                    ("closed", "bool"),
                    ("error_code", "string"),
                )
            ),
        )
        self.define_command(
            "greet",
            self.greet,
            input_fields=({"name": "text", "type": "string", "max_length": 200},),
            output_fields=({"name": "text", "type": "string"},),
        )
        self.host.call(
            "panels.register",
            {
                "panels": [
                    {
                        "key": "main",
                        "title": "独立前端示例",
                        "nodes": [{"type": "button", "label": "打开独立窗口", "command": "open"}],
                    }
                ],
            },
        )

    def open_window(self) -> dict[str, JsonValue]:
        """
        请求专属原生窗口，策略和顶层引导完成后才返回可用状态。

        :return: 本运行随机窗口引用及实际状态
        """
        result = self.host.call("ui.windows.open", timeout=15)
        result.pop("error_code", None)
        return result

    def greet(self, text: str) -> dict[str, JsonValue]:
        """
        仅执行本插件自己的业务，不取得其他插件或宿主命令表。

        :param text: 有界输入
        :return: 问候文本
        """
        return {"text": "Hello, " + text}
