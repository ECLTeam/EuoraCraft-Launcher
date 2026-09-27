# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对启动高级选项（wrapper/后退出命令/环境变量/窗口标题/可见性）的自动化测试。
#
# 公开接口：
#   - test_parse_env_vars_accepts_valid_lines() -> None
#   - test_parse_env_vars_skips_invalid_lines_with_warnings() -> None
#   - test_parse_env_vars_last_duplicate_key_wins() -> None
#   - test_apply_wrapper_replaces_placeholder() -> None
#   - test_apply_wrapper_prefixes_without_placeholder() -> None
#   - test_apply_wrapper_empty_returns_command() -> None
#   - test_render_window_title_expands_placeholders() -> None
#   - test_render_window_title_keeps_unknown_placeholders() -> None
#   - test_render_window_title_empty_template() -> None
#   - test_default_config_contains_advanced_launch_options() -> None
#   - test_launch_request_visibility_rejects_unknown_action() -> None
# ============================================================

import pytest
from pydantic import ValidationError

from ECL.api.models import LaunchRequest
from ECL.services.game.launch import _apply_wrapper, _parse_env_vars, _render_window_title
from ECL.utils.config import default_config


def test_parse_env_vars_accepts_valid_lines() -> None:
    """合法的 KEY=VALUE 行应解析为字典，保留值中的等号并去除首尾空白。"""
    variables, warnings = _parse_env_vars(" FOO=bar \nBaz=qux=1\n\n  EMPTY=  ")
    assert variables == {"FOO": "bar", "Baz": "qux=1", "EMPTY": ""}
    assert warnings == []


def test_parse_env_vars_skips_invalid_lines_with_warnings() -> None:
    """缺少等号或键为空的行应跳过并记录警告，不得中断解析。"""
    variables, warnings = _parse_env_vars("NO_SEPARATOR\n=without_key\nOK=1")
    assert variables == {"OK": "1"}
    assert len(warnings) == 2


def test_parse_env_vars_last_duplicate_key_wins() -> None:
    """重复键以最后一次声明为准。"""
    variables, _ = _parse_env_vars("A=1\nA=2")
    assert variables == {"A": "2"}


def test_apply_wrapper_replaces_placeholder() -> None:
    """含 {} 占位符的包装命令应把占位符替换为完整命令。"""
    assert _apply_wrapper("optirun {}", "java -jar game") == "optirun java -jar game"
    assert _apply_wrapper("wrap {} mid {}", "java") == "wrap java mid java"


def test_apply_wrapper_prefixes_without_placeholder() -> None:
    """不含占位符的包装命令应作为前缀拼接。"""
    assert _apply_wrapper("wine", "java -jar game") == "wine java -jar game"


def test_apply_wrapper_empty_returns_command() -> None:
    """包装命令为空时原样返回命令。"""
    assert _apply_wrapper("", "java") == "java"
    assert _apply_wrapper("   ", "java") == "java"


def test_render_window_title_expands_placeholders() -> None:
    """三种占位符应替换为对应值。"""
    title = _render_window_title(
        "{instance} - {version} ({account})",
        instance="My Inst",
        version="1.21.1",
        account="Steve",
    )
    assert title == "My Inst - 1.21.1 (Steve)"


def test_render_window_title_keeps_unknown_placeholders() -> None:
    """未知占位符应保留原文，用户文本中的花括号不触发意外替换。"""
    title = _render_window_title(
        "{instance} {unknown} {",
        instance="A",
        version="B",
        account="C",
    )
    assert title == "A {unknown} {"


def test_render_window_title_empty_template() -> None:
    """空模板应返回空字符串。"""
    assert _render_window_title("", instance="A", version="B", account="C") == ""
    assert _render_window_title("   ", instance="A", version="B", account="C") == ""


def test_default_config_contains_advanced_launch_options() -> None:
    """全局配置应包含五项启动高级选项的稳定默认值。"""
    game = default_config["game"]
    assert game["wrapper_command"] == ""
    assert game["post_exit_command"] == ""
    assert game["env_vars"] == ""
    assert game["window_title_template"] == ""
    assert game["launcher_visibility"] == "none"


def test_launch_request_visibility_rejects_unknown_action() -> None:
    """未知可见性行为应被请求模型拒绝；新字段缺省值保持零行为变化。"""
    payload = {
        "version_id": "1.21.1",
        "game_path": "C:/minecraft",
        "launcher_visibility": "hide_forever",
    }
    with pytest.raises(ValidationError):
        LaunchRequest.model_validate(payload)

    request = LaunchRequest.model_validate({"version_id": "1.21.1", "game_path": "C:/minecraft"})
    assert request.wrapper_command == ""
    assert request.post_exit_command == ""
    assert request.env_vars == ""
    assert request.window_title == ""
    assert request.launcher_visibility == "none"
