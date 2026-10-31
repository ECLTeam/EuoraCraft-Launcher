# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：复用启动器资源平台查询，投影有界公开目录和下载信息，不授予网络或文件写入权限。
#
# 公开接口：
#   - class RuntimeResourceCatalog — 当前 Worker 的资源搜索、版本和文件信息查询。
# ============================================================

from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, StrictInt

from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.network_targets import NetworkOrigin
from ECL.plugins.runtime.game_models import GameInstallRequest
from ECL.plugins.runtime.models import RequestModel
from ECL.plugins.runtime.resources import ResourcePages
from ECL.utils.errors import GameServiceError

if TYPE_CHECKING:
    from ECL.services.game.plugin_games import PluginGameCoordinator


class _Search(RequestModel):
    source: Literal["modrinth", "curseforge"] = "modrinth"
    query: str = Field(default="", max_length=128)
    game_version: str = Field(default="", pattern=r"^[A-Za-z0-9_.+-]{0,64}$")
    loader: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,32}$")
    offset: StrictInt = Field(default=0, ge=0, le=1000)
    limit: StrictInt = Field(default=20, ge=1, le=50)


class _Project(RequestModel):
    source: Literal["modrinth", "curseforge"] = "modrinth"
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    game_version: str = Field(default="", pattern=r"^[A-Za-z0-9_.+-]{0,64}$")
    loader: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,32}$")
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=20, ge=1, le=50)


class _Download(RequestModel):
    source: Literal["modrinth", "curseforge"] = "modrinth"
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    version_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class _Version(RequestModel):
    source: Literal["modrinth", "curseforge"]
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    version_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    name: str = Field(max_length=256)
    version_number: str = Field(max_length=256)
    filename: str = Field(max_length=256)
    game_versions: list[Annotated[str, Field(max_length=64)]] = Field(max_length=128)
    loaders: list[Annotated[str, Field(max_length=32)]] = Field(max_length=64)


class RuntimeResourceCatalog:
    """
    查询固定平台和用户配置的模组源，下载仍需独立站点许可及私有文件任务。
    """

    def __init__(self, session: PluginGrantSession, game: PluginGameCoordinator) -> None:
        """
        绑定已有资源领域服务与当前运行预算，不创建另一份目录缓存。

        :param session: 当前运行的原始授权会话
        :param game: 应用唯一游戏与资源服务
        """
        self._session = session
        self._game = game
        self._lock = Lock()
        self._pages = ResourcePages()

    @staticmethod
    def _text(value: JsonValue, maximum: int = 256) -> str:
        if not isinstance(value, str) or len(value) > maximum:
            raise ProtocolError("资源平台返回的字段无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
        return value

    def _search(self, kind: str, query: _Search) -> dict[str, JsonValue]:
        resource_type = {"mods": "mod", "resourcepacks": "resourcepack", "shaders": "shaderpack"}[kind]
        raw = self._game.search_online_resources(
            query.query,
            query.game_version,
            query.loader,
            source=query.source,
            limit=query.limit,
            offset=query.offset,
            resource_type=resource_type,
        )
        hits = raw.get("items")
        total = raw.get("total")
        if not isinstance(hits, list) or len(hits) > query.limit or type(total) is not int or total < 0:
            raise ProtocolError("资源搜索结果无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
        items: list[JsonValue] = []
        for hit in hits:
            if not isinstance(hit, dict):
                raise ProtocolError("资源搜索条目无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
            identifier = hit.get("project_id") if query.source == "modrinth" else hit.get("id")
            if query.source == "curseforge" and type(identifier) is int:
                identifier = str(identifier)
            project = _Download.model_validate(
                {"source": query.source, "project_id": identifier, "version_id": "query"}
            )
            title = hit.get("title") if query.source == "modrinth" else hit.get("name")
            items.append(
                {
                    "source": query.source,
                    "project_id": project.project_id,
                    "title": self._text(_json_value(title)),
                    "description": self._text(_json_value(hit.get("description", "")), 4096),
                }
            )
        return {
            "items": items,
            "total": total,
            "next_offset": query.offset + len(items) if query.offset + len(items) < total else None,
        }

    def _versions(self, kind: str, query: _Project, epoch: int) -> dict[str, JsonValue]:
        def load() -> tuple[dict[str, JsonValue], ...]:
            raw = self._game.fetch_project_versions(query.source, query.project_id, query.game_version, query.loader)
            if len(raw) > 4096:
                raise ProtocolError("资源版本数量超过上限", "PLUGIN_RESOURCE_CATALOG_INVALID")
            items: list[dict[str, JsonValue]] = []
            for version in raw:
                value = _json_value(version)
                if not isinstance(value, dict):
                    raise ProtocolError("资源版本结构无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
                download = _Download.model_validate(
                    {"source": query.source, "project_id": query.project_id, "version_id": value.get("id")}
                )
                items.append(
                    _Version.model_validate(
                        {
                            "source": query.source,
                            "project_id": query.project_id,
                            "version_id": download.version_id,
                            "name": self._text(value.get("name")),
                            "version_number": self._text(value.get("versionNumber")),
                            "filename": self._text(value.get("filename")),
                            "game_versions": value.get("gameVersions"),
                            "loaders": value.get("loaders"),
                        }
                    ).model_dump(mode="json")
                )
            return tuple(items)

        return self._pages.page(
            kind + query.model_dump_json(exclude={"cursor"}), str(epoch), query.cursor, query.limit, load
        )

    def _download(self, query: _Download) -> dict[str, JsonValue]:
        selected = self._game.plugin_resource_download(query.source, query.project_id, query.version_id)
        GameInstallRequest.safe_version_name(selected.filename)
        origin, url = NetworkOrigin.from_url(selected.url)
        if selected.sha512 is not None and (
            len(selected.sha512) != 128 or any(c not in "0123456789abcdefABCDEF" for c in selected.sha512)
        ):
            raise ProtocolError("资源文件摘要无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
        return {
            "filename": selected.filename,
            "url": url,
            "origin": origin.reference,
            "sha512": selected.sha512,
            "size": selected.size,
        }

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        交付有限公开结果，原始许可失效或查询超时后拒绝交付。

        :param method: mods/resourcepacks/shaders 的 search、versions 或 download_info
        :param params: 无宿主路径和自定义请求地址的严格参数
        :param deadline: 原始查询截止
        :return: 不含提供方私有字段的公开目录
        :raises ProtocolError: 平台失败、结构无效、许可或截止失效时抛出
        """
        kind, _, action = method.partition(".")
        if kind not in {"mods", "resourcepacks", "shaders"}:
            raise ProtocolError("资源平台类型无效", "PLUGIN_API_METHOD_NOT_FOUND")
        lease = self._session.authorize("plugin.base")
        if monotonic() >= deadline:
            raise ProtocolError("资源查询已超时", "PLUGIN_DEADLINE_EXCEEDED")
        if not self._lock.acquire(blocking=False):
            raise ProtocolError("当前运行已有资源查询", "PLUGIN_RATE_LIMITED")
        try:
            if action == "search":
                result = self._search(kind, _Search.model_validate(params))
            elif action == "versions":
                result = self._versions(kind, _Project.model_validate(params), lease.grant_epoch)
            elif action == "download_info":
                result = self._download(_Download.model_validate(params))
            else:
                raise ProtocolError("资源目录方法未开放", "PLUGIN_API_METHOD_NOT_FOUND")
            self._session.check(lease)
            if monotonic() >= deadline:
                raise ProtocolError("资源查询已超时", "PLUGIN_DEADLINE_EXCEEDED")
            return result
        except GameServiceError as error:
            raise ProtocolError("资源平台查询未完成", error.error_code) from error
        finally:
            self._lock.release()
