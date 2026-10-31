# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示声明式操作面板的六种节点、命令动作与数据回调，不注入界面脚本。
#
# 公开接口：
#   - class Plugin — 可安装的面板示例插件。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import HostApiError
from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    面板由宿主渲染，插件只描述节点并响应自己的命令与数据请求。
    """

    def __init__(self, host) -> None:
        super().__init__(host)
        self._greet_count = 0

    def on_load(self) -> None:
        """
        先定义设置，再登记命令与面板；面板引用未定义设置会被宿主拒绝。
        """
        self.host.call(
            "settings.define",
            {
                "definitions": [
                    {
                        "key": "greeting",
                        "type": "string",
                        "default": "你好",
                        "description": "问候前缀",
                    },
                    {
                        "key": "detailed",
                        "type": "bool",
                        "default": False,
                        "description": "在列表里显示详细说明",
                    },
                ]
            },
        )
        self.define_command(
            "greet",
            self.greet,
            input_fields=({"name": "name", "type": "string", "max_length": 120},),
            output_fields=({"name": "message", "type": "string"},),
        )
        self.define_panel(
            "main",
            "面板示例",
            nodes=(
                {"type": "text", "text": "这个面板由宿主渲染，插件只提供节点描述。"},
                {
                    "type": "group",
                    "title": "执行命令",
                    "children": (
                        {
                            "type": "button",
                            "label": "问候一次",
                            "command": "greet",
                            "args": {"name": "面板"},
                            "tone": "primary",
                            "confirm": "确认执行一次问候命令？",
                        },
                    ),
                },
                {"type": "setting", "key": "greeting"},
                {"type": "setting", "key": "detailed"},
                {
                    "type": "list",
                    "source": "history",
                    "columns": ({"key": "index", "label": "序号"}, {"key": "message", "label": "结果"}),
                },
                {"type": "task", "source": "progress"},
            ),
            description="演示文本、分组、按钮、设置、列表与任务进度六种节点。",
            data_callback=self.panel_data,
        )

    def greet(self, name: str) -> dict[str, str]:
        """
        读取自身设置并记录一次调用，结果由宿主按输出 schema 校验。

        :param name: 已通过宿主输入 schema 校验的称呼
        :return: 不含宿主凭据或对象的问候消息
        :raises HostApiError: 设置读取失败或授权被撤销时抛出
        """
        result = self.host.call("settings.get", {"keys": ["greeting"]})
        values = result.get("values")
        if not isinstance(values, dict) or not isinstance(values.get("greeting"), str):
            raise HostApiError("宿主设置结果无效", "PLUGIN_SCHEMA_INVALID")
        self._greet_count += 1
        return {"message": f"{values['greeting']}，{name}（第 {self._greet_count} 次）"}

    def panel_data(self) -> JsonValue:
        """
        返回列表与任务快照，宿主在每次打开面板或执行动作后按需拉取。

        :return: 仅含字符串行数据与有界任务状态的字典
        """
        rows = [
            {"index": str(index), "message": f"第 {index} 次问候已完成"} for index in range(1, self._greet_count + 1)
        ]
        return {
            "lists": {"history": rows},
            "tasks": {
                "progress": {
                    "state": "succeeded" if self._greet_count else "pending",
                    "percent": 100 if self._greet_count else 0,
                    "detail": f"已执行 {self._greet_count} 次",
                }
            },
        }
