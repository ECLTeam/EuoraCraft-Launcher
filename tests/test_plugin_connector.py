# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_connector 模块的自动化测试。
#
# 公开接口：
#   - test_plugin_connector_extension_requires_permission_and_is_cleaned_on_disable(tmp_path) -> None
#   - test_qomicex_system_plugin_implements_host_and_guest_extension_protocols(tmp_path) -> None
#   - test_qomicex_plugin_uploads_and_registers_its_own_local_player_icon(tmp_path) -> None
# ============================================================

import asyncio
import json
from pathlib import Path

from ECL.plugins import (
    ConnectorExtensionRegistry,
    ConnectorProtocolRequest,
    ConnectorSessionContext,
    PluginManager,
)


def test_plugin_connector_extension_requires_permission_and_is_cleaned_on_disable(tmp_path) -> None:
    data_path = tmp_path / "data"
    plugin_dir = tmp_path / "resources/resources/system_plugins/connector-demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "connector-demo",
                "entry_point": "main:ConnectorPlugin",
                "permissions": [{"scope": "connector", "action": "write", "resource": "demo"}],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins import Plugin\n"
        "class ConnectorPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_connector_extension('demo', {'demo:echo': lambda request: request.body})\n",
        encoding="utf-8",
    )

    registry = ConnectorExtensionRegistry()
    framework = PluginManager(connector_extensions=registry)
    framework.initialize(data_path, tmp_path / "resources")

    assert registry.protocol_names() == ["demo:echo"]
    assert asyncio.run(registry.dispatch(ConnectorProtocolRequest("demo:echo", b"hello"))) == (0, b"hello")
    framework.connector_extensions.unregister_owner("connector-demo")
    assert registry.protocol_names() == []


def test_qomicex_system_plugin_implements_host_and_guest_extension_protocols(tmp_path) -> None:
    registry = ConnectorExtensionRegistry()
    framework = PluginManager(connector_extensions=registry)
    project_root = Path(__file__).resolve().parents[1]
    framework.initialize(tmp_path / "data", project_root)

    assert registry.protocol_names() == [
        "qml:game_info",
        "qml:game_mods",
        "qml:player_icons",
        "qml:player_leave",
    ]

    game_info_request = ConnectorProtocolRequest(
        "qml:game_info",
        b"",
        game_info={"gameVersion": "1.21.8", "loader": "Fabric", "loaderVersion": "0.17.2"},
    )
    status, body = asyncio.run(registry.dispatch(game_info_request))
    assert status == 0
    assert json.loads(body) == {
        "gameVersion": "1.21.8",
        "loader": "Fabric",
        "loaderVersion": "0.17.2",
    }

    icon_upload = ConnectorProtocolRequest(
        "qml:player_icons",
        json.dumps({"machineId": "guest-1", "iconBase64": "aWNvbg=="}).encode(),
    )
    status, body = asyncio.run(registry.dispatch(icon_upload))
    assert status == 0
    assert json.loads(body) == {"icons": {"guest-1": "aWNvbg=="}}

    removed: list[str] = []

    async def remove_player(machine_id: str) -> None:
        removed.append(machine_id)

    leave_request = ConnectorProtocolRequest(
        "qml:player_leave",
        json.dumps({"machineId": "guest-1"}).encode(),
        _remove_player=remove_player,
    )
    status, body = asyncio.run(registry.dispatch(leave_request))
    assert (status, json.loads(body)) == (0, True)
    assert removed == ["guest-1"]

    requests: list[tuple[str, dict | None]] = []

    def request(protocol: str, body: bytes) -> tuple[int, bytes]:
        payload = json.loads(body) if body else None
        requests.append((protocol, payload))
        responses = {
            "qml:game_info": {"gameVersion": "1.20.1", "loader": "Forge", "loaderVersion": "47.4.0"},
            "qml:player_icons": {"icons": {"host-1": "aG9zdA=="}},
            "qml:game_mods": {"mods": []},
            "qml:player_leave": True,
        }
        return 0, json.dumps(responses[protocol]).encode()

    context = ConnectorSessionContext(
        mode="guest",
        room_code="U/TEST-ROOM",
        machine_id="guest-1",
        game_info=None,
        _request=request,
    )
    registry.guest_joined(context)
    enriched = registry.enrich_status(
        context,
        {
            "gameInfo": None,
            "players": [{"machineId": "host-1", "iconBase64": None}],
        },
    )
    registry.before_leave(context)

    assert enriched["gameInfo"]["gameVersion"] == "1.20.1"
    assert enriched["players"][0]["iconBase64"] == "aG9zdA=="
    assert ("qml:player_leave", {"machineId": "guest-1"}) in requests


def test_qomicex_plugin_uploads_and_registers_its_own_local_player_icon(tmp_path) -> None:
    registry = ConnectorExtensionRegistry()
    framework = PluginManager(connector_extensions=registry)
    project_root = Path(__file__).resolve().parents[1]
    framework.initialize(tmp_path / "data", project_root)

    requests: list[tuple[str, dict | None]] = []

    def request(protocol: str, body: bytes) -> tuple[int, bytes]:
        payload = json.loads(body) if body else None
        requests.append((protocol, payload))
        responses = {
            "qml:game_info": {"gameVersion": "1.20.1", "loader": "Forge", "loaderVersion": "47.4.0"},
            "qml:player_icons": {"icons": {"host-1": "aG9zdA=="}},
            "qml:game_mods": {"mods": []},
        }
        return 0, json.dumps(responses[protocol]).encode()

    def local_icon() -> str | None:
        return "c2VsZg=="

    guest_context = ConnectorSessionContext(
        mode="guest",
        room_code="U/TEST-ROOM",
        machine_id="guest-1",
        game_info=None,
        _request=request,
        _local_icon_provider=local_icon,
    )
    registry.guest_joined(guest_context)
    uploads = [
        entry for entry in requests if entry[0] == "qml:player_icons" and entry[1] and entry[1].get("iconBase64")
    ]
    assert uploads
    assert uploads[-1][1]["machineId"] == "guest-1"
    assert uploads[-1][1]["iconBase64"] == "c2VsZg=="

    guest_status = registry.enrich_status(
        guest_context,
        {"gameInfo": None, "players": [{"machineId": "guest-1", "iconBase64": None}]},
    )
    assert guest_status["players"][0]["iconBase64"] == "c2VsZg=="

    # 房主没有客户端请求能力，仅凭 kind==HOST 条目注册并展示自己的头像
    host_registry = ConnectorExtensionRegistry()
    host_framework = PluginManager(connector_extensions=host_registry)
    host_framework.initialize(tmp_path / "data-host", project_root)
    host_context = ConnectorSessionContext(
        mode="host",
        room_code="U/TEST-ROOM",
        machine_id=None,
        game_info=None,
        _local_icon_provider=local_icon,
    )
    host_status = host_registry.enrich_status(
        host_context,
        {"gameInfo": None, "players": [{"machineId": "host-1", "name": "Host", "kind": "HOST", "iconBase64": None}]},
    )
    assert host_status["players"][0]["iconBase64"] == "c2VsZg=="
