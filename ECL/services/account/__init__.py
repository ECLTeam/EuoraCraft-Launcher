# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：账户与皮肤域子包：账户管理、外置登录、衣柜与头像导出。
#
# 公开接口：
#   - class AccountManager — 管理本地账户与当前选中账户。
#   - class AuthlibInjector — 维护外置登录注入组件。
#   - class WardrobeStore — 管理皮肤衣柜条目。
#   - class SkinAvatarExporter — 导出账户头像 PNG。
# ============================================================

from ECL.services.account.accounts import AccountManager, LauncherMicrosoftAccountManager
from ECL.services.account.authlib import AuthlibAccountManager, AuthlibInjector
from ECL.services.account.skin_avatar import SkinAvatarError, SkinAvatarExporter
from ECL.services.account.wardrobe import WardrobeError, WardrobeStore
from ECL.utils import AccountError, AuthlibError

__all__ = [
    "AccountError",
    "AccountManager",
    "AuthlibAccountManager",
    "AuthlibError",
    "AuthlibInjector",
    "LauncherMicrosoftAccountManager",
    "SkinAvatarError",
    "SkinAvatarExporter",
    "WardrobeError",
    "WardrobeStore",
]
