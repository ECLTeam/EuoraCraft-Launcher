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
import hashlib
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from uuid import uuid4

from onlinegame.extensions import ConnectorExtensionRegistry
from onlinegame.nodes import ConnectorNodeConfig
from onlinegame.remote_extensions import RemoteExtensions
from onlinegame.service import ConnectorService

from ECL.plugin_api import HostApiError, JsonValue
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    在独立 Worker 中拥有联机网络与页面命令，不依赖任何兼容插件。
    """

    def on_load(self) -> None:
        """
        登记自身配置和有界命令，只有执行联机操作才创建网络会话。
        """
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="OnlineGame")
        self._tasks: dict[str, Future] = {}
        self._stopped = False
        self._local_icon: str | None = None
        self._icons: dict[str, bytes] = {}
        self.host.call(
            "settings.define",
            {
                "definitions": [
                    {"key": "player_name", "type": "string", "default": "Player", "description": "联机玩家名"},
                    {
                        "key": "node_mode",
                        "type": "select",
                        "default": "automatic",
                        "options": ["automatic", "append", "custom"],
                        "description": "节点策略",
                    },
                    {
                        "key": "nodes",
                        "type": "string",
                        "default": "",
                        "description": "自定义节点，每行一个带协议和端口的地址",
                    },
                ]
            },
        )
        self._extensions = ConnectorExtensionRegistry()
        self._remote = RemoteExtensions(self, self._extensions)
        self._connector = ConnectorService(
            extensions=self._extensions,
            node_settings_provider=self._node_settings,
            local_player_icon_provider=lambda: self._local_icon,
        )
        commands = {
            "status": self.status,
            "host_port": self.host_port,
            "host_instance": self.host_instance,
            "join": self.join,
            "leave": self.leave,
            "kick": self.kick,
            "nat_type": self.nat_type,
            "detect_ports": self.detect_ports,
            "search_mc_port": self.search_mc_port,
            "nodes_get": self.nodes_get,
            "nodes_set": self.nodes_set,
            "operation": self.operation,
            "instances": self.instances,
            "player_icon": self.player_icon,
        }
        for name, callback in commands.items():
            self.define_command(
                name,
                callback,
                input_fields=({"name": "payload", "type": "json", "max_length": 16384, "required": False},),
                output_fields=({"name": "result", "type": "json", "max_length": 16384},),
                exported=True,
                is_query=name in {"status", "operation", "nodes_get", "instances", "player_icon"},
            )
        try:
            ui = Path(__file__).parent / "ui"
            # 保留原模板与样式；仅去掉传输模板的缩进，避免超过 Worker 单帧上限。
            template = "\n".join(
                line.strip() for line in (ui / "template.html").read_text(encoding="utf-8").splitlines()
            )
            self.define_vue_page(
                "/room",
                "联机",
                "OnlineGame",
                template,
                script=(ui / "original-templates.js").read_text(encoding="utf-8")
                + (ui / "page.js").read_text(encoding="utf-8"),
                style=(ui / "page.css").read_text(encoding="utf-8"),
                icon="wifi",
                locales=("zh-CN",),
                container="more",
                nav={"path": "room", "label_key": "more.nav.room", "icon": "wifi"},
            )
        except HostApiError as exc:
            if exc.error_code not in {"PLUGIN_PERMISSION_DENIED", "PLUGIN_UI_INJECTION_DISABLED"}:
                raise

    def _settings(self) -> dict:
        return self.host.call("settings.get", {"keys": ["player_name", "node_mode", "nodes"]})["values"]

    def _node_settings(self) -> ConnectorNodeConfig:
        values = self._settings()
        return ConnectorNodeConfig(mode=values["node_mode"], nodes=values["nodes"].splitlines())

    def _submit(self, action, *, prepare: bool = False) -> dict[str, JsonValue]:
        with self._lock:
            if self._stopped or any(not future.done() for future in self._tasks.values()):
                raise ValueError("联机操作尚未结束，请等待当前结果")
            self._tasks = dict(list(self._tasks.items())[-15:])
            reference = uuid4().hex

            def execute():
                if prepare:
                    if self._connector.get_status()["mode"] != "idle":
                        raise ValueError("请先退出当前房间")
                    self._connector._player_name = self._settings()["player_name"] or "Player"
                    self._load_account()
                    self._icons.clear()
                    self._remote.prepare()
                return action()

            self._tasks[reference] = self._executor.submit(execute)
            return {"result": {"operation_ref": reference, "state": "queued"}}

    def status(self, payload=None) -> dict[str, JsonValue]:
        """
        查询实际网络状态，扩展数据只对当前会话生效。
        """
        status = self._connector.get_status()
        offset = payload.get("offset", 0) if isinstance(payload, dict) else 0
        if type(offset) is not int or not 0 <= offset <= 4096:
            raise ValueError("玩家分页位置无效")
        players = status.get("players", [])
        self._icons = {}
        for player in players:
            icon = player.get("iconBase64")
            if isinstance(icon, str) and len(icon) <= 700000:
                try:
                    self._icons[str(player.get("machineId"))] = base64.b64decode(icon, validate=True)
                except ValueError:
                    continue
        status["players"] = players[offset : offset + 16]
        status["next_offset"] = offset + 16 if offset + 16 < len(players) else None
        for player in status["players"]:
            player["iconBase64"] = None
            image = self._icons.get(str(player.get("machineId")))
            player["iconRevision"] = hashlib.sha256(image).hexdigest() if image else None
        return {"result": status}

    def _load_account(self) -> None:
        """
        只读取用户选择的账户与其皮肤；拒绝授权时继续使用手动玩家名。
        """
        self._local_icon = None
        try:
            accounts = self.host.call("accounts.list", {"limit": 100})["items"]
            if not accounts:
                return
            selected = next((item for item in accounts if item["is_current"]), accounts[0])
            if self._connector._player_name == "Player":
                self._connector._player_name = selected["alias"]
            part = self.host.call("accounts.skin", {"account_ref": selected["account_ref"]})
            chunks = [base64.b64decode(part["content"], validate=True)]
            while part["next_offset"] is not None:
                part = self.host.call(
                    "accounts.skin",
                    {
                        "account_ref": selected["account_ref"],
                        "offset": part["next_offset"],
                        "revision": part["revision"],
                    },
                )
                chunks.append(base64.b64decode(part["content"], validate=True))
            content = b"".join(chunks)
            self._local_icon = base64.b64encode(content).decode() if content else None
        except HostApiError:
            # 账户和皮肤属于可选展示信息，不改变基础联机是否可用。
            return

    def player_icon(self, payload: dict) -> dict[str, JsonValue]:
        """
        对当前玩家 PNG 按修订分块交付，不在状态轮询中携带大图片。
        """
        content = self._icons.get(payload.get("machine_id"))
        if content is None or payload.get("revision") != hashlib.sha256(content).hexdigest():
            raise ValueError("玩家头像已变化")
        offset = payload.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(content):
            raise ValueError("头像分块位置无效")
        block = content[offset : offset + 8192]
        following = offset + len(block)
        return {
            "result": {
                "content": base64.b64encode(block).decode(),
                "size": len(content),
                "next_offset": following if following < len(content) else None,
            }
        }

    def host_port(self, payload: dict) -> dict[str, JsonValue]:
        """
        提交端口建房，拒绝布尔或超出 TCP 范围的端口。
        """
        port = payload.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("端口应在 1 至 65535 之间")
        return self._submit(lambda: self._connector.host_port(port), prepare=True)

    def host_instance(self, payload: dict) -> dict[str, JsonValue]:
        """
        先验证本运行获准实例，再提交端口检测与建房。
        """
        item = self.host.call("instances.get", {"instance_ref": payload.get("instance_ref")})
        return self._submit(lambda: self._connector.host_instance("", str(item.get("version_id", ""))), prepare=True)

    def join(self, payload: dict) -> dict[str, JsonValue]:
        """
        提交房间加入，协议格式由联机服务验证。
        """
        code = payload.get("code")
        if not isinstance(code, str) or len(code) > 256:
            raise ValueError("房间码格式无效")
        return self._submit(lambda: self._connector.join(code.strip()), prepare=True)

    def leave(self, payload=None) -> dict[str, JsonValue]:
        """
        提交实际离房操作，等待任务终态后才表示资源已停止。
        """
        return self._submit(self._connector.leave)

    def kick(self, payload: dict) -> dict[str, JsonValue]:
        """
        只允许本机房主移除当前玩家。
        """
        machine_id = payload.get("machine_id")
        if not isinstance(machine_id, str) or not 1 <= len(machine_id) <= 128:
            raise ValueError("玩家标识无效")
        return self._submit(lambda: self._connector.kick(machine_id))

    def nat_type(self, payload=None) -> dict[str, JsonValue]:
        """
        在有界后台操作中探测 NAT，不阻塞页面命令。
        """
        return self._submit(self._connector.get_nat_type)

    def detect_ports(self, payload=None) -> dict[str, JsonValue]:
        """
        获取本机候选 Java 服务端口。
        """
        return self._submit(self._connector.detect_ports)

    def search_mc_port(self, payload: dict) -> dict[str, JsonValue]:
        """
        仅检查有界合法端口集合是否提供 Minecraft 服务。
        """
        ports = payload.get("ports")
        if (
            not isinstance(ports, list)
            or len(ports) > 128
            or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
        ):
            raise ValueError("候选端口列表无效")
        return self._submit(lambda: self._connector.search_mc_port(ports))

    def nodes_get(self, payload=None) -> dict[str, JsonValue]:
        """
        读取本插件节点设置。
        """
        return {"result": self._node_settings().model_dump()}

    def nodes_set(self, payload: dict) -> dict[str, JsonValue]:
        """
        只更新自身节点策略，不迁移已存在的房间。
        """
        settings = ConnectorNodeConfig.model_validate(payload)
        view = self.host.call("settings.get", {"keys": ["node_mode", "nodes"]})
        self.host.call(
            "settings.set",
            {
                "expected_revision": view["revision"],
                "values": {"node_mode": settings.mode, "nodes": "\n".join(settings.nodes)},
            },
        )
        return {"result": settings.model_dump()}

    def operation(self, payload: dict) -> dict[str, JsonValue]:
        """
        查询本运行提交的操作；结束前不将取消或超时报告为完成。
        """
        with self._lock:
            future = self._tasks.get(payload.get("operation_ref"))
        if future is None:
            raise ValueError("操作不属于当前插件运行")
        if not future.done():
            return {"result": {"state": "running"}}
        try:
            return {"result": {"state": "succeeded", "value": future.result()}}
        except Exception:
            logging.getLogger(__name__).warning("联机操作未完成，请检查网络与节点设置")
            return {"result": {"state": "failed", "error": "联机操作失败，请检查房间状态、网络和节点设置后重试"}}

    def instances(self, payload=None) -> dict[str, JsonValue]:
        """
        只枚举本运行已获准的实例，不读取其他目录。
        """
        try:
            roots = self.host.call("resources.list", {"capability": "instances.read", "limit": 32})
        except HostApiError as exc:
            if exc.error_code == "PLUGIN_PERMISSION_DENIED":
                return {"result": {"items": [], "access_denied": True}}
            raise
        items = []
        for root in roots.get("items", []):
            page = self.host.call("instances.scan", {"root_ref": root["root_ref"], "limit": 32})
            items.extend(page.get("items", []))
        return {"result": {"items": items[:32]}}

    def on_disable(self) -> None:
        """
        拒绝新操作，关闭实际房间；监管负责回收未能在期限内退出的 Worker。
        """
        with self._lock:
            self._stopped = True
        self._connector.close()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def on_unload(self) -> None:
        """
        加载失败或关闭路径也释放网络资源。
        """
        self.on_disable()
