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
#   - test_apply_overrides_none_returns_copy_without_changes() -> None
#   - test_apply_overrides_does_not_mutate_input_config() -> None
#   - test_apply_overrides_applies_all_sections() -> None
#   - test_apply_overrides_creates_missing_sections() -> None
# ============================================================

import pytest

from ECL.cli import LaunchOptions, apply_launch_overrides, parse_launch_options
from ECL.common.version import __version__


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
    ):
        assert argument in output


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
