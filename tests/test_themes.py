# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 themes 模块的自动化测试。
#
# 公开接口：
#   - test_builtin_theme_ids_are_classic_and_folia() -> None
#   - test_normalize_theme_id_accepts_builtin_and_rejects_unknown() -> None
# ============================================================

from __future__ import annotations

from ECL.foundation.themes import ThemeCatalog, normalize_theme_id


def test_builtin_theme_ids_are_classic_and_folia() -> None:
    # 内置主题仅保留 classic 与 folia 两个皮肤标识。
    assert ThemeCatalog.builtin_theme_ids == ("classic", "folia")
    assert isinstance(ThemeCatalog.builtin_theme_ids, tuple)


def test_normalize_theme_id_accepts_builtin_and_rejects_unknown() -> None:
    # 内置主题标识原样返回。
    assert normalize_theme_id("classic") == "classic"
    assert normalize_theme_id("folia") == "folia"

    # 未知值、None 与大小写不一致的值一律回退到默认皮肤。
    assert normalize_theme_id("untrusted") == "classic"
    assert normalize_theme_id(None) == "classic"
    assert normalize_theme_id("CLASSIC") == "classic"
    assert normalize_theme_id("") == "classic"
