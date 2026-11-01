# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在宿主导入前识别进程角色，按需加载桌面、无头任务或插件执行器。
#
# 公开接口：
#   - class ProcessRole — 进程入口选择，不表示业务授权。
#   - class PluginProcessOptions — 经过结构校验的插件连接参数。
#   - class ProcessRoute — 不可变的入口分流结果。
#   - select_process_route — 统一校验模式和插件角色参数。
#   - local_worker_endpoint — 根据平台和运行标识生成唯一的本地端点。
#   - run_process — 按已确定角色延迟导入执行器。
# ============================================================

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ProcessRole(StrEnum):
    """
    区分运行生命周期，不把角色或命令行参数视为授权凭据。
    """

    desktop = "desktop"
    task = "task"
    plugin = "plugin"
    guardian = "plugin-guardian"


@dataclass(frozen=True, slots=True)
class PluginProcessOptions:
    """
    保存插件连接定位信息，真实宿主身份仍须由管道和进程句柄核验。
    """

    endpoint: str
    host_pid: int
    run_id: str


@dataclass(frozen=True, slots=True)
class ProcessRoute:
    """
    保存轻量分流结果，普通任务参数继续交由业务 CLI 校验。
    """

    role: ProcessRole
    arguments: tuple[str, ...]
    plugin: PluginProcessOptions | None = None


def local_worker_endpoint(run_id: str) -> str:
    """
    使用固定本地命名规则，不接受网络地址或调用方指定的临时目录。

    :param run_id: 长度为 32 的小写十六进制运行标识
    :return: 当前平台的本地端点
    :raises ValueError: 运行标识或平台不受支持
    """
    if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("插件运行标识无效")
    if sys.platform == "win32":
        return f"\\\\.\\pipe\\ecl-worker-{run_id}"
    if sys.platform in {"linux", "darwin"}:
        return f"/tmp/ecl-worker-{run_id}/rpc"
    raise ValueError("当前平台不支持插件端点")


def select_process_route(argv: Sequence[str]) -> ProcessRoute:
    """
    在任何宿主导入前确定运行角色，非法插件参数不回退其他入口。

    仅解析模式与角色控制参数；桌面和有限任务的业务选项保留给原 CLI。
    插件角色必须完整匹配自己的参数集合，不能携带用户脚本或任务输出参数。

    :param argv: 不包含执行文件名的原始参数序列
    :return: 只包含不可变参数的分流结果
    :raises SystemExit: 角色、模式、连接参数或混用选项无效时以 2 退出
    """
    arguments = tuple(argv)
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--headless", action="store_true")
    modes.add_argument("--gui", action="store_true")
    parser.add_argument("--role", choices=("plugin", "plugin-guardian"))
    parser.add_argument("--endpoint")
    parser.add_argument("--host-pid", type=int)
    parser.add_argument("--run-id")
    control_flags = frozenset({"--headless", "--gui", "--role", "--endpoint", "--host-pid", "--run-id"})
    for argument in arguments:
        if argument == "--":
            break
        flag = argument.split("=", 1)[0]
        if flag == "--internal-plugin-worker":
            parser.error("旧插件入口已移除，请使用 --headless --role plugin")
        if (
            flag.startswith("--")
            and flag not in control_flags
            and any(option.startswith(flag) for option in control_flags)
        ):
            parser.error("模式与角色控制参数必须使用完整名称")
    options, remaining = parser.parse_known_args(arguments)
    if options.role not in {"plugin", "plugin-guardian"}:
        if any(value is not None for value in (options.endpoint, options.host_pid, options.run_id)):
            parser.error("插件连接参数需要与 --headless --role plugin 一起使用")
        return ProcessRoute(ProcessRole.task if options.headless else ProcessRole.desktop, arguments)
    if not options.headless or remaining:
        parser.error("插件角色仅支持 --headless、--role 和插件连接参数")
    for flag in control_flags:
        if sum(argument.split("=", 1)[0] == flag for argument in arguments) > 1:
            parser.error("插件角色参数不能重复")
    if (
        options.endpoint is None
        or options.host_pid is None
        or options.run_id is None
        or len(options.run_id) != 32
        or any(character not in "0123456789abcdef" for character in options.run_id)
        or options.endpoint != local_worker_endpoint(options.run_id)
        or not 0 < options.host_pid <= 0xFFFFFFFF
    ):
        parser.error("插件连接参数缺失或无效")
    return ProcessRoute(
        ProcessRole.guardian if options.role == "plugin-guardian" else ProcessRole.plugin,
        arguments,
        PluginProcessOptions(options.endpoint, options.host_pid, options.run_id),
    )


def run_process(argv: Sequence[str]) -> int:
    """
    只导入所选角色的执行入口，插件不会经过 ECL 业务 CLI 或桌面初始化。

    :param argv: 不包含执行文件名的参数序列
    :return: 所选执行器的真实退出码
    :raises SystemExit: 参数错误、帮助或版本请求导致解析器退出
    """
    route = select_process_route(argv)
    if route.role is ProcessRole.guardian:
        from ECL.plugin_api._guardian import run_guardian

        assert route.plugin is not None
        return run_guardian(route.plugin)
    if route.role is ProcessRole.plugin:
        from ECL.plugin_api._process import run_plugin_process

        assert route.plugin is not None
        return run_plugin_process(route.plugin)
    from ECL.cli import run_launcher

    return run_launcher(list(route.arguments))
