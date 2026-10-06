# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：内置主题标识。前端根据 ui.theme.theme_id 应用皮肤，无需后端服务。
#
# 公开接口：
#   - class ThemeCatalog — 内置主题标识。
#   - normalize_theme_id(value) -> str
# ============================================================

"""内置主题标识。前端根据 ui.theme.theme_id 应用皮肤，无需后端服务。"""


class ThemeCatalog:
    """
    集中管理启动器内置主题的稳定标识。
    """

    builtin_theme_ids = ("classic", "folia")


def normalize_theme_id(value: object) -> str:
    return value if value in ThemeCatalog.builtin_theme_ids else "classic"


__all__ = ["ThemeCatalog", "normalize_theme_id"]
