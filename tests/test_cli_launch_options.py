# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 cli 模块的自动化测试：启动参数解析、校验与配置覆盖叠加。
#
# 公开接口：
#   - test_parse_defaults_returns_all_unset_options() -> None
#   - test_parse_flags_and_values() -> None
#   - test_parse_data_dir_resolves_relative_path(tmp_path, monkeypatch) -> None
#   - test_parse_data_dir_rejects_existing_file(tmp_path) -> None
#   - test_parse_frontend_dist_keeps_url() -> None
#   - test_parse_frontend_dist_resolves_existing_directory(tmp_path) -> None
#   - test_parse_frontend_dist_rejects_missing_directory(tmp_path) -> None
#   - test_parse_rejects_unknown_log_level() -> None
#   - test_version_flag_prints_version_and_exits(capsys) -> None
#   - test_help_flag_prints_usage_and_exits(capsys) -> None
#   - test_main_forwards_launcher_entry() -> None
#   - test_run_launcher_passes_options_and_exit_code(monkeypatch) -> None
#   - test_run_launcher_exits_before_desktop_import(argv, exit_code, monkeypatch, tmp_path) -> None
#   - test_parse_quick_launch_targets() -> None
#   - test_parse_world_target_allows_spaces() -> None
#   - test_parse_server_requires_launch() -> None
#   - test_parse_world_requires_launch() -> None
#   - test_parse_server_and_world_mutually_exclusive() -> None
#   - test_parse_server_rejects_whitespace() -> None
#   - test_apply_overrides_none_returns_copy_without_changes() -> None
#   - test_apply_overrides_does_not_mutate_input_config() -> None
#   - test_apply_overrides_applies_all_sections() -> None
#   - test_apply_overrides_creates_missing_sections() -> None
# ============================================================

import builtins
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import main
from ECL.cli import LaunchOptions, apply_launch_overrides, parse_launch_options, run_launcher
from ECL.foundation.version import __version__
from ECL.runtime.contracts import HeadlessTask, RunMode


def test_parse_defaults_returns_all_unset_options() -> None:
    """不带参数时应得到全部字段未设置的默认选项。"""
    assert parse_launch_options([]) == LaunchOptions()


def test_parse_flags_and_values() -> None:
    """布尔开关与取值参数应按定义解析。"""
    options = parse_launch_options(["--debug", "--log-level", "warning", "--disable-plugins", "--dev-channel"])

    assert options.debug is True
    assert options.log_level == "warning"
    assert options.disable_plugins is True
    assert options.enable_dev_channel is True


def test_parse_data_dir_resolves_relative_path(tmp_path, monkeypatch) -> None:
    """相对路径与 ~ 展开后的 --data-dir 应解析为绝对路径。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    options = parse_launch_options(["--data-dir", "sandbox"])

    assert options.data_dir == (tmp_path / "sandbox").resolve()


def test_parse_data_dir_rejects_existing_file(tmp_path) -> None:
    """--data-dir 指向已存在的文件时应以用法错误退出。"""
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("content", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--data-dir", str(file_path)])

    assert exc_info.value.code == 2


def test_parse_frontend_dist_keeps_url() -> None:
    """http(s):// 形态的 --frontend-dist 应原样保留。"""
    options = parse_launch_options(["--frontend-dist", "http://localhost:5173"])

    assert options.frontend_dist == "http://localhost:5173"


def test_parse_frontend_dist_resolves_existing_directory(tmp_path) -> None:
    """路径形态的 --frontend-dist 应解析为已存在目录的绝对路径字符串。"""
    dist_path = tmp_path / "dist"
    dist_path.mkdir()

    options = parse_launch_options(["--frontend-dist", str(dist_path)])

    assert options.frontend_dist == str(dist_path.resolve())


def test_parse_frontend_dist_rejects_missing_directory(tmp_path) -> None:
    """路径形态的 --frontend-dist 指向不存在的目录时应以用法错误退出。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--frontend-dist", str(tmp_path / "missing")])

    assert exc_info.value.code == 2


def test_parse_rejects_unknown_log_level() -> None:
    """未知日志级别应被 argparse 的 choices 拒绝。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--log-level", "verbose"])

    assert exc_info.value.code == 2


def test_version_flag_prints_version_and_exits(capsys) -> None:
    """--version 应输出版本信息并以退出码 0 结束。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--version"])

    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_flag_prints_usage_and_exits(capsys) -> None:
    """--help 应输出包含全部参数的中文帮助并以退出码 0 结束。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for argument in (
        "--version",
        "--data-dir",
        "--debug",
        "--log-level",
        "--disable-plugins",
        "--frontend-dist",
        "--dev-channel",
        "--launch",
        "--server",
        "--world",
    ):
        assert argument in output


def test_main_forwards_launcher_entry(monkeypatch) -> None:
    """
    普通启动参数应在分流后交给命令行模块，并保留退出码。
    """
    received: list[list[str]] = []

    def desktop_entry(arguments: list[str]) -> int:
        received.append(arguments)
        return 7

    monkeypatch.setattr("ECL.cli.run_launcher", desktop_entry)
    assert main.run_launcher(["--version"]) == 7
    assert received == [["--version"]]


def test_run_launcher_passes_options_and_exit_code(monkeypatch) -> None:
    """
    命令行模块应把解析结果交给启动器，并返回其退出码。
    """
    received: list[LaunchOptions] = []

    class FakeLauncher:
        def __init__(self, options: LaunchOptions) -> None:
            received.append(options)

        def run(self) -> int:
            return 3

    launcher_module = ModuleType("ECL.launcher")
    launcher_module.EuoraCraftLauncher = FakeLauncher
    monkeypatch.setitem(sys.modules, "ECL.launcher", launcher_module)

    assert run_launcher(["--debug"]) == 3
    monkeypatch.setattr(sys, "argv", ["main.py", "--log-level=warning"])
    assert run_launcher() == 3
    assert received == [
        LaunchOptions(forwarded_argv=("--debug",), debug=True),
        LaunchOptions(forwarded_argv=("--log-level=warning",), log_level="warning"),
    ]


@pytest.mark.parametrize(("argv", "exit_code"), [(["--help"], 0), (["--version"], 0), (["--unknown"], 2)])
def test_run_launcher_exits_before_desktop_import(argv, exit_code, monkeypatch, tmp_path) -> None:
    """
    帮助、版本和错误参数应在导入桌面后端前结束。
    """
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "ECL.launcher":
            raise AssertionError("不应导入桌面后端")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    data_path = tmp_path / "unused-data"
    with pytest.raises(SystemExit) as exc_info:
        run_launcher(["--data-dir", str(data_path), *argv])

    assert exc_info.value.code == exit_code
    assert not data_path.exists()


def test_parse_quick_launch_targets() -> None:
    """--launch 与 --server 应成对解析并保留原始取值。"""
    options = parse_launch_options(["--launch", "Foo", "--server", "127.0.0.1:25565"])

    assert options.launch_target == "Foo"
    assert options.server_target == "127.0.0.1:25565"
    assert options.world_target is None


def test_parse_world_target_allows_spaces() -> None:
    """--world 的世界 ID 允许包含空格（对应存档目录名）。"""
    options = parse_launch_options(["--launch", "Foo", "--world", "New World"])

    assert options.world_target == "New World"


def test_parse_server_requires_launch() -> None:
    """--server 缺少 --launch 时应以用法错误退出。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--server", "127.0.0.1:25565"])

    assert exc_info.value.code == 2


def test_parse_world_requires_launch() -> None:
    """--world 缺少 --launch 时应以用法错误退出。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--world", "New World"])

    assert exc_info.value.code == 2


def test_parse_server_and_world_mutually_exclusive() -> None:
    """--server 与 --world 不能同时使用。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--launch", "Foo", "--server", "a:1", "--world", "w"])

    assert exc_info.value.code == 2


def test_parse_server_rejects_whitespace() -> None:
    """--server 的地址不允许包含空白字符。"""
    with pytest.raises(SystemExit) as exc_info:
        parse_launch_options(["--launch", "Foo", "--server", "bad address"])

    assert exc_info.value.code == 2


def test_apply_overrides_none_returns_copy_without_changes() -> None:
    """options 为 None 时应返回内容相同且独立的配置副本。"""
    config = {"launcher": {"debug": False}}

    result = apply_launch_overrides(config, None)

    assert result == config
    assert result is not config


def test_apply_overrides_does_not_mutate_input_config() -> None:
    """叠加覆盖不得修改传入的配置对象。"""
    config = {"launcher": {"debug": False, "debug_log_level": "info", "dev_channel": False}}
    options = LaunchOptions(
        debug=True, log_level="debug", enable_dev_channel=True, frontend_dist="http://localhost:5173"
    )

    apply_launch_overrides(config, options)

    assert config == {"launcher": {"debug": False, "debug_log_level": "info", "dev_channel": False}}


def test_apply_overrides_applies_all_sections() -> None:
    """全部启动参数应覆盖 launcher 分区并注入 tauri 分区，其余分区不受影响。"""
    config = {
        "launcher": {"debug": False, "debug_log_level": "info", "dev_channel": False},
        "ui": {"locale": "zh-CN"},
    }
    options = LaunchOptions(
        debug=True, log_level="warning", enable_dev_channel=True, frontend_dist="http://localhost:5173"
    )

    result = apply_launch_overrides(config, options)

    assert result["launcher"] == {"debug": True, "debug_log_level": "warning", "dev_channel": True}
    assert result["tauri"] == {"frontenddist": "http://localhost:5173"}
    assert result["ui"] == {"locale": "zh-CN"}


def test_apply_overrides_creates_missing_sections() -> None:
    """缺失的 launcher/tauri 分区应在叠加覆盖时自动创建。"""
    assert apply_launch_overrides({}, LaunchOptions(debug=True)) == {"launcher": {"debug": True}}
    assert apply_launch_overrides({}, LaunchOptions(frontend_dist="http://localhost:5173")) == {
        "tauri": {"frontenddist": "http://localhost:5173"}
    }


def test_temporary_game_overrides_keep_explicit_false_and_dont_copy_defaults() -> None:
    options = parse_launch_options(
        ["--launch=Foo", "--memory=6144", "--windowed", "--no-lock-memory", "--jvm-arg=-Dfoo=bar", "--game-arg=--demo"]
    )
    assert options.game_overrides() == {
        "memory": 6144,
        "fullscreen": False,
        "lock_memory": False,
        "jvm_args": ["-Dfoo=bar"],
        "game_args": ["--demo"],
    }
    assert parse_launch_options(["--launch=Foo"]).game_overrides() == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["--memory=4096"],
        ["--launch=Foo", "--memory=100"],
        ["--launch=Foo", "--width=10"],
        ["--launch=Foo", "--open-page=games"],
        ["--launch=Foo", "--fullscreen", "--windowed"],
    ],
)
def test_invalid_extended_game_options_are_rejected(argv) -> None:
    with pytest.raises(SystemExit):
        parse_launch_options(argv)


def test_explicit_gui_and_headless_options(tmp_path) -> None:
    assert parse_launch_options(["--gui"]).run_mode == RunMode.desktop
    result_file = tmp_path / "nested" / "result.json"
    options = parse_launch_options(["--headless", "--task=runtime-check", "--result-file", str(result_file)])
    assert options.run_mode == RunMode.headless and options.task == HeadlessTask.runtime_check
    assert options.result_file == result_file
    assert not result_file.parent.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--headless"],
        ["--headless", "--task=unknown"],
        ["--gui", "--headless"],
        ["--task=runtime-check"],
        ["--result-file=result.json"],
    ],
)
def test_invalid_mode_exits_before_runtime_import(argv, monkeypatch) -> None:
    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name in {"ECL.launcher", "ECL.runtime.headless"}:
            pytest.fail("参数错误不应加载运行入口")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    with pytest.raises(SystemExit) as error:
        run_launcher(argv)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "desktop_option",
    [
        "--frontend-dist=missing",
        "--open-page=games",
        "--dev-channel",
        "--disable-plugins",
        "--launch=Foo",
        "--server=server",
        "--world=world",
        "--game-dir=missing",
        "--java-path=missing",
        "--memory=4096",
        "--width=1000",
        "--height=600",
        "--fullscreen",
        "--windowed",
        "--isolation=inherit",
        "--process-priority=normal",
        "--lock-memory",
        "--no-lock-memory",
        "--jvm-arg=-Xmx1G",
        "--game-arg=--demo",
        "--launcher-visibility=none",
        "--front=missing",
        "--no-lock-mem",
        "--webview-debug-port=0",
    ],
)
def test_headless_rejects_every_desktop_option(desktop_option) -> None:
    with pytest.raises(SystemExit) as error:
        parse_launch_options(["--headless", "--task=runtime-check", desktop_option])
    assert error.value.code == 2


@pytest.mark.parametrize("target", ["existing", "directory", "blocked/child", "", "bad\0file"])
def test_result_path_rejects_existing_or_invalid_targets(tmp_path, monkeypatch, target) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing").write_text("keep", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    (tmp_path / "blocked").write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        parse_launch_options(["--headless", "--task=runtime-check", "--result-file", target])
    assert error.value.code == 2
    assert Path("existing").read_text(encoding="utf-8") == "keep"


def test_parse_webview_debug_port_accepts_auto_and_explicit_port() -> None:
    """调试端口 0 表示由浏览器分配，其余合法端口原样保留。"""
    assert parse_launch_options(["--dev-channel", "--webview-debug-port", "0"]).webview_debug_port == 0
    assert parse_launch_options(["--dev-channel", "--webview-debug-port=1024"]).webview_debug_port == 1024


@pytest.mark.parametrize("port", ["1", "1023", "65536", "-1"])
def test_parse_webview_debug_port_rejects_out_of_range(port: str) -> None:
    """调试端口只接受 0 或非特权端口范围。"""
    with pytest.raises(SystemExit) as error:
        parse_launch_options(["--dev-channel", f"--webview-debug-port={port}"])

    assert error.value.code == 2


def test_parse_webview_debug_port_requires_dev_channel() -> None:
    """远程调试会暴露完整的页面脚本能力，必须与开发者通道同时开启。"""
    with pytest.raises(SystemExit) as error:
        parse_launch_options(["--webview-debug-port=0"])

    assert error.value.code == 2


def test_parse_webview_debug_port_rejects_other_platforms(monkeypatch) -> None:
    """非 Windows 平台明确拒绝，不用空实现掩盖。"""
    monkeypatch.setattr("ECL.cli.sys", SimpleNamespace(platform="linux"))

    with pytest.raises(SystemExit) as error:
        parse_launch_options(["--dev-channel", "--webview-debug-port=0"])

    assert error.value.code == 2
