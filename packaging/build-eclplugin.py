# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为插件作者提供无签名 .eclplugin 归档的命令行制作入口。
#
# 公开接口：
#   - main(argv) -> int — 解析源目录与输出路径，构建并验证插件归档。
# ============================================================

from __future__ import annotations

import argparse
from pathlib import Path

from ECL.plugins.package_archive import PluginPackageError, build_plugin_package


def main(argv: list[str] | None = None) -> int:
    """
    从插件目录制作未签名的 `.eclplugin` 文件并打印校验结果。

    此命令不会解析或下载 Python 依赖；作者须预先准备目标锁文件与离线 wheels。

    :param argv: 可选的命令行参数；缺省时从当前进程读取
    :return: 成功为 0，归档制作失败为 1
    """
    parser = argparse.ArgumentParser(description="制作未签名的 EuoraCraft 插件归档")
    parser.add_argument("source", type=Path, help="包含 plugin.json 的插件源目录")
    parser.add_argument("output", type=Path, help="输出 .eclplugin 文件")
    args = parser.parse_args(argv)
    try:
        info = build_plugin_package(args.source, args.output)
    except PluginPackageError as exc:
        parser.exit(1, f"插件打包失败: {exc}\n")
    print(f"已制作 {args.output}: {info.name} v{info.version}，{info.file_count} 个文件（来源未验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
