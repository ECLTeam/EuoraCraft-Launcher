# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证插件列表展示条目对 manifest 注册信息（homepage/license）的透传。
#
# 公开接口：
#   - test_plugin_entry_passes_through_registered_info() — 校验两条元数据来源的透传与默认值。
# ============================================================

from __future__ import annotations

from ECL.plugins.manager.registry import _plugin_entry


def test_plugin_entry_passes_through_registered_info() -> None:
    dict_entry = _plugin_entry(
        "example",
        {
            "title": "示例插件",
            "version": "1.0.0",
            "description": "示例",
            "author": "ECLTeam",
            "homepage": "https://github.com/ECLTeam/ECL-Plugin-Example",
            "license": "GPL-3.0",
        },
        "enabled",
        None,
    )
    assert dict_entry["homepage"] == "https://github.com/ECLTeam/ECL-Plugin-Example"
    assert dict_entry["license"] == "GPL-3.0"

    minimal = _plugin_entry("example", {"title": "示例"}, "unloaded", None)
    assert minimal["homepage"] == ""
    assert minimal["license"] == ""

    fallback = _plugin_entry("example", None, "unloaded", None)
    assert fallback["homepage"] == ""
    assert fallback["license"] == ""
