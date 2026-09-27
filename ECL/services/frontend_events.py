# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：前端事件桥：后端事件总线到前端事件名的订阅注册。
#
# 公开接口：
#   - subscribe_frontend_event(bus, frontend_event, emit, owner) -> list[Unsubscribe] — 订阅指定前端事件对应的全部后端事件，把转换后的载荷回调给 emit。
#   - subscribe_all_frontend_events(bus, emit, owner) -> list[Unsubscribe] — 订阅事件桥注册的全部前端事件，供主窗口适配器在启动时一次性接通。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ECL.events.event_bus import EventBus, Unsubscribe

Emit = Callable[[str, Any], None]


# 后端事件到前端事件的纯转换规则：键为前端事件名，值为「后端事件名 + 载荷构造器」。
# launcher:error、账户登录唤起窗口、插件窗口清理等含副作用的事件不属于本表，
# 由具体适配器自行订阅保持行为一致。
class FrontendEventPolicy:
    """
    保存后端事件到前端事件的转换规则。
    """

    bridges: dict[str, list[tuple[str, Callable[..., Any]]]] = {
        "config:updated": [("config:updated", lambda section, data: {"section": section, "data": data})],
        "accounts_changed": [("accounts:changed", lambda data: data)],
        "accounts_microsoft_login_status": [("accounts:microsoft_login_status", lambda data: data)],
        "launcher:popup": [("launcher:popup", lambda payload: payload if isinstance(payload, dict) else None)],
        "launcher:notify": [("launcher:notify", lambda payload: payload)],
        "launcher:visibility": [
            ("launcher:visibility", lambda payload: payload if isinstance(payload, dict) else None)
        ],
        "game:install_progress": [("game:install_progress", lambda payload: payload)],
        "update:progress": [("update:progress", lambda payload: payload)],
        "update:check_completed": [("update:check_completed", lambda payload: payload)],
        "game:launch_progress": [("game:launch_progress", lambda payload: payload)],
        "game:versions_changed": [("game:versions_changed", lambda payload: payload)],
        "game:instances_changed": [("game:instances_changed", lambda payload: payload)],
        "game:operation_progress": [("game:operation_progress", lambda payload: payload)],
        "launcher:log": [("launcher:log", lambda payload: payload)],
        "process:instance_log": [("process:instance_log", lambda payload: payload)],
        "process:instances_changed": [("process:instances_changed", lambda payload: payload)],
        "plugin:status_changed": [
            ("plugin:enabled", lambda plugin: {"name": plugin.name, "action": "enabled", "result": True}),
            ("plugin:disabled", lambda plugin: {"name": plugin.name, "action": "disabled", "result": True}),
            ("plugin:unloaded", lambda name: {"name": name, "action": "unloaded", "result": True}),
        ],
        "plugin:installed": [("plugin:installed", lambda name: {"name": name})],
        "plugin:css_injected": [
            ("plugin:css_injected", lambda plugin, css, key: {"plugin": plugin, "css": css, "key": key})
        ],
        "plugin:html_injected": [
            (
                "plugin:html_injected",
                lambda plugin, slot, html, key, context_key=None: {
                    "plugin": plugin,
                    "slot": slot,
                    "html": html,
                    "key": key,
                    "contextKey": context_key,
                },
            )
        ],
        "plugin:script_injected": [
            ("plugin:script_injected", lambda plugin, script: {"plugin": plugin, "script": script})
        ],
        "plugin:typescript_injected": [
            ("plugin:typescript_injected", lambda plugin, script: {"plugin": plugin, "script": script})
        ],
        "plugin:route_registered": [
            (
                "plugin:route_registered",
                lambda plugin, path, title, icon="": {"plugin": plugin, "path": path, "title": title, "icon": icon},
            )
        ],
        "plugin:settings_changed": [
            (
                "plugin:settings_changed",
                lambda plugin, key, old_value, new_value: {
                    "plugin": plugin,
                    "key": key,
                    "old_value": old_value,
                    "new_value": new_value,
                },
            )
        ],
        "plugin:vue_slot_registered": [
            (
                "plugin:vue_slot_registered",
                lambda plugin, slot, component_name, template, script, style, context_key=None: {
                    "plugin": plugin,
                    "slot": slot,
                    "component_name": component_name,
                    "template": template,
                    "script": script,
                    "style": style,
                    "contextKey": context_key,
                },
            )
        ],
        "plugin:vue_route_registered": [
            (
                "plugin:vue_route_registered",
                lambda plugin, path, title, component_name, template, script, style, icon="": {
                    "plugin": plugin,
                    "path": path,
                    "title": title,
                    "component_name": component_name,
                    "template": template,
                    "script": script,
                    "style": style,
                    "icon": icon,
                },
            )
        ],
    }


def _build_forwarder(emit: Emit, frontend_event: str, builder: Callable[..., Any]) -> Callable[..., None]:
    # 把一次后端事件分发转换为前端事件回调，载荷无法构造时（产物为 None）跳过推送。
    def forward(*args: Any) -> None:
        payload = builder(*args)
        if payload is not None:
            emit(frontend_event, payload)

    return forward


def subscribe_frontend_event(
    bus: EventBus, frontend_event: str, emit: Emit, *, owner: str = "FrontendBridge"
) -> list[Unsubscribe]:
    """
    订阅指定前端事件对应的全部后端事件，把转换后的载荷回调给 emit。

    :param bus: 应用事件总线
    :param frontend_event: 前端事件名，须在 FrontendEventPolicy.bridges 中注册
    :param emit: 收到事件时以 (前端事件名, 载荷) 调用的回调
    :param owner: 事件订阅归属标识，便于成批移除
    :return: 每个后端订阅对应的退订函数
    """
    unsubscribers = []
    for backend_event, builder in FrontendEventPolicy.bridges.get(frontend_event, ()):
        forwarder = _build_forwarder(emit, frontend_event, builder)
        unsubscribers.append(bus.subscribe(backend_event, forwarder, owner=owner))
    return unsubscribers


def subscribe_all_frontend_events(bus: EventBus, emit: Emit, *, owner: str = "FrontendBridge") -> list[Unsubscribe]:
    """
    订阅事件桥注册的全部前端事件，供主窗口适配器在启动时一次性接通。

    :param bus: 应用事件总线
    :param emit: 收到事件时以 (前端事件名, 载荷) 调用的回调
    :param owner: 事件订阅归属标识
    :return: 全部后端订阅对应的退订函数
    """
    unsubscribers = []
    for frontend_event in FrontendEventPolicy.bridges:
        unsubscribers.extend(subscribe_frontend_event(bus, frontend_event, emit, owner=owner))
    return unsubscribers


__all__ = ["FrontendEventPolicy", "subscribe_all_frontend_events", "subscribe_frontend_event"]
