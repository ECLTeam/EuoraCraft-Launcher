# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：统一发行物入口，按需进入桌面、有限无头任务或无头插件角色。
#
# 公开接口：
#   - run_launcher(argv=None) -> int
# ============================================================

from __future__ import annotations

import sys

from ECL.bootstrap import run_process


def run_launcher(argv: list[str] | None = None) -> int:
    """
    在桌面、单实例及业务服务导入前确定进程角色并按需加载。

    :param argv: 不包含执行文件名的参数；None 使用当前进程参数
    :return: 所选运行模式的退出码
    """
    return run_process(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(run_launcher())
