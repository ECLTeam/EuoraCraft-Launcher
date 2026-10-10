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
#   - run_launcher(argv=None) -> int — 解析参数并运行一次启动器。
#   - apply_launch_overrides(config, options) -> dict — 将启动参数叠加为会话级配置覆盖。
# ============================================================

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ECL.foundation.version import __version__, __version_type__

_log_level_choices = ("debug", "info", "warning", "error")
_url_schemes = ("http://", "https://")


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    """
    保存一次运行的命令行启动参数。

    所有参数仅在本次运行内生效，不写入 setting.json；数据目录在运行时信息
    解析阶段单独消费，其余参数经 :func:`apply_launch_overrides` 叠加到配置。
    ``launch_target``/``server_target``/``world_target`` 为一次性动作参数，
    前端就绪后才处理这些选项，不用它们覆盖配置文件。
    """

    forwarded_argv: tuple[str, ...] = field(default=(), compare=False, repr=False)
    data_dir: Path | None = None  # --data-dir 指定的数据目录，None 表示沿用默认解析
    debug: bool = False  # --debug 是否开启调试模式
    log_level: str | None = None  # --log-level 指定的控制台日志级别
    disable_plugins: bool = False  # --disable-plugins 是否跳过插件的加载与启用
    frontend_dist: str | None = None  # --frontend-dist 指定的前端资源来源
    enable_dev_channel: bool = False  # --dev-channel 是否开启开发者通道
    launch_target: str | None = None  # --launch 指向的实例（版本名或实例目录）
    server_target: str | None = None  # --server 指定的快捷进入服务器地址
    world_target: str | None = None  # --world 指定的快捷进入世界 ID
    game_dir: Path | None = None
    java_path: Path | None = None
    memory: int | None = None
    width: int | None = None
    height: int | None = None
    fullscreen: bool | None = None
    isolation_mode: str | None = None
    process_priority: str | None = None
    lock_memory: bool | None = None
    jvm_args: tuple[str, ...] = ()
    game_args: tuple[str, ...] = ()
    launcher_visibility: str | None = None
    open_page: str | None = None

    def game_overrides(self) -> dict[str, object]:
        """
        生成本次显式游戏启动覆盖，不包含进程配置或缺省字段。

        :return: 等待启动流程校验的命令行选项
        """
        values: dict[str, object] = {}
        for name in (
            "java_path",
            "memory",
            "width",
            "height",
            "fullscreen",
            "process_priority",
            "lock_memory",
            "launcher_visibility",
        ):
            value = getattr(self, name)
            if value is not None:
                values[name] = str(value) if isinstance(value, Path) else value
        if self.isolation_mode in {"enabled", "disabled"}:
            values["version_isolation"] = self.isolation_mode == "enabled"
        if self.jvm_args:
            values["jvm_args"] = list(self.jvm_args)
        if self.game_args:
            values["game_args"] = list(self.game_args)
        return values


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
    parser.add_argument(
        "--launch",
        metavar="实例",
        help="快捷启动实例，取值为版本名或 <游戏根>/versions/<版本名> 目录",
    )
    parser.add_argument(
        "--server",
        metavar="地址[:端口]",
        help="配合 --launch 使用：启动后直连该服务器",
    )
    parser.add_argument(
        "--world",
        metavar="世界ID",
        help="配合 --launch 使用：启动后快速进入该世界",
    )
    parser.add_argument("--game-dir", metavar="目录", help="配合 --launch 版本ID指定游戏根目录")
    parser.add_argument("--java-path", metavar="可执行文件", help="本次启动使用的 Java")
    parser.add_argument("--memory", type=int, metavar="MB", help="本次内存（512—65536 MB）")
    parser.add_argument("--width", type=int, help="窗口宽度（320—16384）")
    parser.add_argument("--height", type=int, help="窗口高度（240—16384）")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--fullscreen", dest="fullscreen", action="store_true", default=None, help="本次全屏")
    display.add_argument("--windowed", dest="fullscreen", action="store_false", help="本次窗口模式")
    parser.add_argument(
        "--isolation", choices=("inherit", "enabled", "disabled"), help="本次隔离覆盖，inherit 沿用实例设置"
    )
    parser.add_argument(
        "--process-priority",
        choices=("idle", "below_normal", "normal", "above_normal", "high"),
        help="本次游戏进程优先级",
    )
    memory_lock = parser.add_mutually_exclusive_group()
    memory_lock.add_argument(
        "--lock-memory", dest="lock_memory", action="store_true", default=None, help="锁定初始内存"
    )
    memory_lock.add_argument("--no-lock-memory", dest="lock_memory", action="store_false", help="不锁定初始内存")
    parser.add_argument(
        "--jvm-arg",
        action="append",
        default=[],
        metavar="参数",
        help="追加 JVM 参数，可重复，推荐 --jvm-arg=-Dkey=value",
    )
    parser.add_argument("--game-arg", action="append", default=[], metavar="参数", help="追加游戏参数，可重复")
    parser.add_argument(
        "--launcher-visibility", choices=("none", "minimize", "quit"), help="游戏启动成功后的启动器行为"
    )
    parser.add_argument(
        "--open-page",
        choices=("games", "instances", "download", "settings", "more"),
        help="打开指定页面，与 --launch 互斥",
    )
    return parser


def _validate_quick_target(parser: argparse.ArgumentParser, flag: str, value: str | None) -> str | None:
    # 校验 --server/--world 的取值；服务器地址不允许空白，世界 ID 允许空格。
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or "\0" in cleaned:
        parser.error(f"{flag} 的取值不能为空")
    if flag == "--server" and any(char.isspace() for char in cleaned):
        parser.error(f"{flag} 的取值不能包含空白字符: {cleaned}")
    return cleaned


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


def _validate_game_options(
    parser: argparse.ArgumentParser, args: argparse.Namespace, launch_target: str | None
) -> None:
    """
    校验启动动作依赖及选项范围，不加载游戏或桌面依赖。
    """
    if args.open_page and launch_target:
        parser.error("--open-page 与 --launch 不能同时使用")
    options = (
        args.game_dir,
        args.java_path,
        args.memory,
        args.width,
        args.height,
        args.fullscreen,
        args.isolation,
        args.process_priority,
        args.lock_memory,
        args.launcher_visibility,
    )
    if not launch_target and (any(value is not None for value in options) or args.jvm_arg or args.game_arg):
        parser.error("游戏启动设置需要与 --launch 同时使用")
    for name, lower, upper in (("memory", 512, 65536), ("width", 320, 16384), ("height", 240, 16384)):
        value = getattr(args, name)
        if value is not None and not lower <= value <= upper:
            parser.error(f"--{name} 必须在 {lower}—{upper} 之间")
    if args.game_dir:
        root = Path(args.game_dir).expanduser().resolve()
        if not root.is_dir():
            parser.error("--game-dir 必须为已存在的游戏根目录")
        if launch_target and ("/" in launch_target or "\\" in launch_target):
            parser.error("--game-dir 只能与 --launch 版本ID联用，不能与完整实例目录联用")
    if any("\0" in arg for arg in (*args.jvm_arg, *args.game_arg)):
        parser.error("启动参数不能包含空字符")


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
    if (args.server or args.world) and not args.launch:
        parser.error("--server/--world 需要与 --launch 同时使用")
    if args.server and args.world:
        parser.error("--server 与 --world 不能同时使用")
    launch_target = _validate_quick_target(parser, "--launch", args.launch)
    _validate_game_options(parser, args, launch_target)
    return LaunchOptions(
        forwarded_argv=tuple(argv),
        data_dir=_resolve_data_dir(parser, args.data_dir),
        debug=bool(args.debug),
        log_level=args.log_level,
        disable_plugins=bool(args.disable_plugins),
        frontend_dist=_resolve_frontend_dist(parser, args.frontend_dist),
        enable_dev_channel=bool(args.dev_channel),
        launch_target=args.launch.strip() if args.launch else None,
        server_target=_validate_quick_target(parser, "--server", args.server),
        world_target=_validate_quick_target(parser, "--world", args.world),
        game_dir=Path(args.game_dir).expanduser().resolve() if args.game_dir else None,
        java_path=Path(args.java_path).expanduser().resolve() if args.java_path else None,
        memory=args.memory,
        width=args.width,
        height=args.height,
        fullscreen=args.fullscreen,
        isolation_mode=args.isolation,
        process_priority=args.process_priority,
        lock_memory=args.lock_memory,
        jvm_args=tuple(args.jvm_arg),
        game_args=tuple(args.game_arg),
        launcher_visibility=args.launcher_visibility,
        open_page=args.open_page,
    )


def run_launcher(argv: list[str] | None = None) -> int:
    """
    解析命令行参数并运行启动器。

    帮助、版本和参数错误在导入桌面后端前由解析器处理；其余参数只在本次运行中生效。

    :param argv: 不含程序名的参数列表；None 表示读取 ``sys.argv[1:]``
    :return: 启动器的退出码
    :raises SystemExit: 请求帮助、版本或参数不合法时抛出
    """
    options = parse_launch_options(sys.argv[1:] if argv is None else argv)
    from ECL.launcher import EuoraCraftLauncher

    return int(EuoraCraftLauncher(options).run())


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


__all__ = ["LaunchOptions", "apply_launch_overrides", "build_arg_parser", "parse_launch_options", "run_launcher"]
