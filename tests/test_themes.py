# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 themes 模块的自动化测试。
#
# 公开接口：
#   - test_builtin_theme_ids_cover_skins_and_catppuccin_flavors() -> None
#   - test_normalize_theme_id_accepts_builtin_and_rejects_unknown() -> None
# ============================================================

from __future__ import annotations

from ECL.foundation.themes import ThemeCatalog, normalize_theme_id


def test_builtin_theme_ids_cover_skins_and_catppuccin_flavors() -> None:
    # 内置主题包含两个布局皮肤与 Catppuccin 四个风味。
    assert ThemeCatalog.builtin_theme_ids == (
        "classic",
        "folia",
        "catppuccin-latte",
        "catppuccin-frappe",
        "catppuccin-macchiato",
        "catppuccin-mocha",
    )
    assert isinstance(ThemeCatalog.builtin_theme_ids, tuple)


def test_normalize_theme_id_accepts_builtin_and_rejects_unknown() -> None:
    # 内置主题标识原样返回。
    assert normalize_theme_id("classic") == "classic"
    assert normalize_theme_id("folia") == "folia"
    assert normalize_theme_id("catppuccin-mocha") == "catppuccin-mocha"

    # 未知值、None 与大小写不一致的值一律回退到默认皮肤。
    assert normalize_theme_id("untrusted") == "classic"
    assert normalize_theme_id("catppuccin") == "classic"
    assert normalize_theme_id(None) == "classic"
    assert normalize_theme_id("CLASSIC") == "classic"
    assert normalize_theme_id("") == "classic"
