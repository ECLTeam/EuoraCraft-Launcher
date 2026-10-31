# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义账户领域提供给受信读取边界的最小不可变摘要，不包含认证材料。
#
# 公开接口：
#   - class AccountSummary — 宿主授权目录使用的账户快照值。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """
    保存玩家展示信息与宿主内部账户身份，后者不直接交给 Worker。
    """

    account_id: str
    alias: str
    uuid: str
    kind: str
    is_current: bool
