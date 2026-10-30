# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将固定来源的版本和加载器查询投影为当前插件运行的授权分页。
#
# 公开接口：
#   - class RuntimeGameCatalog — 只交付公开版本字段的有界目录查询。
# ============================================================

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.runtime.game_models import GameCatalogRequest, GameLoadersRequest
from ECL.plugins.runtime.resources import ResourcePages
from ECL.utils.errors import GameServiceError

if TYPE_CHECKING:
    from ECL.services.game.plugin_games import PluginGameCoordinator


class RuntimeGameCatalog:
    """
    每次查询和交付均复核原始目录许可，游标不跨运行或查询复用。
    """

    def __init__(self, session: PluginGrantSession, game: PluginGameCoordinator) -> None:
        """
        绑定宿主已创建的游戏服务，不从请求构造客户端。

        :param session: 当前插件运行的授权会话
        :param game: 应用唯一游戏服务
        """
        self._session = session
        self._game = game
        self._pages = ResourcePages()

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        校验查询后返回有限分页，不把底层异常文本交付给插件。

        :param method: game.catalog 或 game.loaders
        :param params: 不可信请求对象
        :param deadline: RPC 截止
        :return: 公开字段分页
        :raises ProtocolError: 方法、许可、查询或截止不满足
        """
        if method == "game.catalog":
            query = GameCatalogRequest.model_validate(params)
        elif method == "game.loaders":
            query = GameLoadersRequest.model_validate(params)
        else:
            raise ProtocolError("游戏查询尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")
        lease = self._session.authorize("game.catalog.read")
        self._session.check(lease)
        deadline = min(deadline, monotonic() + 30)

        def load() -> tuple[dict[str, JsonValue], ...]:
            if isinstance(query, GameCatalogRequest):
                return tuple(
                    {"version_id": item.version_id, "type": item.version_type, "release_time": item.release_time}
                    for item in self._game.plugin_catalog(query.version_type, deadline)
                )
            return tuple(
                {"loader_version": version, "is_latest": index == 0}
                for index, version in enumerate(self._game.plugin_loaders(query.game_version, query.loader, deadline))
            )

        try:
            result = self._pages.page(
                method + query.model_dump_json(exclude={"cursor"}),
                str(lease.grant_epoch),
                query.cursor,
                query.limit,
                load,
            )
        except GameServiceError as exc:
            raise ProtocolError("游戏版本查询未能完成", exc.error_code) from exc
        self._session.check(lease)
        if monotonic() >= deadline:
            raise ProtocolError("游戏查询超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")
        return result

    def close(self) -> None:
        """
        释放当前运行的分页，不影响宿主目录缓存。
        """
        self._pages.clear()
