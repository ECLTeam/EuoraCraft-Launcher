# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示清单明确拒绝源码保存触发的自动重载，保留人工重载能力。
#
# 公开接口：
#   - class Plugin — 登记可观察代码版本的命令和面板。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    以静态清单退出自动换代，无需运行回调才能发现支持状态。
    """

    code_version: str = "v1"

    def on_load(self) -> None:
        """
        提交版本命令与说明面板，不创建后台工作或持久化数据。
        """
        self.define_command("version", self.version, output_fields=({"name": "version", "type": "string"},))
        self.define_panel("main", "当前代码版本", ({"type": "text", "text": self.code_version},))

    def version(self) -> dict[str, str]:
        """
        返回本运行实际加载的版本文字，源码保存不会自行改变该值。

        :return: 当前代码版本
        """
        return {"version": self.code_version}
