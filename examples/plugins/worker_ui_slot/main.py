# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示向主窗口插槽注册 HTML 片段与 Vue 组件，需要 ui.inject 授权。
#
# 公开接口：
#   - class Plugin — 可安装的界面注入示例插件。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin

HTML_SLOT = "plugin-slot-content-bottom"
VUE_SLOT = "plugin-slot-plugins-toolbar-after"


class Plugin(BasePlugin):
    """
    插槽内容与启动器同页渲染，示例只做静态展示，不读取任何宿主数据。
    """

    def on_load(self) -> None:
        """
        登记一段 HTML 与一个 Vue 组件；未获准 ui.inject 时加载直接失败。
        """
        self.define_html_slot(
            HTML_SLOT,
            '<div class="ecl-demo-slot">来自插件的 HTML 片段</div>',
            key="demo",
        )
        self.define_vue_slot(
            VUE_SLOT,
            "DemoSlotCard",
            '<span class="ecl-demo-slot">{{ message }}</span>',
            script="module.exports = { data() { return { message: '来自插件的 Vue 组件' } } }",
            key="demo",
        )
