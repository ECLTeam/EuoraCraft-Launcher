# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示 Worker 插件通过 Host API 登记命令并读取自身设置，不取得宿主对象。
#
# 公开接口：
#   - class Plugin — 可安装的 API v2 命令与设置示例。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import HostApiError
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    在用户审阅授权后登记问候命令，所有业务能力由当前运行会话校验。
    """

    def on_load(self) -> None:
        """
        定义自身设置和命令；任何必要权限缺失都会使加载失败。
        """
        self.host.call(
            "settings.define",
            {"definitions": [{"key": "prefix", "type": "string", "default": "ECL", "description": "问候前缀"}]},
        )
        self.define_command(
            "greet",
            self.greet,
            input_fields=({"name": "name", "type": "string", "max_length": 120},),
            output_fields=({"name": "message", "type": "string"},),
        )

    def greet(self, name: str) -> dict[str, str]:
        """
        通过 Host API 读取本插件设置并返回已登记的命令结果。

        :param name: 已通过宿主输入 schema 校验的称呼
        :return: 不含宿主凭据或对象的问候消息
        :raises HostApiError: 宿主返回的设置结构无效或权限被撤销时抛出
        """
        result = self.host.call("settings.get", {"keys": ["prefix"]})
        values = result.get("values")
        if not isinstance(values, dict) or not isinstance(values.get("prefix"), str):
            raise HostApiError("宿主设置结果无效", "PLUGIN_SCHEMA_INVALID")
        return {"message": f"{values['prefix']}: {name}"}
