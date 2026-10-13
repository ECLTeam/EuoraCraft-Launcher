# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：保留通用工具包的原子写入入口，实际实现由基础层统一维护。
#
# 公开接口：
#   - atomic_write_bytes — 原子写入字节。
#   - atomic_write_text — 原子写入文本。
# ============================================================

from __future__ import annotations

from ECL.foundation.files import atomic_write_bytes, atomic_write_text

__all__ = ["atomic_write_bytes", "atomic_write_text"]
