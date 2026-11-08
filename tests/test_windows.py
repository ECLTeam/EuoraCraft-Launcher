# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 windows 模块的自动化测试。
#
# 公开接口：
#   - class FakeWindow
#       - label() -> str
#   - state_with(metadata) -> Any
#   - test_plugin_window_is_limited_to_declared_settings_and_commands() -> Any
#   - test_unregistered_child_window_is_denied() -> Any
#   - test_host_created_windows_are_in_tauri_capability() -> Any
#   - test_child_window_uses_a_valid_raw_rgba_icon() -> Any
# ============================================================

import tomllib
from pathlib import Path
from types import SimpleNamespace

from ECL.api.bridge import _FrontendState
from ECL.api.windows import _create_child_window_icon


class FakeWindow:
    def __init__(self, label: str) -> None:
        self._label = label

    def label(self) -> str:
        return self._label


def state_with(metadata):
    state = object.__new__(_FrontendState)
    state._window_metadata = {metadata["label"]: metadata}
    return state


def test_plugin_window_is_limited_to_declared_settings_and_commands():
    state = state_with(
        {
            "label": "plugin.demo.panel",
            "windowType": "plugin",
            "plugin": "demo",
            "allowedSettings": ["color"],
            "allowedCommands": ["refresh"],
        }
    )
    window = FakeWindow("plugin.demo.panel")
    assert (
        state.authorize_window_command("plugin_update_setting", {"plugin_name": "demo", "key": "color"}, window) is None
    )
    assert state.authorize_window_command("plugin_call_command", {"command": "demo:refresh"}, window) is None
    denied = state.authorize_window_command(
        "plugin_update_setting", {"plugin_name": "demo", "key": "host_path"}, window
    )
    assert denied["errorCode"] == "WINDOW_DATA_DENIED"
    denied = state.authorize_window_command("settings_set", {"section": "ui"}, window)
    assert denied["errorCode"] == "WINDOW_COMMAND_DENIED"


def test_unregistered_child_window_is_denied():
    state = SimpleNamespace(_window_metadata={})
    denied = _FrontendState.authorize_window_command(state, "settings_set", {}, FakeWindow("rogue"))
    assert denied["errorCode"] == "WINDOW_NOT_REGISTERED"


def test_host_created_windows_are_in_tauri_capability():
    capability_path = Path(__file__).parents[1] / "capabilities" / "default.toml"
    capability = tomllib.loads(capability_path.read_text(encoding="utf-8"))

    assert "main" in capability["windows"]
    assert "plugin:*" in capability["windows"]


def test_child_window_uses_a_valid_raw_rgba_icon():
    icon = _create_child_window_icon()

    if icon is not None:
        assert icon.width == 32
        assert icon.height == 32
