# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_slots 模块的自动化测试。
#
# 公开接口：
#   - test_html_slot_appends_without_key_and_updates_matching_key() -> None
#   - test_vue_slot_keeps_distinct_components_from_the_same_plugin() -> None
# ============================================================

from ECL.plugins.manager import PluginManager
from ECL.utils import EventBus


def _framework() -> PluginManager:
    EventBus._instance = None
    EventBus._initialized = False
    PluginManager._instance = None
    PluginManager._initialized = False
    framework = PluginManager()
    framework._candidate_map["demo"] = {"is_system": True}
    return framework


def test_html_slot_appends_without_key_and_updates_matching_key() -> None:
    framework = _framework()

    framework._on_html_injected("demo", "sidebar-bottom", "first", None)
    framework._on_html_injected("demo", "sidebar-bottom", "second", None)
    framework._on_html_injected("demo", "sidebar-bottom", "old", "status")
    framework._on_html_injected("demo", "sidebar-bottom", "new", "status")

    assert framework.get_slots()["sidebar-bottom"] == [
        {"plugin": "demo", "html": "first"},
        {"plugin": "demo", "html": "second"},
        {"plugin": "demo", "html": "new", "key": "status"},
    ]


def test_vue_slot_keeps_distinct_components_from_the_same_plugin() -> None:
    framework = _framework()

    framework._on_vue_slot_registered("demo", "sidebar-bottom", "clock", "old", "", "")
    framework._on_vue_slot_registered("demo", "sidebar-bottom", "status", "status", "", "")
    framework._on_vue_slot_registered("demo", "sidebar-bottom", "clock", "new", "", "")

    entries = framework.get_vue_slots()["sidebar-bottom"]
    assert [entry["component_name"] for entry in entries] == ["clock", "status"]
    assert entries[0]["template"] == "new"
