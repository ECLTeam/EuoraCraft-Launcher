# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示实例识别来源，只回传有界字段，不读取任何文件。
#
# 公开接口：
#   - class Plugin — 登记实例识别来源的示例插件。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    未获准的实例不会被投递，插件在这些实例上表现为无识别结果。
    """

    def on_load(self) -> None:
        """
        登记来源；标题会显示在实例来源列表里。
        """
        self.register_instance_source("demo", "示例来源", self.identify)

    def identify(self, context: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        按加载器回传一条元数据，字段值全部来自宿主给出的上下文。

        :param context: 含 root_ref、instance_rel、版本与加载器的上下文
        :return: 只含 fields 的元数据
        """
        loader = context.get("primary_loader")
        version = context.get("version_id")
        return {
            "fields": {
                "来源": "示例来源",
                "加载器": loader if isinstance(loader, str) else "未知",
                "版本": version if isinstance(version, str) else "未知",
            }
        }
