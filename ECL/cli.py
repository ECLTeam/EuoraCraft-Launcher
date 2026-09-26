# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：命令行启动参数：解析、校验并折算为会话级配置覆盖。
#
# 公开接口：
#   - class LaunchOptions — 一次运行的命令行启动参数。
#   - build_arg_parser() -> argparse.ArgumentParser — 构建启动参数解析器。
#   - parse_launch_options(argv) -> LaunchOptions — 解析并校验命令行启动参数。
#   - apply_launch_overrides(config, options) -> dict — 将启动参数叠加为会话级配置覆盖。
# ============================================================

from __future__ import annotations

import argparse
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ECL.common.version import __version__, __version_type__

_log_level_choices = ("debug", "info", "warning", "error")
_url_schemes = ("http://", "https://")


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """
    保存一次运行的命令行启动参数。

    所有参数仅在本次运行内生效，不写入 setting.json；数据目录在运行时信息
    解析阶段单独消费，其余参数经 :func:`apply_launch_overrides` 叠加到配置。
    """

    data_dir: Path | None = None  # --data-dir 指定的数据目录，None 表示沿用默认解析
    debug: bool = False  # --debug 是否开启调试模式
    log_level: str | None = None  # --log-level 指定的控制台日志级别
    disable_plugins: bool = False  # --disable-plugins 是否跳过插件的加载与启用
    frontend_dist: str | None = None  # --frontend-dist 指定的前端资源来源
    enable_dev_channel: bool = False  # --dev-channel 是否开启开发者通道


def build_arg_parser() -> argparse.ArgumentParser:
    """
    构建启动参数解析器。

    帮助文本与错误信息使用中文；``--help`` 与 ``--version`` 由 argparse
    打印信息后抛出 ``SystemExit(0)``，调用方无需重复处理。

    :return: 已注册全部启动参数的解析器
    """
    parser = argparse.ArgumentParser(
        prog="EuoraCraft Launcher",
        description="EuoraCraft Launcher 命令行启动参数。除 --help/--version 外，参数均仅在本次运行内生效，不写入配置文件。",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"EuoraCraft Launcher {__version__} ({__version_type__})",
        help="输出版本信息后退出",
    )
    parser.add_argument(
        "--data-dir",
        metavar="路径",
        help="指定本次运行的数据目录，目录不存在时自动创建",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启调试模式（控制台 DEBUG 输出与界面调试标签）",
    )
    parser.add_argument(
        "--log-level",
        choices=_log_level_choices,
        metavar="级别",
        help="控制台日志级别，可选值：" + "|".join(_log_level_choices),
    )
    parser.add_argument(
        "--disable-plugins",
        action="store_true",
        help="本次启动不加载、不启用任何插件（安全模式）",
    )
    parser.add_argument(
        "--frontend-dist",
        metavar="URL|路径",
        help="覆盖前端资源来源，支持 http(s):// 地址或本地目录",
    )
    parser.add_argument(
        "--dev-channel",
        action="store_true",
        help="开启开发者通道（供插件开发工具箱连接）",
    )
    return parser


def _resolve_data_dir(parser: argparse.ArgumentParser, raw: str | None) -> Path | None:
    # 校验并解析 --data-dir；已存在的路径必须是目录，不存在的目录允许自动创建。
    if not raw:
        return None
    data_path = Path(raw).expanduser().resolve()
    if data_path.exists() and not data_path.is_dir():
        parser.error(f"--data-dir 指向的路径不是目录: {data_path}")
    return data_path


def _resolve_frontend_dist(parser: argparse.ArgumentParser, raw: str | None) -> str | None:
    # 校验并解析 --frontend-dist；URL 原样保留，路径形态必须是已存在的目录。
    if not raw:
        return None
    if raw.startswith(_url_schemes):
        return raw
    frontend_path = Path(raw).expanduser().resolve()
    if not frontend_path.is_dir():
        parser.error(f"--frontend-dist 指向的本地目录不存在: {frontend_path}")
    return str(frontend_path)


def parse_launch_options(argv: Sequence[str]) -> LaunchOptions:
    """
    解析并校验命令行启动参数。

    ``--help``/``--version`` 在解析阶段打印信息后直接退出进程；
    路径类参数按不可信输入处理，非法取值以用法错误（退出码 2）终止。

    :param argv: 不含程序名的参数列表
    :return: 本次运行使用的启动参数
    :raises SystemExit: ``--help``/``--version`` 或参数校验失败时由 argparse 抛出
    """
    parser = build_arg_parser()
    args = parser.parse_args(list(argv))
    return LaunchOptions(
        data_dir=_resolve_data_dir(parser, args.data_dir),
        debug=bool(args.debug),
        log_level=args.log_level,
        disable_plugins=bool(args.disable_plugins),
        frontend_dist=_resolve_frontend_dist(parser, args.frontend_dist),
        enable_dev_channel=bool(args.dev_channel),
    )


def apply_launch_overrides(config: dict[str, Any], options: LaunchOptions | None) -> dict[str, Any]:
    """
    将启动参数折算为配置覆盖并叠加到配置副本上。

    覆盖作用于 ``launcher.debug``、``launcher.debug_log_level``、
    ``launcher.dev_channel`` 与 ``tauri.frontenddist``；调用时机在
    环境变量覆盖（``ECL_CONFIG_*``）之后，因此命令行优先级最高。
    本函数不修改传入的 config 对象。

    :param config: 已应用环境变量覆盖的完整配置
    :param options: 本次运行的启动参数；None 表示无覆盖
    :return: 叠加覆盖后的配置副本
    """
    result = deepcopy(config)
    if options is None:
        return result
    launcher_section = dict(result.get("launcher") or {})
    if options.debug:
        launcher_section["debug"] = True
    if options.log_level is not None:
        launcher_section["debug_log_level"] = options.log_level
    if options.enable_dev_channel:
        launcher_section["dev_channel"] = True
    if launcher_section:
        result["launcher"] = launcher_section
    if options.frontend_dist is not None:
        tauri_section = dict(result.get("tauri") or {})
        tauri_section["frontenddist"] = options.frontend_dist
        result["tauri"] = tauri_section
    return result


__all__ = ["LaunchOptions", "apply_launch_overrides", "build_arg_parser", "parse_launch_options"]
