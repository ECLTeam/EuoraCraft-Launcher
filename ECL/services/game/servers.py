# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：服务器列表服务：servers.dat 读写与在线状态刷新。
#
# 公开接口：
#   - class ServerCoordinator — 读写 Minecraft 服务器列表并管理 ECL 收藏和短时状态缓存。
#       - list_servers(game_path, version_id, version_isolation=…) -> list[dict[str, Any]] — 返回 Minecraft 原始顺序和 ECL 收藏状态，未知 NBT 字段保持在原文档中。
#       - upsert_server(game_path, version_id, server_id, name, address, favorite=…, version_isolation=…) -> dict[str, Any] — 新增或修改服务器，仅更新已知字段并保留同一 Compound 的未知字段。
#       - delete_server(game_path, version_id, server_id, version_isolation=…) -> None
#       - reorder_servers(game_path, version_id, server_ids, version_isolation=…) -> list[dict[str, Any]]
#       - set_server_favorite(game_path, version_id, address, favorite) -> None
#       - refresh_server_statuses(addresses, timeout=…) -> list[dict[str, Any]] — 以受限并发查询 Java 服务器状态并使用三十秒短缓存。
# ============================================================

from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from mcstatus import JavaServer

from ECL.game import Compound, File, List, String, load
from ECL.utils import atomic_write_text

from .base import GameServiceError


class ServerCoordinator:
    """
    读写 Minecraft 服务器列表并管理 ECL 收藏和短时状态缓存。
    """

    _STATUS_TTL = 30.0

    def _servers_path(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> Path:
        return self.resolve_instance(game_path, version_id, version_isolation).data_path / "servers.dat"

    def _server_meta_path(self, game_path: Any, version_id: Any) -> Path:
        return self.resolve_instance(game_path, version_id).instance_path / ".ecl" / "servers.json"

    def _read_server_meta(self, game_path: Any, version_id: Any) -> dict[str, Any]:
        path = self._server_meta_path(game_path, version_id)
        if not path.is_file():
            return {"schemaVersion": 1, "favorites": []}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"schemaVersion": 1, "favorites": []}
        except (OSError, UnicodeDecodeError, ValueError):
            return {"schemaVersion": 1, "favorites": []}

    def _write_server_meta(self, game_path: Any, version_id: Any, value: dict[str, Any]) -> None:
        path = self._server_meta_path(game_path, version_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def _load_servers(path: Path) -> tuple[Any, Any]:
        if not path.is_file():
            document = File({"servers": List[Compound]()}, gzipped=True)
            return document, document["servers"]
        try:
            document = load(path)
            servers = document.get("servers")
            if servers is None:
                servers = List[Compound]()
                document["servers"] = servers
            return document, servers
        except Exception as exc:
            raise GameServiceError(f"读取 servers.dat 失败：{exc}", "SERVERS_NBT_INVALID") from exc

    @staticmethod
    def _save_servers(document: Any, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.ecl-tmp")
        try:
            document.save(temp, gzipped=True)
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)

    def list_servers(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> list[dict[str, Any]]:
        """
        返回 Minecraft 原始顺序和 ECL 收藏状态，未知 NBT 字段保持在原文档中。
        """
        path = self._servers_path(game_path, version_id, version_isolation)
        _, servers = self._load_servers(path)
        favorites = {
            str(item).casefold() for item in self._read_server_meta(game_path, version_id).get("favorites") or []
        }
        result: list[dict[str, Any]] = []
        for index, server in enumerate(servers):
            address = str(server.get("ip", ""))
            result.append(
                {
                    "id": str(index),
                    "name": str(server.get("name", "服务器")),
                    "address": address,
                    "icon": str(server.get("icon", "")) or None,
                    "acceptTextures": int(server.get("acceptTextures", 0)) if "acceptTextures" in server else None,
                    "favorite": address.casefold() in favorites,
                    "order": index,
                }
            )
        return result

    def upsert_server(
        self,
        game_path: Any,
        version_id: Any,
        server_id: str | None,
        name: str,
        address: str,
        favorite: bool = False,
        version_isolation: Any = False,
    ) -> dict[str, Any]:
        """
        新增或修改服务器，仅更新已知字段并保留同一 Compound 的未知字段。
        """
        name = str(name).strip()
        address = str(address).strip()
        if not name or not address or any(char in address for char in ("\0", "\r", "\n")):
            raise GameServiceError("服务器名称或地址无效", "INVALID_SERVER")
        path = self._servers_path(game_path, version_id, version_isolation)
        document, servers = self._load_servers(path)
        if server_id is None:
            server = Compound()
            servers.append(server)
            index = len(servers) - 1
        else:
            try:
                index = int(server_id)
                server = servers[index]
            except (ValueError, IndexError) as exc:
                raise GameServiceError("服务器不存在", "SERVER_NOT_FOUND") from exc
        old_address = str(server.get("ip", ""))
        server["name"] = String(name)
        server["ip"] = String(address)
        self._save_servers(document, path)
        meta = self._read_server_meta(game_path, version_id)
        favorites = {str(item).casefold(): str(item) for item in meta.get("favorites") or []}
        favorites.pop(old_address.casefold(), None)
        if favorite:
            favorites[address.casefold()] = address
        meta["favorites"] = list(favorites.values())
        self._write_server_meta(game_path, version_id, meta)
        return self.list_servers(game_path, version_id, version_isolation)[index]

    def delete_server(self, game_path: Any, version_id: Any, server_id: Any, version_isolation: Any = False) -> None:
        path = self._servers_path(game_path, version_id, version_isolation)
        document, servers = self._load_servers(path)
        try:
            index = int(server_id)
            address = str(servers[index].get("ip", ""))
            del servers[index]
        except (ValueError, IndexError) as exc:
            raise GameServiceError("服务器不存在", "SERVER_NOT_FOUND") from exc
        self._save_servers(document, path)
        meta = self._read_server_meta(game_path, version_id)
        meta["favorites"] = [item for item in meta.get("favorites") or [] if str(item).casefold() != address.casefold()]
        self._write_server_meta(game_path, version_id, meta)

    def reorder_servers(
        self, game_path: Any, version_id: Any, server_ids: list[str], version_isolation: Any = False
    ) -> list[dict[str, Any]]:
        path = self._servers_path(game_path, version_id, version_isolation)
        document, servers = self._load_servers(path)
        try:
            indices = [int(value) for value in server_ids]
        except ValueError as exc:
            raise GameServiceError("服务器顺序无效", "INVALID_SERVER_ORDER") from exc
        if sorted(indices) != list(range(len(servers))):
            raise GameServiceError("服务器顺序必须包含全部服务器", "INVALID_SERVER_ORDER")
        reordered = List[Compound]([servers[index] for index in indices])
        document["servers"] = reordered
        self._save_servers(document, path)
        return self.list_servers(game_path, version_id, version_isolation)

    def set_server_favorite(self, game_path: Any, version_id: Any, address: str, favorite: bool) -> None:
        meta = self._read_server_meta(game_path, version_id)
        values = {str(item).casefold(): str(item) for item in meta.get("favorites") or []}
        if favorite:
            values[address.casefold()] = address
        else:
            values.pop(address.casefold(), None)
        meta["favorites"] = list(values.values())
        self._write_server_meta(game_path, version_id, meta)

    def _query_server_status(self, address: str, timeout: float) -> dict[str, Any]:
        with self._server_status_lock:
            cached = self._server_status_cache.get(address.casefold())
            if cached and time.monotonic() - cached[0] < self._STATUS_TTL:
                return dict(cached[1])
        try:
            status = JavaServer.lookup(address, timeout=timeout).status(tries=1)
            icon = status.icon
            if icon and icon.startswith("data:image"):
                try:
                    icon = base64.b64encode(base64.b64decode(icon.split(",", 1)[1])).decode("ascii")
                except Exception:
                    icon = None
            result = {
                "address": address,
                "online": True,
                "latency": round(float(status.latency), 1),
                "playersOnline": int(status.players.online),
                "playersMax": int(status.players.max),
                "version": status.version.name,
                "protocol": status.version.protocol,
                "motd": status.description.to_plain()
                if hasattr(status.description, "to_plain")
                else str(status.description),
                "icon": icon,
            }
        except Exception as exc:
            result = {"address": address, "online": False, "error": str(exc)}
        with self._server_status_lock:
            self._server_status_cache[address.casefold()] = (time.monotonic(), result)
        return result

    def refresh_server_statuses(self, addresses: list[str], timeout: float = 3.0) -> list[dict[str, Any]]:
        """
        以受限并发查询 Java 服务器状态并使用三十秒短缓存。
        """
        normalized = list(dict.fromkeys(address.strip() for address in addresses if address.strip()))[:64]
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(normalized)))) as executor:
            futures = {executor.submit(self._query_server_status, address, timeout): address for address in normalized}
            for future in as_completed(futures):
                results.append(future.result())
        order = {address: index for index, address in enumerate(normalized)}
        return sorted(results, key=lambda item: order.get(item["address"], 0))
