# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过显式跨插件命令接入可选联机扩展。
#
# 公开接口：
#   - class RemoteExtensions — 插件业务接口。
# ============================================================

from __future__ import annotations

import base64
import json
import logging
from functools import partial
from uuid import uuid4

from ECL.plugin_api import HostApiError

from .extensions import ConnectorExtensionRegistry, ConnectorProtocolRequest, ConnectorSessionContext


class RemoteExtensions:
    """
    将可选 Worker 扩展接入本次房间，不把任何特定启动器协议写入联机核心。
    """

    command = "onlinegame.extension.v1"

    def __init__(self, plugin, registry: ConnectorExtensionRegistry) -> None:
        self._plugin = plugin
        self._registry = registry
        self._owners: set[str] = set()
        self._session = ""

    def prepare(self) -> None:
        """
        在每次新房间开始前发现当前扩展，固定双方运行身份及本次会话。
        """
        for owner in self._owners:
            self._registry.unregister_owner(owner)
        self._owners.clear()
        self._session = uuid4().hex
        for item in self._plugin.list_plugin_commands():
            if item.get("command") != self.command:
                continue
            owner = item["plugin"]
            try:
                description = self._plugin.call_plugin_command(
                    owner, self.command, {"payload": {"event": "describe"}}, timeout=5
                )["result"]
                if description.get("version") != 1:
                    continue
                binding = description["binding"]
                protocols = description["protocols"]
                if not isinstance(protocols, list) or len(protocols) > 8:
                    continue
                self._registry.register(
                    owner=owner,
                    name=description["name"],
                    protocols={protocol: partial(self._dispatch, owner, binding) for protocol in protocols},
                    on_guest_joined=partial(self._hook, owner, binding, "joined"),
                    enrich_status=partial(self._enrich, owner, binding),
                    before_leave=partial(self._hook, owner, binding, "leaving"),
                    on_reset=partial(self._hook, owner, binding, "reset"),
                )
                self._owners.add(owner)
            except (HostApiError, KeyError, TypeError, ValueError):
                logging.getLogger(__name__).warning("可选联机扩展未接入，基础联机仍可使用")

    def _call(self, owner: str, binding: str, payload: dict) -> dict:
        """
        所有回调只调用目标 Worker；目标失效立即从当前房间撤销扩展。
        """
        try:
            return self._transfer_call(owner, {**payload, "binding": binding, "session": self._session})
        except (HostApiError, KeyError, TypeError):
            self._registry.unregister_owner(owner)
            self._owners.discard(owner)
            raise

    def _transfer_call(self, owner: str, message: dict) -> dict:
        """
        图片和列表以有界块传输，不扩大普通控制命令的 16 KiB 上限。
        """

        def send(payload):
            return self._plugin.call_plugin_command(owner, self.command, {"payload": payload}, timeout=5)["result"]

        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        identity = {"binding": message["binding"], "session": message["session"]}
        if len(encoded) > 1024 * 1024:
            raise ValueError("扩展请求超过大小上限")
        if len(encoded) > 12000:
            reference = None
            for offset in range(0, len(encoded), 8192):
                result = send(
                    {
                        **identity,
                        "event": "transfer_put",
                        "transfer_ref": reference,
                        "offset": offset,
                        "total": len(encoded),
                        "content": base64.b64encode(encoded[offset : offset + 8192]).decode(),
                    }
                )
                reference = result["transfer_ref"]
            result = send({**identity, "event": "transfer_execute", "transfer_ref": reference})
        else:
            result = send(message)
        if "transfer_ref" not in result:
            return result
        size = result.get("transfer_size")
        if type(size) is not int or not 0 <= size <= 1024 * 1024:
            raise ValueError("扩展响应大小无效")
        chunks, offset = [], 0
        while offset is not None:
            part = send({**identity, "event": "transfer_get", "transfer_ref": result["transfer_ref"], "offset": offset})
            block = base64.b64decode(part["content"], validate=True)
            if len(block) > 8192 or sum(map(len, chunks)) + len(block) > size:
                raise ValueError("扩展响应分块无效")
            chunks.append(block)
            following = part["next_offset"]
            if following is not None and following != offset + len(block):
                raise ValueError("扩展响应位置无效")
            offset = following
        if sum(map(len, chunks)) != size:
            raise ValueError("扩展响应不完整")
        return json.loads(b"".join(chunks))

    async def _dispatch(self, owner: str, binding: str, request: ConnectorProtocolRequest):
        if len(request.body) > 512 * 1024:
            return 255, b"payload too large"
        payload = request.json({})
        result = self._call(
            owner,
            binding,
            {
                "event": "protocol",
                "protocol": request.protocol,
                "body": payload,
                "peer_machine_id": request.peer_machine_id,
                "game_info": dict(request.game_info or {}),
            },
        )
        remove = result.get("remove_player")
        if remove is not None:
            if remove != request.peer_machine_id:
                return 255, b"peer identity mismatch"
            await request.remove_player(remove)
        return 0, json.dumps(result.get("value"), ensure_ascii=False).encode("utf-8")

    def _hook(self, owner: str, binding: str, event: str, context: ConnectorSessionContext) -> dict:
        snapshot = {
            "event": event,
            "mode": context.mode,
            "machine_id": context.machine_id,
            "game_info": dict(context.game_info or {}),
            "local_icon": context.local_player_icon(),
        }
        plan = self._call(owner, binding, snapshot)
        responses = {}
        for request in plan.get("requests", [])[:8]:
            protocol = request.get("protocol")
            if protocol not in self._registry.protocol_names():
                continue
            try:
                responses[protocol] = context.request_json(protocol, request.get("body", {}))
            except (RuntimeError, ValueError, OSError, EOFError, TimeoutError):
                continue
        if responses:
            return self._call(owner, binding, {**snapshot, "event": "received", "responses": responses})
        return plan

    def _enrich(self, owner: str, binding: str, context: ConnectorSessionContext, status: dict) -> dict:
        result = self._hook(owner, binding, "refresh", context)
        patch = result.get("status", {})
        icons = patch.get("playerIcons", {})
        if isinstance(icons, dict):
            for player in status.get("players", []):
                player["iconBase64"] = icons.get(player.get("machineId"))
        return {key: value for key, value in patch.items() if key in {"gameInfo", "hostMods", "plugin_fields"}}
