# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：统一进程入口，将桌面或无头任务的启动请求交给命令行模块。
#
# 公开接口：
#   - run_launcher(argv=None) -> int
# ============================================================

import sys

from ECL.cli import run_launcher

if __name__ == "__main__":
    sys.exit(run_launcher())
