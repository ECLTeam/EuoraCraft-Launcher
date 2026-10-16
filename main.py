# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在导入宿主前分流内部 Worker，将桌面或无头任务交给命令行模块。
#
# 公开接口：
#   - run_launcher(argv=None) -> int
# ============================================================

from __future__ import annotations

import sys


def run_launcher(argv: list[str] | None = None) -> int:
    """
    在桌面、单实例及业务服务导入前分流受信 Worker。

    :param argv: 不包含执行文件名的参数；None 使用当前进程参数
    :return: 所选运行模式的退出码
    """
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "--internal-plugin-worker":
        from ecl_plugin_api._worker import run_worker

        return run_worker(arguments[1:])
    from ECL.cli import run_launcher as run_desktop

    return run_desktop(arguments)


if __name__ == "__main__":
    sys.exit(run_launcher())
