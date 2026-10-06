# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 dev_channel 模块的自动化测试。
#
# 公开接口：
#   - service(tmp_path) -> Any
#   - test_start_writes_discovery_file(service) -> None
#   - test_close_removes_discovery_file(tmp_path) -> None
#   - test_auth_rejects_wrong_token(service) -> None
#   - test_launcher_info_reports_runtime_state(service, tmp_path) -> None
#   - test_unknown_method_returns_error(service) -> None
#   - test_plugin_list_maps_fields(tmp_path) -> None
#   - test_install_frontend_handlers_purges_and_fills(service) -> None
#   - test_frontend_invoke_dispatches_to_installed_handler(service) -> None
#   - test_frontend_invoke_without_payload_uses_empty_body(service) -> None
#   - test_frontend_invoke_unknown_command_returns_error(service) -> None
#   - test_plugin_reload_forwards_and_reports_missing(tmp_path) -> None
#   - test_plugin_reload_requires_name(service) -> None
#   - test_plugin_install_returns_installed_name(tmp_path) -> None
#   - test_plugin_archive_install_forwards_confirmation(tmp_path) -> None
#   - test_plugin_inspect_reports_unverified_source(tmp_path) -> None
#   - test_plugin_call_command_returns_result(tmp_path) -> None
#   - test_logs_subscribe_returns_history_and_pushes_live(service) -> None
#   - test_events_subscribe_filters_whitelist_and_forwards(service) -> None
#   - test_events_subscribe_rejects_invalid_payload(service) -> None
#   - test_multi_argument_event_forwards_as_list(service) -> None
#   - test_frontend_subscribe_rewrites_event_name_and_payload(service) -> None
#   - test_frontend_subscribe_reshapes_positional_event(service) -> None
#   - test_frontend_subscribe_ignores_unregistered_event(service) -> None
#   - test_frontend_subscribe_rejects_invalid_payload(service) -> None
#   - test_frontend_subscribe_idempotent_and_unsubscribe(service) -> None
#   - test_frontend_serving_serves_index_with_injected_connection(tmp_path) -> None
#   - test_frontend_serving_serves_static_asset(tmp_path) -> None
#   - test_frontend_serving_returns_404_for_missing_asset(tmp_path) -> None
#   - test_frontend_serving_falls_back_to_index_for_route(tmp_path) -> None
#   - test_frontend_serving_blocks_traversal(tmp_path) -> None
#   - test_frontend_serving_disabled_without_dist(tmp_path) -> None
#   - test_launcher_info_reports_frontend_url(tmp_path) -> None
#   - test_preview_session_injects_origin_bound_bridge(tmp_path) -> None
#   - test_preview_session_rejects_nonlocal_parent_origin(tmp_path) -> None
#   - test_preview_session_close_removes_bridge_injection(tmp_path) -> None
# ============================================================

"""验证开发者通道服务的鉴权、方法分发、日志与事件订阅能力。"""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ECL.foundation import EventBus
from ECL.plugins import PluginAction, PluginActionResult
from ECL.plugins.package_archive import PluginPackageInfo
from ECL.plugins.package_preparation import PluginPackagePreflight
from ECL.services.dev_channel import DevChannelService
from ECL.utils.logging import LoggingPolicy


def _action_result(name: str, action: PluginAction, status: str, message: str = "") -> PluginActionResult:
    return PluginActionResult(plugin_name=name, action=action, status=status, message=message)


def _make_service(
    tmp_path, plugins: Mock | None = None, events: EventBus | None = None, frontend_dist=None
) -> DevChannelService:
    return DevChannelService(
        plugins=plugins or Mock(),
        events=events or EventBus(),
        data_path=tmp_path / "ECL_data",
        launcher_version="1.4.2-alpha.3+20260906",
        debug=False,
        frontend_dist=frontend_dist,
    )


def _read_discovery(service: DevChannelService) -> dict[str, Any]:
    discovery_path = service._data_path / service.discovery_filename
    return json.loads(discovery_path.read_text(encoding="utf-8"))


@pytest.fixture
def service(tmp_path):
    # 测试进程未配置 LoggingRuntime，显式放开级别确保 INFO 日志能到达推送处理器。
    launcher_logger = logging.getLogger(LoggingPolicy.logger_name)
    original_level = launcher_logger.level
    launcher_logger.setLevel(logging.DEBUG)
    instance = _make_service(tmp_path)
    instance.start()
    yield instance
    instance.close()
    launcher_logger.setLevel(original_level)


async def _authed_client(service: DevChannelService):
    discovery = _read_discovery(service)
    websocket = await connect(f"ws://127.0.0.1:{discovery['port']}")
    await websocket.send(json.dumps({"op": "auth", "token": discovery["token"]}))
    reply = json.loads(await websocket.recv())
    assert reply["op"] == "auth_ok"
    assert reply["protocolVersion"] == 1
    assert reply["launcherVersion"] == "1.4.2-alpha.3+20260906"
    return websocket


async def _request(websocket, request_id: int, method: str, params: dict | None = None) -> dict:
    await websocket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    return json.loads(await websocket.recv())


def test_start_writes_discovery_file(service) -> None:
    discovery = _read_discovery(service)

    assert discovery["port"] == service.port
    assert discovery["pid"] > 0
    assert discovery["launcherVersion"] == "1.4.2-alpha.3+20260906"
    assert discovery["protocolVersion"] == 1
    assert len(discovery["token"]) > 20


def test_close_removes_discovery_file(tmp_path) -> None:
    service = _make_service(tmp_path)
    service.start()
    assert (service._data_path / service.discovery_filename).is_file()
    service.close()
    assert not (service._data_path / service.discovery_filename).exists()
    service.close()


async def test_auth_rejects_wrong_token(service) -> None:
    discovery = _read_discovery(service)
    websocket = await connect(f"ws://127.0.0.1:{discovery['port']}")
    await websocket.send(json.dumps({"op": "auth", "token": "wrong-token"}))
    reply = json.loads(await websocket.recv())
    assert reply["op"] == "auth_failed"
    with pytest.raises(ConnectionClosed):
        await websocket.recv()


async def test_launcher_info_reports_runtime_state(service, tmp_path) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 1, "launcher.info")
        assert reply["id"] == 1
        assert reply["ok"] is True
        data = reply["data"]
        assert data["version"] == "1.4.2-alpha.3+20260906"
        assert data["debug"] is False
        assert data["devChannel"] is True
        assert data["dataPath"] == str(tmp_path / "ECL_data")
        assert data["pluginDir"].endswith("plugins")
    finally:
        await websocket.close()


async def test_unknown_method_returns_error(service) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 2, "not.a_method")
        assert reply["ok"] is False
        assert reply["error"]["code"] == "METHOD_NOT_FOUND"
    finally:
        await websocket.close()


async def test_plugin_list_maps_fields(tmp_path) -> None:
    plugins = Mock()
    plugins.list_plugins.return_value = [
        {
            "name": "demo",
            "title": "演示插件",
            "version": "1.0.0",
            "description": "描述",
            "author": "作者",
            "status": "enabled",
            "error": None,
            "dependencies": {"core": ">=1.0"},
            "is_system": False,
        }
    ]
    service = _make_service(tmp_path, plugins=plugins)
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(websocket, 3, "plugin.list")
            assert reply["ok"] is True
            entry = reply["data"][0]
            assert entry["name"] == "demo"
            assert entry["isSystem"] is False
            assert entry["dependencies"] == {"core": ">=1.0"}
            assert "icon" not in entry
        finally:
            await websocket.close()
    finally:
        service.close()


def test_install_frontend_handlers_purges_and_fills(service) -> None:
    # 同一服务只安装于一个应用上下文，重复安装应整体替换而非累积。
    service.install_frontend_handlers({"system_ping": lambda body: {"pong": True}})
    service.install_frontend_handlers({"launcher_info": lambda body: {"version": "x"}})
    assert list(service._frontend_handlers) == ["launcher_info"]


async def test_frontend_invoke_dispatches_to_installed_handler(service) -> None:
    called: dict[str, Any] = {}

    async def handler(body: dict[str, Any]) -> dict[str, Any]:
        called.update(body)
        return {"echo": body.get("value")}

    service.install_frontend_handlers({"system_ping": handler})
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 4, "frontend.invoke", {"command": "system_ping", "payload": {"value": 42}})
        assert reply["ok"] is True
        assert reply["data"]["command"] == "system_ping"
        assert reply["data"]["result"] == {"echo": 42}
        assert called == {"value": 42}
    finally:
        await websocket.close()


async def test_frontend_invoke_without_payload_uses_empty_body(service) -> None:
    async def handler(body: dict[str, Any]) -> dict[str, Any]:
        return {"got": body}

    service.install_frontend_handlers({"system_ping": handler})
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 5, "frontend.invoke", {"command": "system_ping"})
        assert reply["ok"] is True
        assert reply["data"]["result"] == {"got": {}}
    finally:
        await websocket.close()


async def test_frontend_invoke_unknown_command_returns_error(service) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 6, "frontend.invoke", {"command": "no_such_command"})
        assert reply["ok"] is False
        assert reply["error"]["code"] == "METHOD_NOT_FOUND"
    finally:
        await websocket.close()


async def test_plugin_reload_forwards_and_reports_missing(tmp_path) -> None:
    plugins = Mock()
    plugins.reload.side_effect = [
        _action_result("demo", PluginAction.RELOAD, "enabled"),
        _action_result("ghost", PluginAction.RELOAD, "not_found", "插件不存在"),
    ]
    service = _make_service(tmp_path, plugins=plugins)
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(websocket, 4, "plugin.reload", {"name": "demo"})
            assert reply["ok"] is True
            assert reply["data"] == {"name": "demo"}
            plugins.reload.assert_called_once_with("demo")

            reply = await _request(websocket, 5, "plugin.reload", {"name": "ghost"})
            assert reply["ok"] is False
            assert reply["error"]["code"] == "PLUGIN_NOT_FOUND"
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_plugin_reload_requires_name(service) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 6, "plugin.reload", {})
        assert reply["ok"] is False
        assert reply["error"]["code"] == "INVALID_PARAMS"
    finally:
        await websocket.close()


async def test_plugin_install_returns_installed_name(tmp_path) -> None:
    plugins = Mock()
    plugins.install.return_value = _action_result("demo", PluginAction.INSTALL, "installed")
    service = _make_service(tmp_path, plugins=plugins)
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(websocket, 7, "plugin.install", {"path": str(tmp_path / "src")})
            assert reply["ok"] is True
            assert reply["data"] == {"name": "demo", "status": "installed", "message": ""}
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_plugin_archive_install_forwards_confirmation(tmp_path) -> None:
    """
    Dev Channel 把归档确认和联网选项交给同一安装服务。
    """
    plugins = Mock()
    plugins.install.return_value = _action_result("demo", PluginAction.INSTALL, "installed")
    service = _make_service(tmp_path, plugins=plugins)
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(
                websocket,
                8,
                "plugin.install",
                {"path": str(tmp_path / "demo.eclplugin"), "confirm_unverified_source": True, "allow_network": True},
            )
            assert reply["ok"] is True and reply["data"] == {"name": "demo", "status": "installed", "message": ""}
            plugins.install.assert_called_once_with(
                str(tmp_path / "demo.eclplugin"),
                confirm_unverified_source=True,
                allow_network=True,
            )
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_plugin_inspect_reports_unverified_source(tmp_path) -> None:
    """
    开发者通道可在安装前读取无签名预检摘要。
    """
    plugins = Mock()
    plugins.inspect_package.return_value = PluginPackagePreflight(
        PluginPackageInfo("demo", "1.0.0", "a" * 64, 2, 123), "windows-x86_64-cp312", (), 0, False
    )
    service = _make_service(tmp_path, plugins=plugins)
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(websocket, 9, "plugin.inspect", {"path": str(tmp_path / "demo.eclplugin")})
            assert reply["ok"] is True
            assert reply["data"]["unverified_source"] is True
            assert reply["data"]["package"]["name"] == "demo"
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_plugin_call_command_returns_result(tmp_path) -> None:
    plugins = Mock()
    plugins.call_command.return_value = {"echo": True}
    service = _make_service(tmp_path, plugins=plugins)
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(
                websocket,
                8,
                "plugin.call_command",
                {"command": "demo:echo", "params": {"value": 1}, "timeout": 5},
            )
            assert reply["ok"] is True
            assert reply["data"] == {"result": {"echo": True}}
            plugins.call_command.assert_called_once_with("demo:echo", {"value": 1}, timeout=5.0)
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_logs_subscribe_returns_history_and_pushes_live(service) -> None:
    websocket = await _authed_client(service)
    try:
        logging.getLogger(LoggingPolicy.logger_name).info("历史日志")
        reply = await _request(websocket, 9, "logs.subscribe")
        assert reply["ok"] is True
        assert reply["data"]["history"][-1]["message"] == "历史日志"
        assert reply["data"]["history"][-1]["level"] == "INFO"
        assert "T" in reply["data"]["history"][-1]["timestamp"]

        logging.getLogger(LoggingPolicy.logger_name).warning("实时日志")
        notification = json.loads(await websocket.recv())
        assert notification["event"] == "log.line"
        assert notification["data"]["message"] == "实时日志"
        assert notification["data"]["level"] == "WARNING"

        await _request(websocket, 10, "logs.unsubscribe")
        logging.getLogger(LoggingPolicy.logger_name).error("不应推送")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(websocket.recv(), timeout=0.5)
    finally:
        await websocket.close()


async def test_events_subscribe_filters_whitelist_and_forwards(service) -> None:
    events = service._events
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 11, "events.subscribe", {"events": ["plugin:enabled", "secret:leak"]})
        assert reply["ok"] is True
        assert reply["data"]["subscribed"] == ["plugin:enabled"]

        events.emit("plugin:enabled", {"plugin": "demo"})
        notification = json.loads(await websocket.recv())
        assert notification["event"] == "plugin:enabled"
        assert notification["data"] == {"plugin": "demo"}

        reply = await _request(websocket, 12, "events.unsubscribe", {"events": ["plugin:enabled"]})
        assert reply["data"]["unsubscribed"] == ["plugin:enabled"]
    finally:
        await websocket.close()


async def test_events_subscribe_rejects_invalid_payload(service) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 13, "events.subscribe", {"events": "plugin:enabled"})
        assert reply["ok"] is False
        assert reply["error"]["code"] == "INVALID_PARAMS"
    finally:
        await websocket.close()


async def test_multi_argument_event_forwards_as_list(service) -> None:
    events = service._events
    websocket = await _authed_client(service)
    try:
        await _request(websocket, 14, "events.subscribe", {"events": ["game:state"]})
        events.emit("game:state", "running", 42)
        notification = json.loads(await websocket.recv())
        assert notification["event"] == "game:state"
        assert notification["data"] == ["running", 42]
    finally:
        await websocket.close()


async def test_frontend_subscribe_rewrites_event_name_and_payload(service) -> None:
    events = service._events
    websocket = await _authed_client(service)
    try:
        reply = await _request(
            websocket, 15, "frontend.subscribe", {"events": ["accounts_changed", "plugin:status_changed"]}
        )
        assert reply["data"]["subscribed"] == ["accounts_changed", "plugin:status_changed"]

        events.emit("accounts:changed", {"id": "a1"})
        notification = json.loads(await websocket.recv())
        assert notification["event"] == "accounts_changed"
        assert notification["data"] == {"id": "a1"}

        events.emit("plugin:enabled", SimpleNamespace(name="demo"))
        notification = json.loads(await websocket.recv())
        assert notification["event"] == "plugin:status_changed"
        assert notification["data"] == {"name": "demo", "action": "enabled", "result": True}
    finally:
        await websocket.close()


async def test_frontend_subscribe_reshapes_positional_event(service) -> None:
    events = service._events
    websocket = await _authed_client(service)
    try:
        await _request(websocket, 16, "frontend.subscribe", {"events": ["plugin:css_injected"]})
        events.emit("plugin:css_injected", "demo", "body { color: red }", "theme-key")
        notification = json.loads(await websocket.recv())
        assert notification["event"] == "plugin:css_injected"
        assert notification["data"] == {"plugin": "demo", "css": "body { color: red }", "key": "theme-key"}
    finally:
        await websocket.close()


async def test_frontend_subscribe_ignores_unregistered_event(service) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 17, "frontend.subscribe", {"events": ["launcher:error"]})
        assert reply["data"]["subscribed"] == []
    finally:
        await websocket.close()


async def test_frontend_subscribe_rejects_invalid_payload(service) -> None:
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 18, "frontend.subscribe", {"events": "accounts_changed"})
        assert reply["ok"] is False
        assert reply["error"]["code"] == "INVALID_PARAMS"
    finally:
        await websocket.close()


async def test_frontend_subscribe_idempotent_and_unsubscribe(service) -> None:
    events = service._events
    websocket = await _authed_client(service)
    try:
        reply = await _request(websocket, 19, "frontend.subscribe", {"events": ["accounts_changed"]})
        assert reply["data"]["subscribed"] == ["accounts_changed"]
        reply = await _request(
            websocket, 20, "frontend.subscribe", {"events": ["accounts_changed", "unknown_frontend_event"]}
        )
        assert reply["data"]["subscribed"] == ["accounts_changed"]

        reply = await _request(websocket, 21, "frontend.unsubscribe", {"events": ["accounts_changed"]})
        assert reply["data"]["unsubscribed"] == ["accounts_changed"]
        events.emit("accounts:changed", {"id": "a2"})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(websocket.recv(), timeout=0.1)
    finally:
        await websocket.close()


def _make_frontend_dist(tmp_path):
    # 构造最小前端构建产物：入口与一个静态脚本。
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body>ok</body></html>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hello');", encoding="utf-8")
    return dist


async def _http_get(service: DevChannelService, path: str) -> httpx.Response:
    with httpx.Client(trust_env=False) as client:
        return client.get(f"http://127.0.0.1:{service.port}{path}")


async def test_frontend_serving_serves_index_with_injected_connection(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        discovery = _read_discovery(service)
        assert discovery["frontendUrl"] == f"http://127.0.0.1:{service.port}/"
        assert service.frontend_url == f"http://127.0.0.1:{service.port}/"

        response = await _http_get(service, "/")
        assert response.status_code == 200
        assert "text/html" in response.headers["Content-Type"]
        assert f'window.__ECL_DEV_WS__={{port:{service.port},token:"{discovery["token"]}"}}' in response.text
        # 命中 get 断言的是注入后的连接信息，不存在原始 token 泄露之外的误匹配
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        service.close()


async def test_frontend_serving_serves_static_asset(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        response = await _http_get(service, "/assets/app.js")
        assert response.status_code == 200
        assert response.text == "console.log('hello');"
        assert "javascript" in response.headers["Content-Type"]
    finally:
        service.close()


async def test_frontend_serving_returns_404_for_missing_asset(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        response = await _http_get(service, "/assets/missing.js")
        assert response.status_code == 404
    finally:
        service.close()


async def test_frontend_serving_falls_back_to_index_for_route(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        response = await _http_get(service, "/overview")
        assert response.status_code == 200
        assert "text/html" in response.headers["Content-Type"]
        assert "window.__ECL_DEV_WS__" in response.text
    finally:
        service.close()


async def test_frontend_serving_blocks_traversal(tmp_path) -> None:
    dist = _make_frontend_dist(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    service = _make_service(tmp_path, frontend_dist=dist)
    service.start()
    try:
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            response = client.get(f"http://127.0.0.1:{service.port}/../secret.txt")
        assert response.status_code in (200, 404)
        assert "top-secret" not in response.text
    finally:
        service.close()


async def test_frontend_serving_disabled_without_dist(tmp_path) -> None:
    service = _make_service(tmp_path)
    service.start()
    try:
        discovery = _read_discovery(service)
        assert discovery["frontendUrl"] is None
        assert service.frontend_url is None
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{service.port}/")
        assert response.status_code == 426  # websockets 对非升级请求返回 426 Upgrade Required
    finally:
        service.close()


async def test_launcher_info_reports_frontend_url(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(websocket, 22, "launcher.info")
            assert reply["data"]["frontendUrl"] == f"http://127.0.0.1:{service.port}/"
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_preview_session_injects_origin_bound_bridge(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(
                websocket,
                23,
                "preview.session.create",
                {"parentOrigin": "http://tauri.localhost"},
            )
            assert reply["ok"] is True
            preview = reply["data"]
            assert preview["url"].startswith(f"http://127.0.0.1:{service.port}/?eclPreviewSession=")
            assert preview["expiresInSeconds"] == service.preview_session_ttl_seconds

            with httpx.Client(trust_env=False) as client:
                response = client.get(preview["url"])
            assert response.status_code == 200
            assert "window.__ECL_PREVIEW_SESSION__" in response.text
            assert preview["nonce"] in response.text
            assert '"parentOrigin": "http://tauri.localhost"' in response.text
            assert "channel:'ecl-preview'" in response.text
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_preview_session_rejects_nonlocal_parent_origin(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            reply = await _request(
                websocket,
                24,
                "preview.session.create",
                {"parentOrigin": "https://example.com"},
            )
            assert reply["ok"] is False
            assert reply["error"]["code"] == "INVALID_PARAMS"
        finally:
            await websocket.close()
    finally:
        service.close()


async def test_preview_session_close_removes_bridge_injection(tmp_path) -> None:
    service = _make_service(tmp_path, frontend_dist=_make_frontend_dist(tmp_path))
    service.start()
    try:
        websocket = await _authed_client(service)
        try:
            created = await _request(
                websocket,
                25,
                "preview.session.create",
                {"parentOrigin": "http://tauri.localhost"},
            )
            nonce = created["data"]["nonce"]
            closed = await _request(websocket, 26, "preview.session.close", {"nonce": nonce})
            assert closed["data"] == {"nonce": nonce}

            with httpx.Client(trust_env=False) as client:
                response = client.get(created["data"]["url"])
            assert response.status_code == 200
            assert "window.__ECL_PREVIEW_SESSION__" not in response.text
        finally:
            await websocket.close()
    finally:
        service.close()
