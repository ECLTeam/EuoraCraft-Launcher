# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义共享安装目标的占位身份，执行状态仍由原安装或任务服务维护。
#
# 公开接口：
#   - class InstallReservation — 同一物理游戏根与实例名的一次独占使用凭据。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ECL.utils.instance_reading import DirectoryIdentity

if TYPE_CHECKING:
    from ECL.utils.file_handles import FileDirectory


@dataclass(frozen=True, slots=True)
class InstallReservation:
    """
    只标识目标占用，不复制安装进度或任务终态。
    """

    root_path: Path
    root_identity: DirectoryIdentity
    version_name: str
    comparison_name: str
    token: str
    handles: list[FileDirectory] = field(default_factory=list, compare=False, hash=False, repr=False)
