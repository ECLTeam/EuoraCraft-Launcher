# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示崩溃分析回调，按进程输出补充一条原因，需要游戏授权。
#
# 公开接口：
#   - class Plugin — 登记崩溃分析回调的示例插件。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    只在输出里出现内存不足特征时补充原因，不覆盖宿主已有结论。
    """

    def on_load(self) -> None:
        """
        登记分析回调；上下文不含路径，插件无从读取实例目录。
        """
        self.register_crash_analyzer(self.analyze)

    def analyze(self, context: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        按输出特征返回一条附加原因。

        :param context: 含 report_id、version_id、退出码与截断输出的上下文
        :return: 只含 reasons 的附加结果，无匹配时为空字典
        """
        output = context.get("output")
        if not isinstance(output, str) or "OutOfMemoryError" not in output:
            return {}
        return {
            "reasons": [
                {
                    "title": "内存不足",
                    "detail": "游戏进程因堆内存耗尽退出，可尝试提高最大内存。",
                    "severity": "error",
                    "source": "worker_crash_analyzer",
                }
            ]
        }
