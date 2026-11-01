# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按实际平台提供受控文件对象，领域调用方不选择原生实现。
#
# 公开接口：
#   - FileBoundaryError / FileIdentity / FileInformation — 共同文件模型。
#   - FileDirectory / FileHandle — 当前平台的实际目录和文件实现。
# ============================================================

from __future__ import annotations

import sys

from ECL.utils.file_models import FileBoundaryError, FileIdentity, FileInformation

if sys.platform == "win32":
    from ECL.utils.windows_file_handles import FileDirectory, FileHandle
else:
    from ECL.utils.posix_file_handles import FileDirectory, FileHandle

__all__ = ("FileBoundaryError", "FileDirectory", "FileHandle", "FileIdentity", "FileInformation")
