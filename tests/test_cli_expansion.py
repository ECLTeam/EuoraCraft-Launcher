from __future__ import annotations

from types import SimpleNamespace

import pytest

from ECL.cli import parse_launch_options
from ECL.foundation import EventBus
from ECL.host.single_instance import SingleInstanceService, probe_running_instance
from ECL.host.tauri import Adapter


def test_desktop_parameters_keep_request_and_process_settings_separate() -> None:
    options = parse_launch_options(
        ["--launch", "Demo", "--account", "offline:player", "--language", "en-US", "--start-minimized"]
    )
    assert options.game_overrides() == {"account_id": "offline:player"}
    assert options.language == "en-US"
    assert options.start_minimized


def test_targeted_plugin_disable_is_deduplicated_and_validated() -> None:
    assert parse_launch_options(["--disable-plugin", "example", "--disable-plugin", "example"]).disabled_plugin_ids == (
        "example",
    )
    with pytest.raises(SystemExit):
        parse_launch_options(["--disable-plugin", "../outside"])


@pytest.mark.parametrize("page", ["accounts", "java", "plugins"])
def test_management_destinations_are_explicit(page: str) -> None:
    assert parse_launch_options(["--open-page", page]).open_page == page


@pytest.mark.parametrize(
    "arguments",
    [
        ["--account", "player"],
        ["--launch", "Demo", "--account", " "],
        ["--language", "unknown"],
        ["--lang", "en-US"],
        ["--headless", "--task", "runtime-check", "--start-minimized"],
        ["--headless", "--task", "runtime-check", "--language", "en-US"],
    ],
)
def test_invalid_combinations_fail_before_initialization(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        parse_launch_options(arguments)
    assert error.value.code == 2


def test_forwarding_keeps_different_accounts_as_different_requests(tmp_path) -> None:
    service = SingleInstanceService(EventBus(), tmp_path, "test")
    service.start()
    try:
        for account in ("first", "second", "first"):
            assert probe_running_instance(tmp_path, ["--launch", "Demo", "--account", account])
        requests = service.take_launch_requests()
        assert [request.account_id for request in requests] == ["first", "second"]
        for arguments in (["--language", "en-US"], ["--start-minimized"]):
            with pytest.raises(ValueError, match="拒绝"):
                probe_running_instance(tmp_path, arguments)
        assert not service.take_launch_requests()
    finally:
        service.close()


def test_initial_minimize_does_not_request_focus(monkeypatch) -> None:
    actions: list[str] = []
    window = SimpleNamespace(minimize=lambda: actions.append("minimize"), set_focus=lambda: actions.append("focus"))
    monkeypatch.setattr("ECL.host.tauri.Manager.get_webview_window", lambda *_: window)
    adapter = object.__new__(Adapter)
    adapter._start_minimized = True
    adapter._startup_focus_attempted = False
    monkeypatch.setattr("ECL.host.tauri.RunEvent", SimpleNamespace(Ready=type("Ready", (), {})))
    from ECL.host.tauri import RunEvent

    adapter._focus_main_window_on_ready(None, RunEvent.Ready())
    adapter._focus_main_window_on_ready(None, RunEvent.Ready())
    assert actions == ["minimize"]
