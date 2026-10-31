# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将获准游戏根中的安装接到共享目标占位、原始许可与应用唯一任务服务。
#
# 公开接口：
#   - class RuntimeGameInstall — 当前Worker运行的游戏安装提交与结果投影。
# ============================================================

from __future__ import annotations

from threading import Event
from time import monotonic
from typing import TYPE_CHECKING

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.runtime.game_models import GameInstallRequest
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.plugins.runtime.resources import RuntimeResources
from ECL.services.operations import OperationOrigin
from ECL.utils.errors import GameServiceError

if TYPE_CHECKING:
    from ECL.services.game.plugin_games import PluginGameCoordinator


class RuntimeGameInstall:
    """
    只接纳当前运行的安装，不维护下载进度或自行创建执行线程池。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        game: PluginGameCoordinator,
        resources: RuntimeResources,
        operations: RuntimeOperations,
    ) -> None:
        """
        绑定当前会话及应用已创建的游戏、资源和任务服务。

        :param session: 当前运行授权
        :param game: 唯一游戏业务服务
        :param resources: 当前运行的精确引用表
        :param operations: 当前运行任务控制门面
        """
        self._session = session
        self._game = game
        self._resources = resources
        self._operations = operations
        self._closed = Event()
        self._origin = OperationOrigin(session.view().installation_id, session.binding.content_digest, session.run_id)

    def submit(self, params: dict[str, JsonValue], submission_deadline: float) -> dict[str, JsonValue]:
        """
        准入时占用具体目标，实际线程及句柄收尾完成后才释放占位。

        :param params: 严格校验的版本、加载器及当前根引用
        :param submission_deadline: 仅限制本次接纳的RPC截止
        :return: 自身任务引用，不表示游戏已经安装
        :raises ProtocolError: 请求、授权、占位或任务接纳不满足
        """
        from ECL.services.game.plugin_games import InstalledGame

        if self._closed.is_set():
            raise ProtocolError("游戏安装运行已关闭", "PLUGIN_SESSION_EXPIRED")
        query = GameInstallRequest.model_validate(params)
        target, write = self._resources.game_target(query.root_ref, "instances.manage", submission_deadline)
        if target.version_id is not None:
            raise ProtocolError("安装需要游戏根目录", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        try:
            reservation = self._game.reserve_install_target(target.root_path, query.version_name or query.version_id)
        except GameServiceError as exc:
            code = "PLUGIN_INSTALL_BUSY" if exc.error_code == "INSTALL_ALREADY_RUNNING" else exc.error_code
            raise ProtocolError("游戏安装目标暂时不可用", code) from exc
        deadline = monotonic() + 1800

        def verify() -> None:
            self._resources.verify_game_target(target, write, min(deadline, monotonic() + 5))

        def project(value: object) -> JsonValue:
            if not isinstance(value, InstalledGame):
                return None
            return self._resources.installed_instance(value, write, monotonic() + 5)

        def cleanup() -> bool:
            released = self._game.release_install_target(reservation)
            if self._closed.is_set():
                self._game.release_plugin_install_records(self._origin)
            return released

        try:
            reference = self._operations.submit(
                "plugin_game_install",
                lambda context: self._game.plugin_install(
                    context,
                    self._origin,
                    target,
                    reservation,
                    query.version_id,
                    query.loader,
                    query.loader_version,
                    verify,
                ),
                write,
                result_lease=write,
                project_result=project,
                cleanup=cleanup,
                deadline=deadline,
                submission_deadline=submission_deadline,
            )
        except BaseException:
            cleanup()
            raise
        return {"operation_ref": reference, "state": "queued"}

    def close(self) -> None:
        """
        失效本运行的重试身份，执行中占位仍由任务的实际收尾释放。
        """
        self._closed.set()
        self._game.release_plugin_install_records(self._origin)
