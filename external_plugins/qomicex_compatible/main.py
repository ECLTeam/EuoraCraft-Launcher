# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义普通 Worker 插件的业务命令、生命周期与宿主接口。
#
# 公开接口：
#   - class Plugin — 插件业务接口。
# ============================================================

from __future__ import annotations

import base64
import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

from transfer import JsonTransfers

from ECL.plugin_api import HostApiError, JsonValue
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    只读实例兼容和可选联机协议；没有联机插件时仍可单独使用。
    """

    protocols = ("qml:game_info", "qml:player_icons", "qml:player_leave", "qml:game_mods")

    def on_load(self) -> None:
        """
        登记用户选定索引和可选协议命令，不声明对联机插件的依赖。
        """
        self._lock = RLock()
        self._binding = uuid4().hex
        self._transfers = JsonTransfers()
        self._sessions: dict[str, dict] = {}
        self._index_revision: str | None = None
        self._index: list[dict] = []
        self.host.call(
            "settings.define",
            {
                "definitions": [
                    {
                        "key": "instances_index",
                        "type": "readonly_file",
                        "default": "",
                        "description": "QomicEX instances.json 的完整路径；仅由用户选择，只读不修改",
                    }
                ]
            },
        )
        self.define_command(
            "onlinegame.extension.v1",
            self.extension,
            exported=True,
            input_fields=({"name": "payload", "type": "json", "max_length": 16384},),
            output_fields=({"name": "result", "type": "json", "max_length": 16384},),
        )
        self.define_command(
            "detect_index", self.detect_index, output_fields=({"name": "result", "type": "json", "max_length": 16384},)
        )

    def on_enable(self) -> None:
        """
        获准实例读取时登记来源，拒绝授权不阻断可选联机扩展。
        """
        try:
            self.register_instance_source("qomicex", "QomicEX", self.read_instance)
        except HostApiError as exc:
            if exc.error_code != "PLUGIN_PERMISSION_DENIED":
                raise

    def detect_index(self) -> dict[str, JsonValue]:
        """
        提供候选路径供用户检查；不自动写入文件选择或读取索引内容。
        """
        import os

        candidates = []
        configured = os.environ.get("QOMICEX_HOME")
        if configured:
            candidates.append(str(Path(configured) / "instances.json"))
        base = Path.home() / "AppData/Local/qomicex-launcher"
        candidates.extend(str(path) for path in (base / "instances.json", base / "data/instances.json"))
        return {"result": {"candidates": candidates}}

    def _load_index(self) -> tuple[list[dict], int]:
        """
        经宿主只读引用按修订读取，变化时从头重试由下一次扫描负责。
        """
        first = self.host.call("compat.read_index", {"key": "instances_index"})
        revision = first["revision"]
        if self._index_revision == revision:
            return self._index, int(revision[:12], 16)
        chunks = [base64.b64decode(first["content"], validate=True)]
        following = first["next_offset"]
        while following is not None:
            part = self.host.call(
                "compat.read_index", {"key": "instances_index", "offset": following, "revision": revision}
            )
            chunks.append(base64.b64decode(part["content"], validate=True))
            following = part["next_offset"]
        document = b"".join(chunks)
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                value = json.loads(document.decode(encoding))
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("实例索引文本编码不支持")
        if isinstance(value, dict):
            value = next((value[key] for key in ("instances", "data", "items") if isinstance(value.get(key), list)), [])
        if not isinstance(value, list):
            raise ValueError("实例索引应包含实例列表")
        self._index = [item for item in value if isinstance(item, dict)][:10000]
        self._index_revision = revision
        return self._index, int(revision[:12], 16)

    def read_instance(self, context: dict) -> dict[str, JsonValue]:
        """
        返回当前获准实例的完整兼容元数据，路径歧义时不猜测匹配。
        """
        with self._lock:
            try:
                instances, modified = self._load_index()
            except (HostApiError, ValueError):
                return {"fields": {}, "warnings": ["QomicEX 索引未选择、无法读取或格式无效，请检查插件设置"]}
            expected = str(Path(context["instance_path"]).resolve()).casefold()
            matches = [
                item
                for item in instances
                if item.get("gameDir") and str(Path(item["gameDir"]).resolve()).casefold() == expected
            ]
            if not matches:
                matches = [
                    item
                    for item in instances
                    if str(item.get("name", "")).casefold() == context["version_id"].casefold()
                    and str(item.get("gameVersion") or "").casefold() in {"", context["vanilla_name"].casefold()}
                    and str(item.get("loader") or "vanilla").casefold() in {"", context["primary_loader"].casefold()}
                ]
            if len(matches) != 1:
                return {"fields": {}, "warnings": ["QomicEX 实例匹配存在歧义"] if matches else []}
            item = matches[0]
            fields: dict[str, JsonValue] = {}
            if isinstance(item.get("modpackSummary"), str):
                fields["description"] = item["modpackSummary"]
            for origin, target in (("isHidden", "hidden"), ("isDefault", "pinned")):
                if type(item.get(origin)) is bool:
                    fields[target] = item[origin]
            icon = item.get("iconData")
            if isinstance(icon, str) and icon.startswith("data:image/png;base64,"):
                fields["icon"] = {"type": "data", "value": icon, "source": "qomicex"}
            elif isinstance(item.get("icon"), str) and item["icon"]:
                name = item["icon"].casefold()
                fields["icon"] = {
                    "type": "loader" if name in {"forge", "neoforge", "fabric", "quilt", "optifine"} else "builtin",
                    "value": name,
                    "source": "qomicex",
                }
            stats: dict[str, JsonValue] = {}
            duration = item.get("playTime")
            if type(duration) in {int, float} and duration >= 0:
                stats["totalRunDurationSeconds"] = int(duration * 60)
            if isinstance(item.get("lastPlayed"), str):
                stats["lastLaunchedAt"] = item["lastPlayed"]
            return {"fields": fields, "stats": stats, "modified_ns": modified, "warnings": []}

    def extension(self, payload: dict) -> dict[str, JsonValue]:
        """
        在校验运行和会话后处理控制分块，拒绝跨运行的传输引用。
        """
        if payload.get("event") == "describe":
            return {"result": self._extension(payload)}
        if payload.get("binding") != self._binding:
            raise ValueError("兼容扩展的运行身份已失效")
        if not isinstance(payload.get("session"), str) or len(payload["session"]) != 32:
            raise ValueError("房间引用无效")
        return {"result": self._transfers.handle(payload, self._extension)}

    def _extension(self, payload: dict) -> dict:
        """
        处理带运行绑定和房间代次的协议语义，永不反向同步调用联机插件。
        """
        event = payload.get("event")
        if event == "describe":
            return {"name": "qomicex", "version": 1, "binding": self._binding, "protocols": list(self.protocols)}
        if payload.get("binding") != self._binding:
            raise ValueError("兼容扩展的运行身份已失效")
        session = payload.get("session")
        if not isinstance(session, str) or len(session) != 32:
            raise ValueError("房间引用无效")
        with self._lock:
            if event == "reset":
                self._sessions.pop(session, None)
                return {}
            if session not in self._sessions:
                if len(self._sessions) >= 8:
                    raise ValueError("会话数量超过上限")
                self._sessions[session] = {"icons": {}, "game_info": {}, "mods": []}
            state = self._sessions[session]
            local_icon = payload.get("local_icon")
            machine_id = payload.get("machine_id")
            if isinstance(local_icon, str) and isinstance(machine_id, str):
                state["icons"][machine_id] = local_icon
            if event == "protocol":
                return self._protocol(payload, state)
            self._receive(event, payload, state)
            requests = []
            if event in {"joined", "refresh"} and payload.get("mode") == "guest":
                requests = [
                    {
                        "protocol": protocol,
                        "body": {"machineId": machine_id, "iconBase64": local_icon}
                        if protocol == "qml:player_icons" and machine_id and local_icon
                        else {},
                    }
                    for protocol in self.protocols
                    if protocol != "qml:player_leave"
                ]
            if event == "leaving" and payload.get("mode") == "guest":
                requests = [{"protocol": "qml:player_leave", "body": {"machineId": payload.get("machine_id")}}]
            return {
                "requests": requests,
                "status": {"gameInfo": state["game_info"], "hostMods": state["mods"], "playerIcons": state["icons"]},
            }

    def _receive(self, event: str, payload: dict, state: dict) -> None:
        if event == "received":
            responses = payload.get("responses", {})
            for protocol, target, field in (
                ("qml:game_info", "game_info", None),
                ("qml:player_icons", "icons", "icons"),
                ("qml:game_mods", "mods", "mods"),
            ):
                value = responses.get(protocol)
                if isinstance(value, dict):
                    state[target] = value.get(field, {}) if field else value

    def _protocol(self, payload: dict, state: dict) -> dict:
        protocol, body = payload.get("protocol"), payload.get("body", {})
        if not isinstance(body, dict):
            raise ValueError("协议请求结构无效")
        if protocol == "qml:game_info":
            return {"value": payload.get("game_info") or {"gameVersion": "", "loader": None, "loaderVersion": None}}
        if protocol == "qml:game_mods":
            return {"value": {"mods": state["mods"]}}
        machine_id = body.get("machineId")
        if machine_id and machine_id != payload.get("peer_machine_id"):
            raise ValueError("玩家身份与连接不符")
        if protocol == "qml:player_leave":
            state["icons"].pop(machine_id, None)
            return {"value": True, "remove_player": machine_id}
        if protocol == "qml:player_icons":
            icon = body.get("iconBase64")
            if machine_id and isinstance(icon, str) and len(icon) <= 700000:
                state["icons"][machine_id] = icon
            return {"value": {"icons": state["icons"]}}
        raise ValueError("协议未声明")

    def on_disable(self) -> None:
        """
        丢弃本运行会话缓存，宿主自动撤销来源和导出命令。
        """
        with self._lock:
            self._sessions.clear()
            self._index.clear()
