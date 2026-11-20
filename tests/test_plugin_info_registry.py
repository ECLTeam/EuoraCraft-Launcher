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

import base64
from pathlib import Path

from ECL.plugins.manager.registry import PluginRegistry, _plugin_entry
from ECL.utils import get_logger


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


def _registry_for_icon() -> PluginRegistry:
    registry = object.__new__(PluginRegistry)
    registry.logger = get_logger("PluginManager")
    return registry


def test_icon_data_url_reads_declared_image(tmp_path: Path) -> None:
    # 清单声明图标时编码为 data URL；缺失、超限或未声明都回退空串。
    registry = _registry_for_icon()
    icon = tmp_path / "icon.webp"
    icon.write_bytes(b"pixel")

    url = registry._icon_data_url(tmp_path, {"icon": "icon.webp"})
    assert url.startswith("data:image/webp;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"pixel"

    assert registry._icon_data_url(tmp_path, {"icon": "missing.webp"}) == ""
    assert registry._icon_data_url(None, {"icon": "icon.webp"}) == ""
    assert registry._icon_data_url(tmp_path, {}) == ""


def test_icon_data_url_rejects_oversized_image(tmp_path: Path) -> None:
    registry = _registry_for_icon()
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"x" * (registry.max_icon_bytes + 1))

    assert registry._icon_data_url(tmp_path, {"icon": "icon.png"}) == ""
