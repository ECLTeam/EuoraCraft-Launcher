# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证第三方启动钩子的准入拒绝、参数修改边界与必要钩子的阻断语义。
#
# 公开接口：
#   - test_remote_modifications_apply_within_limits() -> None
#   - test_remote_modifications_reject_out_of_bounds(payload) -> None
#   - test_required_hook_blocks_prepare_but_not_exit() -> None
#   - test_optional_hook_failure_is_isolated() -> None
#   - test_hook_registration_requires_instance_management(request) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.launch_hooks import LaunchContext, LaunchHookRegistry, apply_remote_modifications
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture


def context() -> LaunchContext:
    return LaunchContext(
        version_id="1.21.1",
        loader="fabric",
        game_path=Path("/game"),
        game_directory=Path("/game/versions/1.21.1"),
        version_isolation=True,
    )


def test_remote_modifications_apply_within_limits() -> None:
    target = context()
    apply_remote_modifications(
        target,
        {"jvm_args": ["-Ddemo=1"], "game_args": ["--demo"], "env": {"ECL_DEMO": "1"}},
    )
    assert target.jvm_args == ["-Ddemo=1"]
    assert target.game_args == ["--demo"]
    assert target.env == {"ECL_DEMO": "1"}


@pytest.mark.parametrize(
    "payload",
    [
        {"jvm_args": ["x"] * 33},
        {"game_args": ["x" * 513]},
        {"env": {"bad-key": "1"}},
        {"env": {"OK": "x" * 513}},
        {"env": {"K": 1}},
    ],
)
def test_remote_modifications_reject_out_of_bounds(payload: dict) -> None:
    target = context()
    with pytest.raises(ValueError):
        apply_remote_modifications(target, payload)
    assert target.jvm_args == []
    assert target.env == {}


def test_required_hook_blocks_prepare_but_not_exit() -> None:
    registry = LaunchHookRegistry()

    def fail(_context: LaunchContext) -> None:
        raise RuntimeError("前置失败")

    registry.register(owner="demo", name="hook", on_prepare=fail, required=True)
    with pytest.raises(RuntimeError):
        registry.prepare(context())

    registry.register(owner="demo", name="hook", on_exit=fail, required=True)
    registry.on_exit(context())


def test_optional_hook_failure_is_isolated() -> None:
    registry = LaunchHookRegistry()

    def fail(_context: LaunchContext) -> None:
        raise RuntimeError("失败")

    registry.register(owner="demo", name="hook", on_prepare=fail)
    registry.prepare(context())


def test_hook_registration_requires_instance_management(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        broker.handle(
            RpcRequest(
                "hooks.register",
                {"phase": "prepare", "callback_ref": "a" * 32},
                monotonic() + 5,
                uuid4().hex,
            )
        )
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"
