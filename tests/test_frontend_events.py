# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 frontend_events 模块的自动化测试。
#
# 公开接口：
#   - test_bridge_renames_accounts_changed() -> None
#   - test_bridge_reshapes_config_updated() -> None
#   - test_bridge_maps_plugin_status_events_to_single_event() -> None
#   - test_bridge_puts_multiple_positional_args_into_frontend_object() -> None
#   - test_bridge_passthrough_events_keep_name_and_payload() -> None
#   - test_bridge_omits_side_effect_events() -> None
#   - test_bridge_subscribe_returns_working_unsubscribers() -> None
#   - test_bridge_ignores_unregistered_frontend_event() -> None
# ============================================================

"""验证前端事件桥：后端事件总线到前端事件名与载荷的纯转换规则。"""

from types import SimpleNamespace
from typing import Any

from ECL.foundation import EventBus
from ECL.services.frontend_events import (
    FrontendEventPolicy,
    subscribe_all_frontend_events,
    subscribe_frontend_event,
)


def _collect_events() -> tuple[EventBus, list[tuple[str, Any]], Any]:
    bus = EventBus()
    received: list[tuple[str, Any]] = []

    def emit(frontend_event: str, payload: Any) -> None:
        received.append((frontend_event, payload))

    subscribe_all_frontend_events(bus, emit)
    return bus, received, emit


def test_bridge_renames_accounts_changed() -> None:
    bus, received, _ = _collect_events()
    bus.emit("accounts:changed", {"id": "a1"})
    assert received == [("accounts_changed", {"id": "a1"})]


def test_bridge_reshapes_config_updated() -> None:
    bus, received, _ = _collect_events()
    bus.emit("config:updated", "launcher", {"debug": True})
    assert received == [("config:updated", {"section": "launcher", "data": {"debug": True}})]


def test_bridge_maps_plugin_status_events_to_single_event() -> None:
    bus, received, _ = _collect_events()
    plugin = SimpleNamespace(name="my-plugin")
    bus.emit("plugin:enabled", plugin)
    bus.emit("plugin:disabled", plugin)
    bus.emit("plugin:unloaded", "my-plugin")
    assert received == [
        ("plugin:status_changed", {"name": "my-plugin", "action": "enabled", "result": True}),
        ("plugin:status_changed", {"name": "my-plugin", "action": "disabled", "result": True}),
        ("plugin:status_changed", {"name": "my-plugin", "action": "unloaded", "result": True}),
    ]


def test_bridge_puts_multiple_positional_args_into_frontend_object() -> None:
    bus, received, _ = _collect_events()
    bus.emit("plugin:css_injected", "my-plugin", "body { color: red }", "theme-key")
    bus.emit("plugin:vue_route_registered", "my-plugin", "/panel", "Panel", "PanelComp", "<p/>", "", "")
    assert received[0] == (
        "plugin:css_injected",
        {"plugin": "my-plugin", "css": "body { color: red }", "key": "theme-key"},
    )
    assert received[1] == (
        "plugin:vue_route_registered",
        {
            "plugin": "my-plugin",
            "path": "/panel",
            "title": "Panel",
            "component_name": "PanelComp",
            "template": "<p/>",
            "script": "",
            "style": "",
            "icon": "",
        },
    )


def test_bridge_passthrough_events_keep_name_and_payload() -> None:
    bus, received, _ = _collect_events()
    bus.emit("game:install_progress", {"done": 1, "total": 2})
    bus.emit("process:instances_changed", [{"id": "p1"}])
    bus.emit("update:check_completed", {"status": "up_to_date"})
    assert received == [
        ("game:install_progress", {"done": 1, "total": 2}),
        ("process:instances_changed", [{"id": "p1"}]),
        ("update:check_completed", {"status": "up_to_date"}),
    ]


def test_bridge_omits_side_effect_events() -> None:
    # launcher:error 的规范化与内存副本依赖 FrontendApi，不应进入纯转换桥。
    assert "launcher:error" not in FrontendEventPolicy.bridges


def test_bridge_subscribe_returns_working_unsubscribers() -> None:
    bus = EventBus()
    received: list[tuple[str, Any]] = []

    def emit(frontend_event: str, payload: Any) -> None:
        received.append((frontend_event, payload))

    unsubscribers = subscribe_frontend_event(bus, "plugin:css_injected", emit)
    assert len(unsubscribers) == 1
    bus.emit("plugin:css_injected", "p", "a", "k")
    unsubscribers[0]()
    bus.emit("plugin:css_injected", "p", "b", "k")
    assert received == [("plugin:css_injected", {"plugin": "p", "css": "a", "key": "k"})]


def test_bridge_ignores_unregistered_frontend_event() -> None:
    bus = EventBus()
    unsubscribers = subscribe_frontend_event(bus, "unknown.event:anything", lambda event, payload: None)
    assert unsubscribers == []
