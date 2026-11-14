# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 public_returns 模块的自动化测试。
#
# 公开接口：
#   - test_config_save_uses_exceptions_instead_of_ambiguous_boolean(tmp_path) -> None
#   - test_plugin_action_result_exposes_action_status_and_success() -> None
#   - test_launcher_exit_codes_distinguish_failure_stages() -> None
# ============================================================

from __future__ import annotations

import pytest

from ECL.launcher import LauncherExitCode
from ECL.plugins.framework import PluginAction, PluginActionResult
from ECL.utils.config import ConfigStore, ConfigValidationError


def test_config_save_uses_exceptions_instead_of_ambiguous_boolean(tmp_path) -> None:
    manager = ConfigStore(tmp_path / "ECL_data")

    assert manager.save_config("launcher", {"debug": True}) is None
    assert manager.get_config("launcher") == {"debug": True}

    with pytest.raises(ConfigValidationError):
        manager.save_config("", {})


def test_plugin_action_result_exposes_action_status_and_success() -> None:
    result = PluginActionResult("example", PluginAction.ENABLE, "enabled")

    assert result.plugin_name == "example"
    assert result.action is PluginAction.ENABLE
    assert result.status == "enabled"
    assert result.success is True


def test_launcher_exit_codes_distinguish_failure_stages() -> None:
    assert int(LauncherExitCode.SUCCESS) == 0
    assert LauncherExitCode.STARTUP_FAILED != LauncherExitCode.FRONTEND_FAILED
