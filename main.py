# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：进程入口：解析命令行启动参数并启动 EuoraCraft Launcher启动器。
#
# 公开接口：
#   - run_launcher(argv=None) -> int
# ============================================================

import sys


def run_launcher(argv: list[str] | None = None) -> int:
    """
    解析命令行启动参数并运行一次启动器。

    参数解析先于后端导入：``--help``/``--version`` 在加载 pytauri 与日志
    系统之前退出，保证轻量响应且不产生任何文件副作用。

    :param argv: 待解析的参数列表；None 表示读取 ``sys.argv[1:]``
    :return: 本次运行的结果退出码
    :raises SystemExit: 参数用法错误或 ``--help``/``--version`` 时抛出
    """
    from ECL.cli import parse_launch_options

    options = parse_launch_options(sys.argv[1:] if argv is None else list(argv))
    # 延迟导入后端编排层，避免 --help/--version 早早退出时加载完整依赖。
    from ECL.launcher import EuoraCraftLauncher

    return int(EuoraCraftLauncher(options).run())


if __name__ == "__main__":
    sys.exit(run_launcher())
