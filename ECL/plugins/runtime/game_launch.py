# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：绑定本次Worker的启动、运行查询和停止引用，任务与进程仍由应用现有服务拥有。
#
# 公开接口：
#   - class RuntimeGameLaunch — 当前运行的精确进程控制与公开结果投影。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, RLock
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.runtime.game_models import GameLaunchRequest, GameRunningRequest, GameStopRequest
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.plugins.runtime.resources import RuntimeResources
from ECL.services.operations import OperationOrigin
from ECL.utils.errors import GameServiceError

if TYPE_CHECKING:
    from ECL.plugins.runtime.java_api import RuntimeJava
    from ECL.services.game.plugin_games import GameResourceTarget, PluginGameCoordinator, PluginGameRun


@dataclass(frozen=True, slots=True)
class _GameRunReference:
    token: str
    instance_ref: str
    target: GameResourceTarget


class RuntimeGameLaunch:
    """
    仅持有本运行的随机进程引用，不接受PID或实例名替代所有者校验。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        game: PluginGameCoordinator,
        resources: RuntimeResources,
        operations: RuntimeOperations,
        java: RuntimeJava | None = None,
    ) -> None:
        """
        绑定已有授权、游戏和任务服务，不建立进程管理器或执行器。

        :param session: 当前Worker授权会话
        :param game: 应用唯一游戏服务
        :param resources: 当前运行的物理资源引用
        :param operations: 当前运行任务门面
        :param java: 同一运行的 Java 引用服务，不接受客户端路径
        """
        self._session = session
        self._game = game
        self._resources = resources
        self._operations = operations
        self._java = java
        self._origin = OperationOrigin(session.view().installation_id, session.binding.content_digest, session.run_id)
        self._references: dict[str, _GameRunReference] = {}
        self._lock = RLock()
        self._closed = Event()

    def _view(self, run: PluginGameRun, instance_ref: str, target: GameResourceTarget) -> dict[str, JsonValue]:
        """
        为同一所有者的确切运行复用随机引用，只更新已经验证的实例范围。
        """
        with self._lock:
            if self._closed.is_set():
                raise ProtocolError("游戏控制运行已经关闭", "PLUGIN_SESSION_EXPIRED")
            key = next((key for key, item in self._references.items() if item.token == run.token), None)
            if key is None:
                if len(self._references) >= 128:
                    raise ProtocolError("游戏进程引用已达上限", "PLUGIN_RATE_LIMITED")
                key = uuid4().hex
            self._references[key] = _GameRunReference(run.token, instance_ref, target)
        return {
            "game_run_ref": key,
            "instance_ref": instance_ref,
            "pid": run.pid,
            "running": run.running,
            "exit_code": run.exit_code,
        }

    def _project(self, value: object, instance_ref: str, target: GameResourceTarget, read: GrantLease) -> JsonValue:
        """
        结果交付仍需原始管理准入，任务完成不能恢复已撤销权限。
        """
        from ECL.services.game.plugin_games import PluginGameRun

        if not isinstance(value, PluginGameRun):
            return None
        self._resources.verify_game_target(target, read, monotonic() + 5)
        return self._view(self._game.plugin_game_run(self._origin, value.token), instance_ref, target)

    def launch(self, params: dict[str, JsonValue], submission_deadline: float) -> dict[str, JsonValue]:
        """
        用原始管理准入登记全局单启动任务，在进程创建时再次复核。

        :param params: 当前实例引用及有限窗口、内存选项
        :param submission_deadline: 任务接纳截止
        :return: 本运行任务回执
        :raises ProtocolError: 请求、权限、全局占位或宿主不满足
        """
        query = GameLaunchRequest.model_validate(params)
        target, launch = self._resources.game_target(query.instance_ref, "instances.manage", submission_deadline)
        if target.version_id is None:
            raise ProtocolError("启动需要具体实例", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        deadline = monotonic() + 180
        java_path = None
        java_check = None
        if query.options.runtime_ref is not None:
            if self._java is None:
                raise ProtocolError("当前宿主未提供 Java 选用", "PLUGIN_API_METHOD_NOT_FOUND")
            record, java_check = self._java.selection(query.options.runtime_ref, deadline)
            java_path = record.executable_path
        try:
            reservation = self._game.reserve_plugin_launch(self._origin)
        except GameServiceError as exc:
            raise ProtocolError("游戏启动暂时不可用", exc.error_code) from exc
        deadline = monotonic() + 180

        def verify() -> None:
            self._resources.verify_game_target(target, launch, min(deadline, monotonic() + 5))
            if java_check is not None:
                java_check()

        try:
            reference = self._operations.submit(
                "plugin_game_launch",
                lambda context: self._game.plugin_launch(
                    context,
                    self._origin,
                    target,
                    reservation,
                    verify,
                    query.options.memory_mb,
                    query.options.width,
                    query.options.height,
                    query.options.fullscreen,
                    java_path=java_path,
                ),
                launch,
                result_lease=launch,
                project_result=lambda value: self._project(value, query.instance_ref, target, launch),
                cleanup=lambda: self._game.release_plugin_launch(reservation),
                deadline=deadline,
                submission_deadline=submission_deadline,
            )
        except BaseException:
            self._game.release_plugin_launch(reservation)
            raise
        return {"operation_ref": reference, "state": "queued"}

    def running(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        仅返回本运行已提交的进程，原始业务准入失效时不交付旧状态。

        :param params: 当前实例引用
        :param deadline: 当前查询截止
        :return: 本运行进程记录
        :raises ProtocolError: 当前引用或读取权限失效
        """
        query = GameRunningRequest.model_validate(params)
        target, read = self._resources.game_target(query.instance_ref, "instances.read", deadline)
        items: list[JsonValue] = [
            self._view(run, query.instance_ref, target) for run in self._game.plugin_running(self._origin, target)
        ]
        self._resources.verify_game_target(target, read, deadline)
        return {"items": items}

    def stop(self, params: dict[str, JsonValue], submission_deadline: float) -> dict[str, JsonValue]:
        """
        管理权限的停止操作仅作用于本运行的确切进程，重复请求不会获得其他进程控制权。

        :param params: 本运行持有的游戏引用
        :param submission_deadline: 当前接纳截止
        :return: 自身停止任务回执，非已退出确认
        :raises ProtocolError: 引用、来源或停止权限不满足
        """
        query = GameStopRequest.model_validate(params)
        with self._lock:
            owned = self._references.get(query.game_run_ref)
        if owned is None:
            raise ProtocolError("游戏进程不属于当前运行", "PLUGIN_OWNER_MISMATCH")
        target, stop = self._resources.game_target(owned.instance_ref, "instances.manage", submission_deadline)
        if target != owned.target:
            raise ProtocolError("游戏实例身份已经变化", "PLUGIN_RESOURCE_UNAVAILABLE")
        deadline = monotonic() + 30
        reference = self._operations.submit(
            "plugin_game_stop",
            lambda context: self._game.plugin_stop(context, self._origin, owned.token),
            stop,
            result_lease=stop,
            project_result=lambda value: self._project(value, owned.instance_ref, target, stop),
            deadline=deadline,
            submission_deadline=submission_deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    def close(self) -> None:
        """
        拒绝新引用，已接纳的停止先实际收尾；不停止已提交的游戏。
        """
        self._closed.set()
        if self._operations.is_idle():
            self.release()

    def release(self) -> None:
        """
        任务执行和审计收尾结束后释放本运行索引，不删除游戏服务中的活跃用户视图。
        """
        if not self._closed.is_set():
            return
        self._game.release_plugin_game_runs(self._origin)
        with self._lock:
            self._references.clear()
