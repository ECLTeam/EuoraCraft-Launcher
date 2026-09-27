# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：崩溃分析兼容门面：分析器实现位于 crash 子包。
#
# 公开接口：
#   - CrashAnalyzer — re-export，见 ECL.services.game.crash.analyzer。
# ============================================================

from __future__ import annotations

from .crash.analyzer import CrashAnalyzer

__all__ = ["CrashAnalyzer"]
