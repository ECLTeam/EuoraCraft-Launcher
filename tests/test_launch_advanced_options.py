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
#   - test_split_command_lines_drops_blanks_and_strips() -> None
#   - test_split_command_lines_keeps_shell_connectors_in_one_line() -> None
#   - test_render_window_title_expands_placeholders() -> None
#   - test_render_window_title_keeps_unknown_placeholders() -> None
#   - test_render_window_title_empty_template() -> None
#   - test_default_config_contains_advanced_launch_options() -> None
#   - test_launch_request_visibility_rejects_unknown_action() -> None
#   - test_rewrite_window_title_gives_up_when_process_dead(monkeypatch) -> None
#   - test_rewrite_window_title_invokes_platform_renamer(monkeypatch) -> None
#   - test_rename_window_title_win32_returns_false_without_match() -> None
#   - test_launch_applies_wrapper_env_title_and_visibility(tmp_path, monkeypatch) -> None
#   - test_launch_visibility_none_emits_nothing(tmp_path, monkeypatch) -> None
#   - test_launch_triggers_post_exit_command(tmp_path, monkeypatch) -> None
#   - test_run_pre_launch_command_executes_each_line_in_order(tmp_path, monkeypatch) -> None
#   - test_run_pre_launch_command_stops_on_intermediate_failure(tmp_path, monkeypatch) -> None
#   - test_run_pre_launch_command_shares_total_timeout(tmp_path, monkeypatch) -> None
#   - test_run_post_exit_command_continues_after_failure(tmp_path, monkeypatch) -> None
#   - test_run_post_exit_command_stops_remaining_on_timeout(tmp_path, monkeypatch) -> None
# ============================================================

import asyncio
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ECL.api.models import LaunchRequest
from ECL.foundation import EventBus
from ECL.services.game import GameService, GameServiceError
from ECL.services.game import launch as launch_module
from ECL.services.game.launch import _apply_wrapper, _parse_env_vars, _render_window_title, _split_command_lines
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


def test_split_command_lines_drops_blanks_and_strips() -> None:
    """多行命令应逐行拆分、去除首尾空白并忽略空行。"""
    assert _split_command_lines("  echo A \n\n\techo B\n   ") == ["echo A", "echo B"]
    assert _split_command_lines("") == []
    assert _split_command_lines("   \n  \n") == []


def test_split_command_lines_keeps_shell_connectors_in_one_line() -> None:
    """同一行内的 shell 连接符不应被拆分为多条命令。"""
    assert _split_command_lines("echo A && echo B\necho C") == ["echo A && echo B", "echo C"]


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


# ---------- 窗口标题改写 ----------


class _DeadProcess:
    pid = 5

    def poll(self):
        return 0


class _AliveProcess:
    pid = 7

    def poll(self):
        return None


def test_rewrite_window_title_gives_up_when_process_dead(monkeypatch) -> None:
    """游戏进程已退出时应立即放弃，不调用平台改写实现。"""
    called = []
    monkeypatch.setattr(launch_module, "_select_title_renamer", lambda: lambda pids, title: called.append(1))
    assert launch_module._rewrite_game_window_title(_DeadProcess(), "T", timeout_seconds=1) is False
    assert called == []


def test_rewrite_window_title_invokes_platform_renamer(monkeypatch) -> None:
    """平台改写实现应收到进程树 PID 集合与渲染后的标题。"""
    seen = []
    monkeypatch.setattr(
        launch_module, "_select_title_renamer", lambda: lambda pids, title: seen.append((pids, title)) or True
    )
    assert launch_module._rewrite_game_window_title(_AliveProcess(), "T") is True
    assert len(seen) == 1
    pids, title = seen[0]
    assert title == "T"
    assert 7 in pids


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 调用 user32")
def test_rename_window_title_win32_returns_false_without_match() -> None:
    """无匹配进程窗口时应返回 False 而不报错。"""
    assert launch_module._rename_window_title_win32({99999999}, "T") is False


# ---------- 启动链路集成 ----------


class _FakeAccounts:
    def current_account(self):
        return {"id": "offline", "type": "offline"}

    async def get_launch_credentials(self):
        return {
            "player_name": "Steve",
            "uuid": "0123456789abcdef0123456789abcdef",
            "user_type": "legacy",
            "access_token": "None",
        }


class _FakeProcess:
    def __init__(self):
        self.running = True
        self.pid = 24680

    def poll(self):
        return None if self.running else 0


class _FakeInstances:
    def __init__(self):
        self.options = None

    def create_instance(self, **options):
        self.options = options
        return "mc-1", _FakeProcess()

    def get_instances_info(self):
        return []


def _make_launch_service(tmp_path, monkeypatch, event_bus):
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.1"
    version_path.mkdir(parents=True)
    (version_path / "1.21.1.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    instances = _FakeInstances()
    service = GameService(
        _FakeAccounts(),
        search_factory=lambda _path: SimpleNamespace(search_minecraft=lambda: {}),
        instances_manager=instances,
        command_builder=lambda _config: "java -jar cmd",
        event_bus=event_bus,
        enable_version_watcher=False,
    )
    service.logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )
    return service, instances, game_path, java_path


def test_launch_applies_wrapper_env_title_and_visibility(tmp_path, monkeypatch) -> None:
    """启动应应用 wrapper 前缀、注入用户环境变量、发射可见性事件并启动标题改写。"""
    visibility_events = []
    event_bus = EventBus()
    event_bus.subscribe("launcher:visibility", visibility_events.append)
    service, instances, game_path, java_path = _make_launch_service(tmp_path, monkeypatch, event_bus)

    title_calls = []
    monkeypatch.setattr(
        launch_module,
        "_select_title_renamer",
        lambda: lambda pids, title: title_calls.append((tuple(sorted(pids)), title)) or True,
    )

    result = asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.1"},
            game_path=game_path,
            java_path=java_path,
            wrapper_command="wrap {}",
            env_vars="FOO=1",
            post_exit_command="echo bye",
            window_title="{instance}|{version}|{account}",
            launcher_visibility="quit",
        )
    )
    assert result["instanceId"] == "mc-1"

    args = instances.options["args"]
    assert args.startswith("wrap java -jar cmd")
    env = instances.options["env"]
    assert env["FOO"] == "1"
    # 系统环境变量必须保留，仅追加用户自定义项
    assert env.get("PATH") == os.environ.get("PATH")
    assert visibility_events == [{"action": "quit"}]

    deadline = time.time() + 5
    while not title_calls and time.time() < deadline:
        time.sleep(0.05)
    assert len(title_calls) == 1
    _, title = title_calls[0]
    assert title == "1.21.1|1.21.1|Steve"


def test_launch_visibility_none_emits_nothing(tmp_path, monkeypatch) -> None:
    """默认可见性不应发射事件，空 wrapper 不改变命令。"""
    visibility_events = []
    event_bus = EventBus()
    event_bus.subscribe("launcher:visibility", visibility_events.append)
    service, instances, game_path, java_path = _make_launch_service(tmp_path, monkeypatch, event_bus)

    asyncio.run(service.launch_instance({"version_id": "1.21.1"}, game_path=game_path, java_path=java_path))

    assert instances.options["args"] == "java -jar cmd"
    assert instances.options["env"] is None
    assert visibility_events == []


def test_launch_triggers_post_exit_command(tmp_path, monkeypatch) -> None:
    """游戏退出回调应触发后退出命令，并带上实例工作目录与退出码。"""
    recorded = []
    done = threading.Event()
    service, instances, game_path, java_path = _make_launch_service(tmp_path, monkeypatch, EventBus())

    def fake_post_exit(command, working_directory, code):
        recorded.append((command, str(working_directory), code))
        done.set()

    monkeypatch.setattr(service, "_run_post_exit_command", fake_post_exit)

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.1"},
            game_path=game_path,
            java_path=java_path,
            post_exit_command="echo bye",
        )
    )
    assert not recorded
    instances.options["exit_callback"](0, "1.21.1")
    assert done.wait(5)
    assert recorded == [("echo bye", str(game_path / "versions" / "1.21.1"), 0)]


def _patch_subprocess_run(monkeypatch, outcomes):
    """替换 launch 模块的 subprocess.run，记录调用参数并按序返回预设结果。"""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(
            stdout=outcome.get("stdout", ""),
            stderr=outcome.get("stderr", ""),
            returncode=outcome.get("returncode", 0),
        )

    monkeypatch.setattr(launch_module.subprocess, "run", fake_run)
    return calls


def test_run_pre_launch_command_executes_each_line_in_order(tmp_path, monkeypatch) -> None:
    """启动前命令应按行逐条执行，并沿用同一工作目录与 shell 语义。"""
    service, _instances, _game_path, _java_path = _make_launch_service(tmp_path, monkeypatch, EventBus())
    calls = _patch_subprocess_run(monkeypatch, [{"stdout": "A"}, {"stdout": "B"}])

    service._run_pre_launch_command("echo A\necho B", tmp_path)

    assert [call[0] for call in calls] == ["echo A", "echo B"]
    assert all(call[1]["cwd"] == tmp_path for call in calls)
    assert all(call[1]["shell"] is True for call in calls)


def test_run_pre_launch_command_stops_on_intermediate_failure(tmp_path, monkeypatch) -> None:
    """中间某条命令非零退出时应立即取消启动，后续命令不再执行。"""
    service, _instances, _game_path, _java_path = _make_launch_service(tmp_path, monkeypatch, EventBus())
    calls = _patch_subprocess_run(
        monkeypatch,
        [{"returncode": 0}, {"returncode": 5, "stderr": "boom"}, {"returncode": 0}],
    )

    with pytest.raises(GameServiceError, match="第 2 条执行失败，退出码: 5") as raised:
        service._run_pre_launch_command("echo A\necho B\necho C", tmp_path)

    assert raised.value.error_code == "PRE_LAUNCH_COMMAND_FAILED"
    assert [call[0] for call in calls] == ["echo A", "echo B"]


def test_run_pre_launch_command_shares_total_timeout(tmp_path, monkeypatch) -> None:
    """多条命令共享总超时预算：单条额度不超过上限，且随时间推移逐条递减。"""
    service, _instances, _game_path, _java_path = _make_launch_service(tmp_path, monkeypatch, EventBus())
    clock = {"now": 1000.0}
    monkeypatch.setattr(launch_module, "monotonic", lambda: clock["now"])
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        clock["now"] += 5
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(launch_module.subprocess, "run", fake_run)

    service._run_pre_launch_command("echo A\necho B", tmp_path)

    assert [call[1]["timeout"] for call in calls] == [60.0, 55.0]


def test_run_post_exit_command_continues_after_failure(tmp_path, monkeypatch) -> None:
    """后退出命令某条失败时应记录日志并继续执行后续命令。"""
    service, _instances, _game_path, _java_path = _make_launch_service(tmp_path, monkeypatch, EventBus())
    calls = _patch_subprocess_run(monkeypatch, [{"returncode": 1}, {"returncode": 0}])

    service._run_post_exit_command("echo A\necho B", tmp_path, 0)

    assert [call[0] for call in calls] == ["echo A", "echo B"]


def test_run_post_exit_command_stops_remaining_on_timeout(tmp_path, monkeypatch) -> None:
    """后退出命令某条超时时应放弃剩余命令且不抛出异常。"""
    service, _instances, _game_path, _java_path = _make_launch_service(tmp_path, monkeypatch, EventBus())
    calls = _patch_subprocess_run(
        monkeypatch,
        [subprocess.TimeoutExpired("echo A", 60), {"returncode": 0}],
    )

    service._run_post_exit_command("echo A\necho B", tmp_path, 0)

    assert [call[0] for call in calls] == ["echo A"]
