# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：投影公开偏好与明确选定账户，拒绝透传配置、凭据和持久账户标识。
#
# 公开接口：
#   - class RuntimePublicQueries — 当前 Worker 的有界公开查询及账户引用。
# ============================================================

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from threading import RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import Field, StrictInt, ValidationError

from ECL.plugins.account_resources import PluginAccountResourceCatalog
from ECL.plugins.api.contracts import JsonValue, ProtocolError
from ECL.plugins.run_store import PluginRunSession
from ECL.plugins.runtime.models import RequestModel, ResourceListRequest
from ECL.plugins.runtime.pagination import ResourcePages


class _AccountQuery(RequestModel):
    account_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class _AccountList(RequestModel):
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class _AccountSkin(_AccountQuery):
    """
    绑定本运行账户与固定皮肤快照的分块读取。
    """

    offset: StrictInt = Field(default=0, ge=0, le=512 * 1024)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class _PublicConfig(RequestModel):
    locale: str = Field(min_length=1, max_length=32)
    theme_id: str = Field(min_length=1, max_length=64)
    theme_mode: Literal["system", "light", "dark"]
    accent_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    default_memory_mb: StrictInt = Field(ge=256, le=1048576)


class RuntimePublicQueries:
    """
    以当前领域快照和原始租约交付白名单 DTO，不承担认证或配置写入。
    """

    def __init__(
        self,
        session: PluginRunSession,
        catalog: PluginAccountResourceCatalog | None,
        refresh: Callable[[], int],
        config: Callable[[], JsonValue] | None,
        skin: Callable[[str], bytes | None] | None = None,
    ) -> None:
        """
        绑定应用现有查询来源，不创建账户服务或配置副本。

        :param session: 当前 Worker 授权会话
        :param catalog: 宿主维护的账户授权身份
        :param refresh: 刷新账户身份并更新可用授权范围
        :param config: 应用当前配置的防御性快照，仅在宿主内读取
        """
        self._session = session
        self._catalog = catalog
        self._refresh = refresh
        self._config = config
        self._skin = skin
        self._skins: dict[str, bytes] = {}
        self._lock = RLock()
        self._epoch = -1
        self._refs: dict[str, str] = {}
        self._pages = ResourcePages()

    def account_refs(self) -> list[str]:
        """
        为当前全部有效账户生成本运行引用，不暴露宿主持久身份。

        :return: 当前有效账户的运行引用
        """
        self._refresh()
        lease = self._session.lease()
        with self._lock:
            if lease.run_epoch != self._epoch:
                self._refs.clear()
                self._skins.clear()
                self._epoch = lease.run_epoch
            for resource_id, account in self._catalog.candidates():
                if account is None:
                    continue
                if resource_id not in self._refs.values():
                    self._refs[uuid4().hex] = resource_id
            return list(self._refs)

    def _configuration(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        RequestModel.model_validate(params)
        lease = self._session.lease()
        if self._config is None:
            raise ProtocolError("当前宿主未提供公开配置", "PLUGIN_API_METHOD_NOT_FOUND")
        config = self._config()
        if not isinstance(config, dict):
            raise ProtocolError("公开配置无法读取", "PLUGIN_PUBLIC_CONFIG_INVALID")
        ui, game = config.get("ui"), config.get("game")
        if not isinstance(ui, dict) or not isinstance(game, dict) or not isinstance(ui.get("theme"), dict):
            raise ProtocolError("公开配置结构无效", "PLUGIN_PUBLIC_CONFIG_INVALID")
        theme = ui["theme"]
        try:
            result = _PublicConfig.model_validate(
                {
                    "locale": ui.get("locale"),
                    "theme_id": theme.get("theme_id"),
                    "theme_mode": theme.get("mode"),
                    "accent_color": theme.get("primary_color"),
                    "default_memory_mb": game.get("memory_size"),
                }
            ).model_dump(mode="json")
        except ValidationError as error:
            raise ProtocolError("公开配置字段无效", "PLUGIN_PUBLIC_CONFIG_INVALID") from error
        self._session.check(lease)
        if monotonic() >= deadline:
            raise ProtocolError("公开配置查询已超时", "PLUGIN_DEADLINE_EXCEEDED")
        return result

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        查询公开偏好或选定账户，返回前再次验证原始准入和账户身份。

        :param method: 明确接线的方法名称
        :param params: 严格校验的不可信请求
        :param deadline: 当前 RPC 的原始截止时间
        :return: 无路径、凭据和真实账户标识的白名单结果
        :raises ProtocolError: 方法、选择、引用或原授权失效时抛出
        """
        if method == "config.public":
            return self._configuration(params, deadline)
        if method not in {"accounts.list", "accounts.get", "accounts.skin", "resources.list"} or self._catalog is None:
            raise ProtocolError("账户查询尚未提供", "PLUGIN_API_METHOD_NOT_FOUND")
        query = None
        if method in {"accounts.get", "accounts.skin"}:
            requested = (
                _AccountSkin.model_validate(params)
                if method == "accounts.skin"
                else _AccountQuery.model_validate(params)
            ).account_ref
        else:
            query = (
                ResourceListRequest.model_validate(params)
                if method == "resources.list"
                else _AccountList.model_validate(params)
            )
            if isinstance(query, ResourceListRequest) and query.capability != "accounts.read":
                raise ProtocolError("账户资源能力无效", "PLUGIN_SCHEMA_INVALID")
            requested = None
        references = self.account_refs()
        lease = self._session.lease()

        def project(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
            reference = str(item["account_ref"])
            with self._lock:
                resource_id = self._refs.get(reference)
            if resource_id is None:
                raise ProtocolError("账户引用不属于当前运行", "PLUGIN_RESOURCE_UNAVAILABLE")
            account = self._catalog.resolve(resource_id)
            return {
                "account_ref": reference,
                "alias": account.alias,
                "uuid": account.uuid,
                "type": account.kind,
                "is_current": account.is_current,
            }

        if requested is not None:
            result = project({"account_ref": requested})
            if method == "accounts.skin":
                result = self._read_skin(requested, _AccountSkin.model_validate(params))
        else:
            assert query is not None
            result = self._pages.page(
                f"{method}:{query.limit}",
                str(lease.run_epoch),
                query.cursor,
                query.limit,
                lambda: tuple({"account_ref": ref} for ref in references),
                project=project,
            )
        self._refresh()
        self._session.check(lease)
        if monotonic() >= deadline:
            raise ProtocolError("账户查询已超时", "PLUGIN_DEADLINE_EXCEEDED")
        return result

    def _read_skin(self, reference: str, query: _AccountSkin) -> dict[str, JsonValue]:
        """
        只有已通过账户引用检查的调用可获取皮肤，不交付其来源地址。
        """
        if self._skin is None or self._catalog is None:
            raise ProtocolError("账户皮肤查询不可用", "PLUGIN_API_METHOD_NOT_FOUND")
        with self._lock:
            if query.offset == 0 and query.revision is None:
                if len(self._skins) >= 16:
                    self._skins.clear()
                account = self._catalog.resolve(self._refs[reference])
                self._skins[reference] = self._skin(account.account_id) or b""
            content = self._skins.get(reference)
            if content is None or len(content) > 512 * 1024:
                raise ProtocolError("皮肤快照已失效", "PLUGIN_RESOURCE_CHANGED")
            revision = hashlib.sha256(content).hexdigest()
            if (query.offset and query.revision is None) or (query.revision and query.revision != revision):
                raise ProtocolError("皮肤快照已变化", "PLUGIN_REVISION_CONFLICT")
            if query.offset > len(content):
                raise ProtocolError("皮肤分块位置无效", "PLUGIN_SCHEMA_INVALID")
            block = content[query.offset : query.offset + 8192]
            following = query.offset + len(block)
            return {
                "content": base64.b64encode(block).decode("ascii"),
                "revision": revision,
                "size": len(content),
                "next_offset": following if following < len(content) else None,
            }
