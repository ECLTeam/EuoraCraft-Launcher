from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from ECL.plugins import (
    ConnectorProtocolRequest,
    ConnectorProtocolResponse,
    ConnectorSessionContext,
    ExternalInstanceMetadata,
    InstanceCompatibilityContext,
    Plugin,
)


def _read_text(path: Path) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return path.read_text(encoding="utf-8")


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(path).expanduser().resolve(strict=False))))


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


class QomicExCompatibilityPlugin(Plugin):
    """
    读取 QomicEX ``instances.json``，不修改第三方启动器的任何文件。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cache: dict[
            str,
            tuple[tuple[int, int], list[dict[str, Any]] | None, str | None],
        ] = {}
        self._connector_lock = RLock()
        self._player_icons: dict[str, str] = {}
        self._room_game_info: dict[str, Any] | None = None
        self._room_mods: list[dict[str, str]] = []
        self._last_player_count = -1

    def on_enable(self) -> None:
        """
        启用插件并注册 QomicEX 实例兼容读取器。
        """
        super().on_enable()
        self.register_instance_compatibility(
            "qomicex",
            "QomicEX",
            self.read_instance,
            self.watch_paths,
        )
        self.register_connector_extension(
            "qomicex",
            {
                "qml:game_info": self._protocol_game_info,
                "qml:player_icons": self._protocol_player_icons,
                "qml:player_leave": self._protocol_player_leave,
                "qml:game_mods": self._protocol_game_mods,
            },
            on_guest_joined=self._on_guest_joined,
            enrich_status=self._enrich_connector_status,
            before_leave=self._before_connector_leave,
            on_reset=self._reset_connector_state,
        )
        self.register_command(
            "resolve",
            self._resolve_instance_index,
            "解析当前生效的 QomicEX 实例索引路径",
        )

    def on_frontend_ready(self) -> None:
        """
        前端就绪后向启动器设置页注入实例兼容设置卡片。
        """
        super().on_frontend_ready()
        self.register_vue_slot_file(
            "plugin-slot-settings-launcher-section-after",
            "QomicExInstanceCompatSettings",
            "instance_compat_settings.vue",
        )

    def _resolve_instance_index(self, instances_path: str | None = None) -> dict[str, Any]:
        """
        解析当前生效的 QomicEX 实例索引路径，供前端设置卡片展示。
        """
        manual = str(instances_path or "").strip() or None
        options: Mapping[str, Any] = {"qomicex": {"instances_path": manual}}
        resolved = self.resolve_data_path(options)
        return {
            "path": str(resolved) if resolved is not None else None,
            "valid": bool(resolved is not None and resolved.is_file()),
            "manual": manual,
        }

    @staticmethod
    def _default_game_info() -> dict[str, Any]:
        return {"gameVersion": "", "loader": None, "loaderVersion": None}

    def _protocol_game_info(self, request: ConnectorProtocolRequest) -> ConnectorProtocolResponse:
        """实现 QomicEX ``qml:game_info`` 房主版本信息协议。"""
        game_info = dict(request.game_info or self._default_game_info())
        return ConnectorProtocolResponse.json(game_info)

    def _protocol_player_icons(self, request: ConnectorProtocolRequest) -> ConnectorProtocolResponse:
        """实现 QomicEX ``qml:player_icons`` 头像交换协议。"""
        payload = request.json({})
        if isinstance(payload, Mapping):
            machine_id = str(payload.get("machineId") or "").strip()
            icon_base64 = str(payload.get("iconBase64") or "").strip()
            if machine_id and icon_base64:
                with self._connector_lock:
                    self._player_icons[machine_id] = icon_base64
        with self._connector_lock:
            icons = dict(self._player_icons)
        return ConnectorProtocolResponse.json({"icons": icons})

    async def _protocol_player_leave(self, request: ConnectorProtocolRequest) -> ConnectorProtocolResponse:
        """实现 QomicEX ``qml:player_leave`` 优雅退出协议。"""
        payload = request.json({})
        machine_id = str(payload.get("machineId") or "").strip() if isinstance(payload, Mapping) else ""
        if machine_id:
            with self._connector_lock:
                self._player_icons.pop(machine_id, None)
            await request.remove_player(machine_id)
        return ConnectorProtocolResponse.json(True)

    def _protocol_game_mods(self, request: ConnectorProtocolRequest) -> ConnectorProtocolResponse:
        """实现 QomicEX ``qml:game_mods`` 房主模组清单协议。"""
        with self._connector_lock:
            mods = [dict(item) for item in self._room_mods]
        return ConnectorProtocolResponse.json({"mods": mods})

    @staticmethod
    def _try_request_json(context: ConnectorSessionContext, protocol: str, payload: Any = None) -> Any:
        try:
            if payload is None:
                return context.request_json(protocol)
            return context.request_json(protocol, payload)
        except (RuntimeError, UnicodeDecodeError, ValueError, OSError, EOFError):
            # 房主可能刚完成握手就关闭连接；这是可预期的瞬时失败，
            # 返回空结果让调用方跳过本次补充，不把网络异常抛给扩展注册表。
            return None

    def _refresh_remote_game_info(self, context: ConnectorSessionContext) -> None:
        result = self._try_request_json(context, "qml:game_info")
        if isinstance(result, Mapping):
            with self._connector_lock:
                self._room_game_info = {
                    "gameVersion": str(result.get("gameVersion") or ""),
                    "loader": result.get("loader"),
                    "loaderVersion": result.get("loaderVersion"),
                }

    def _refresh_remote_icons(self, context: ConnectorSessionContext) -> None:
        result = self._try_request_json(
            context,
            "qml:player_icons",
            {"machineId": context.machine_id or "", "iconBase64": ""},
        )
        icons = result.get("icons") if isinstance(result, Mapping) else None
        if isinstance(icons, Mapping):
            with self._connector_lock:
                self._player_icons.update(
                    {str(machine_id): str(icon) for machine_id, icon in icons.items() if machine_id and icon}
                )

    def _on_guest_joined(self, context: ConnectorSessionContext) -> None:
        """加入 QomicEX 房间后交换其全部扩展数据。"""
        self._refresh_remote_game_info(context)
        self._refresh_remote_icons(context)
        result = self._try_request_json(context, "qml:game_mods")
        mods = result.get("mods") if isinstance(result, Mapping) else None
        if isinstance(mods, list):
            with self._connector_lock:
                self._room_mods = [dict(item) for item in mods if isinstance(item, Mapping)]
        self._publish_local_icon(context)

    def _publish_local_icon(self, context: ConnectorSessionContext) -> None:
        """
        把本机玩家的完整皮肤作为头像发布给房主，并写入本机缓存。
        """
        machine_id = (context.machine_id or "").strip()
        if not machine_id:
            return
        icon = context.local_player_icon()
        if not icon:
            return
        self._try_request_json(
            context,
            "qml:player_icons",
            {"machineId": machine_id, "iconBase64": icon},
        )
        with self._connector_lock:
            self._player_icons[machine_id] = icon

    def _ensure_local_player_icon(self, context: ConnectorSessionContext, players: list[dict[str, Any]]) -> None:
        """
        确保本机玩家头像在 `_player_icons` 中，供本人与房客的状态合并使用。
        """
        local_machine_id: str | None = None
        if context.mode == "guest":
            local_machine_id = (context.machine_id or "").strip() or None
        elif context.mode == "host" and isinstance(players, list):
            for player in players:
                if isinstance(player, dict) and str(player.get("kind") or "").upper() == "HOST":
                    local_machine_id = str(player.get("machineId") or "").strip() or None
                    break
        if not local_machine_id:
            return
        icon = context.local_player_icon()
        if not icon:
            return
        with self._connector_lock:
            self._player_icons.setdefault(local_machine_id, icon)

    def _enrich_connector_status(
        self, context: ConnectorSessionContext, status: dict[str, Any]
    ) -> Mapping[str, Any] | None:
        """把 QomicEX 版本信息和头像合并进宿主联机状态。"""
        players = status.get("players")
        player_count = len(players) if isinstance(players, list) else 0
        if isinstance(players, list):
            self._ensure_local_player_icon(context, players)
        if context.mode == "guest":
            self._refresh_remote_game_info(context)
            if player_count != self._last_player_count:
                self._refresh_remote_icons(context)
        self._last_player_count = player_count

        with self._connector_lock:
            icons = dict(self._player_icons)
            game_info = dict(self._room_game_info) if self._room_game_info is not None else None
        if isinstance(players, list):
            for player in players:
                if not isinstance(player, dict):
                    continue
                icon = icons.get(str(player.get("machineId") or ""))
                if icon:
                    player["iconBase64"] = icon
        if context.mode == "guest" and game_info is not None:
            return {"gameInfo": game_info}
        return None

    def _before_connector_leave(self, context: ConnectorSessionContext) -> None:
        """房客退出前发送 QomicEX 的优雅离房通知。"""
        if context.mode == "guest" and context.machine_id:
            self._try_request_json(context, "qml:player_leave", {"machineId": context.machine_id})

    def _reset_connector_state(self, context: ConnectorSessionContext) -> None:
        """清理只属于当前联机会话的 QomicEX 扩展缓存。"""
        with self._connector_lock:
            self._player_icons.clear()
            self._room_game_info = None
            self._room_mods = []
            self._last_player_count = -1

    def resolve_data_path(self, options: Mapping[str, Any] | None = None) -> Path | None:
        """
        按手动配置、环境变量、引导文件和默认目录顺序查找实例索引。

        :param options: 宿主传入的全部兼容来源配置
        :return: 首个存在的 ``instances.json`` 路径
        """
        candidates: list[Path] = []
        source_options = (options or {}).get("qomicex")
        if isinstance(source_options, Mapping):
            manual_path = source_options.get("instances_path")
            if manual_path and str(manual_path).strip():
                candidates.append(Path(str(manual_path).strip()).expanduser())

        env_home = os.environ.get("QOMICEX_HOME", "").strip()
        if env_home:
            candidates.append(Path(env_home).expanduser())

        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            base = Path(local_app_data) / "qomicex-launcher"
            bootstrap = base / ".qomicex-bootstrap"
            bootstrap_directory = self._read_bootstrap_directory(bootstrap)
            if bootstrap_directory is not None:
                candidates.append(bootstrap_directory)
            candidates.extend((base, base / "data"))

        for candidate in candidates:
            file_path = candidate if candidate.name.casefold() == "instances.json" else candidate / "instances.json"
            if file_path.is_file():
                return file_path.resolve(strict=False)
        return None

    @staticmethod
    def _read_bootstrap_directory(bootstrap: Path) -> Path | None:
        """
        解析 QomicEX 引导文件中记录的数据目录。

        :param bootstrap: ``.qomicex-bootstrap`` 文件路径
        """
        if not bootstrap.is_file():
            return None
        try:
            bootstrap_value = _read_text(bootstrap).strip()
            try:
                parsed = json.loads(bootstrap_value)
            except ValueError:
                parsed = bootstrap_value
        except (OSError, UnicodeDecodeError):
            return None
        if isinstance(parsed, dict):
            parsed = parsed.get("dataDir") or parsed.get("path") or parsed.get("home")
        if isinstance(parsed, str) and parsed.strip():
            return Path(parsed.strip()).expanduser()
        return None

    def watch_paths(self, options: Mapping[str, Any]) -> list[Path]:
        """
        返回需要参与版本扫描缓存失效判断的 QomicEX 索引文件。

        :param options: 宿主传入的全部兼容来源配置
        """
        data_path = self.resolve_data_path(options)
        return [data_path] if data_path is not None else []

    @staticmethod
    def _instances(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("instances", "data", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _match_instances(
        instances: list[dict[str, Any]], context: InstanceCompatibilityContext
    ) -> list[dict[str, Any]]:
        exact = [
            item
            for item in instances
            if item.get("gameDir") and _path_key(item["gameDir"]) == _path_key(context.instance_path)
        ]
        if exact:
            return exact
        version_key = context.version_id.casefold()
        vanilla_key = context.vanilla_name.casefold()
        loader_key = context.primary_loader.casefold()
        return [
            item
            for item in instances
            if str(item.get("name") or "").casefold() == version_key
            and str(item.get("gameVersion") or "").casefold() in {"", vanilla_key}
            and str(item.get("loader") or "vanilla").casefold() in {"", loader_key}
        ]

    def _load_instances(self, data_path: Path) -> tuple[list[dict[str, Any]] | None, int, str | None]:
        try:
            stat = data_path.stat()
        except OSError as exc:
            return None, 0, str(exc)
        signature = (stat.st_mtime_ns, stat.st_size)
        cache_key = _path_key(data_path)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1], stat.st_mtime_ns, cached[2]
        try:
            instances = self._instances(json.loads(_read_text(data_path)))
            error = None
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            instances = None
            error = str(exc)
            self.logger.warning("读取 QomicEX 实例索引失败 %s: %s", data_path, exc)
        self._cache[cache_key] = (signature, instances, error)
        return instances, stat.st_mtime_ns, error

    def read_instance(self, context: InstanceCompatibilityContext) -> ExternalInstanceMetadata | None:
        """
        读取与扫描上下文唯一匹配的 QomicEX 实例元数据。

        :param context: 当前 Minecraft 实例的只读扫描上下文
        """
        data_path = self.resolve_data_path(context.options)
        if data_path is None:
            return None
        instances, modified_ns, error = self._load_instances(data_path)
        if instances is None:
            return ExternalInstanceMetadata(
                source="qomicex",
                modified_ns=modified_ns,
                warnings=[f"QomicEX 配置读取失败: {error}"],
            )

        matches = self._match_instances(instances, context)
        if len(matches) != 1:
            if len(matches) > 1:
                self.logger.warning(
                    "QomicEX 实例匹配存在歧义: instance_path=%s, version_id=%s, matched_count=%d",
                    context.instance_path,
                    context.version_id,
                    len(matches),
                )
            return None

        item = matches[0]
        metadata = ExternalInstanceMetadata(source="qomicex", modified_ns=modified_ns)
        description = str(item.get("modpackSummary") or "").strip()
        if description:
            metadata.fields["description"] = description
        if isinstance(item.get("isHidden"), bool):
            metadata.fields["hidden"] = item["isHidden"]
        if isinstance(item.get("isDefault"), bool):
            metadata.fields["pinned"] = item["isDefault"]

        icon_data = item.get("iconData")
        icon_name = str(item.get("icon") or "").strip().casefold()
        if isinstance(icon_data, str) and icon_data.startswith("data:image/"):
            metadata.fields["icon"] = {"type": "data", "value": icon_data, "source": "qomicex"}
        elif icon_name:
            loader_icons = {"forge", "neoforge", "fabric", "quilt", "optifine"}
            metadata.fields["icon"] = {
                "type": "loader" if icon_name in loader_icons else "builtin",
                "value": icon_name,
                "source": "qomicex",
            }

        play_time_minutes = _non_negative_int(item.get("playTime"))
        if play_time_minutes is not None:
            metadata.stats["totalRunDurationSeconds"] = play_time_minutes * 60
        last_played = item.get("lastPlayed")
        if isinstance(last_played, str) and last_played.strip():
            metadata.stats["lastLaunchedAt"] = last_played.strip()
        return metadata
