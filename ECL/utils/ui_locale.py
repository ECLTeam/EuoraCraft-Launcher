# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：界面语言判定，供仅面向简体中文用户的功能统一判断是否启用。
#
# 公开接口：
#   - simplified_chinese_locale — 唯一提供国内专属功能的界面语言。
#   - is_simplified_chinese(configuration) -> bool — 判断有效配置的界面语言是否为简体中文。
# ============================================================

from __future__ import annotations

from collections.abc import Mapping

# 下载源、模组源与联机功能只面向简体中文用户。
simplified_chinese_locale = "zh-CN"


def is_simplified_chinese(configuration: Mapping[str, object]) -> bool:
    """
    判断当前界面语言是否为简体中文。

    缺少界面语言配置时按默认的简体中文处理，避免旧配置丢失功能入口。
    本函数只做判断，不读取也不改写配置文件。

    :param configuration: 启动器有效配置
    :return: 简体中文界面为 True
    """
    ui_config = configuration.get("ui")
    locale = ui_config.get("locale") if isinstance(ui_config, Mapping) else None
    return locale is None or locale == simplified_chinese_locale
