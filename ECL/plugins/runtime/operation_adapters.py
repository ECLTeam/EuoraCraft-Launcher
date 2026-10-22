# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：绑定单次 Worker 的宿主任务引用、结果投影和退出收尾，不创建业务执行器。
#
# 公开接口：
#   - class RuntimeOperations — 将自身任务控制接到应用唯一任务服务。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.runtime.models import OperationGetRequest, OperationListRequest
from ECL.plugins.runtime.resources import ResourcePages
from ECL.services.operations import OperationAdmission, OperationManager, OperationOrigin, OperationWorker
from ECL.utils.errors import GameServiceError


@dataclass(frozen=True, slots=True)
class _OperationReference:
    operation_id: str
    leases: tuple[GrantLease, ...]
    project_result: Callable[[object], JsonValue] | None
    result_lease: GrantLease | None


class RuntimeOperations:
    """
    仅保存本 run 的引用和投影契约，执行状态始终由应用任务服务维护。
    """

    def __init__(self, session: PluginGrantSession, manager: OperationManager) -> None:
        """
        绑定宿主来源，不接受 Worker 自报身份。

        :param session: 当前授权会话
        :param manager: 应用现有唯一任务服务
        """
        self._session = session
        self._manager = manager
        self._origin = OperationOrigin(session.view().installation_id, session.binding.content_digest, session.run_id)
        self._references: dict[str, _OperationReference] = {}
        self._lock = RLock()
        self._closed = False
        self._pages = ResourcePages()

    def submit(
        self,
        kind: str,
        worker: OperationWorker,
        lease: GrantLease,
        *,
        project_result: Callable[[object], JsonValue] | None = None,
        result_lease: GrantLease | None = None,
        cleanup: Callable[[], bool] | None = None,
        initial_effect: Literal["none", "staged"] = "none",
        additional_leases: tuple[GrantLease, ...] = (),
        deadline: float | None = None,
        submission_deadline: float | None = None,
    ) -> str:
        """
        供受信业务适配器创建有限任务，不暴露生产 RPC 提交入口。

        :param kind: 宿主定义的任务类型，不从插件请求直接透传
        :param worker: 受信宿主工作函数
        :param lease: 该工作实际能力和精确资源的原始准入
        :param project_result: 显式安全结果投影；未提供时不交付结果
        :param result_lease: 显式结果准入；文件视图需独立读许可，删除确认绑定原删除许可
        :param cleanup: 实际执行结束或排队取消后的幂等资源清理
        :param initial_effect: 接纳时已持有的暂存状态
        :param additional_leases: 同一任务必须共同有效的其他原始执行准入
        :param deadline: 执行及最终提交接受的总截止
        :param submission_deadline: 仅控制新任务入队的 RPC 截止
        :return: 本次运行的随机引用
        :raises PluginGrantError: 准入已经失效时抛出
        :raises ProtocolError: 运行已关闭或引用容量耗尽时抛出
        """
        leases = (lease, *additional_leases)

        def check_deadline() -> None:
            if deadline is not None and monotonic() >= deadline:
                raise ProtocolError("插件任务超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")

        def check() -> None:
            self._session.check_all(leases)
            check_deadline()

        def accept(register: Callable[[], None]) -> None:
            def accepted() -> None:
                check_deadline()
                register()

            self._session.accept_all(leases, accepted)

        check()
        admission = OperationAdmission(
            check,
            accept,
            lease.capability,
            lease.resource_refs,
            cleanup,
            initial_effect,
        )
        # 准备后的提交按授权锁 → 引用锁 → 任务锁排列，关闭与创建不能相互越过。
        reference = uuid4().hex

        def register() -> None:
            with self._lock:
                if submission_deadline is not None and monotonic() >= submission_deadline:
                    raise ProtocolError("插件提交超过截止时间", "PLUGIN_DEADLINE_EXCEEDED")
                if self._closed:
                    raise ProtocolError("插件运行已经关闭", "PLUGIN_SESSION_EXPIRED")
                if len(self._references) >= 128:
                    raise ProtocolError("本运行任务引用已达上限", "PLUGIN_RATE_LIMITED")
                # manager 的内部入队只登记状态；昂贵的授权检查已在外层完成。
                operation_id = self._manager.submit_owned(kind, worker, self._origin, admission)
                self._references[reference] = _OperationReference(operation_id, leases, project_result, result_lease)

        try:
            accept(register)
        except GameServiceError as exc:
            raise ProtocolError("插件任务未能提交", exc.error_code) from exc
        return reference

    def _reference(self, reference: str) -> _OperationReference:
        with self._lock:
            if self._closed:
                raise ProtocolError("插件运行已经关闭", "PLUGIN_SESSION_EXPIRED")
            item = self._references.get(reference)
        if item is None:
            raise ProtocolError("未找到自身任务", "PLUGIN_RESOURCE_NOT_FOUND")
        return item

    def _view(self, reference: str) -> dict[str, JsonValue]:
        """
        从权威快照构造白名单视图；每次交付结果前复核原权限。
        """
        item = self._reference(reference)
        snapshot = self._manager.owned_snapshot(item.operation_id, self._origin)
        state = (
            snapshot.status
            if snapshot.phase == "terminal"
            else {
                "queued": "queued",
                "executing": "running",
                "committing": "committing",
            }[snapshot.phase]
        )
        result: JsonValue = None
        if snapshot.status == "completed" and item.project_result is not None and item.result_lease is not None:
            try:
                self._session.check(item.result_lease)
                result = _json_value(item.project_result(snapshot.result))
                self._session.check(item.result_lease)
            except (PluginGrantError, ProtocolError):
                result = None
        return {
            "operation_ref": reference,
            "kind": snapshot.kind,
            "state": state,
            "percent": snapshot.percent,
            "stage": snapshot.phase,
            "message": state,
            "can_cancel": snapshot.can_cancel,
            "cancellation_requested": snapshot.cancellation_requested,
            "effect": snapshot.effect,
            "cleanup_pending": snapshot.cleanup_pending,
            "result": result,
            "error_code": snapshot.error_code,
        }

    def handle(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        分派严格的自身任务查询、分页和取消，不接受生产任务创建请求。

        :param method: 三个正式控制方法之一
        :param params: 不可信 JSON 参数
        :return: 安全任务视图、页面或取消请求状态
        :raises ProtocolError: 引用、游标、状态或方法不允许时抛出
        """
        if method == "operations.list":
            request = OperationListRequest.model_validate(params)

            def load() -> tuple[dict[str, JsonValue], ...]:
                with self._lock:
                    if self._closed:
                        raise ProtocolError("插件运行已经关闭", "PLUGIN_SESSION_EXPIRED")
                    return tuple({"operation_ref": ref} for ref in self._references)

            def project(entry: dict[str, JsonValue]) -> dict[str, JsonValue]:
                reference = entry["operation_ref"]
                assert isinstance(reference, str)
                return self._view(reference)

            return self._pages.page(
                f"{self._origin.run_id}:{request.limit}",
                self._origin.run_id,
                request.cursor,
                request.limit,
                load,
                project=project,
            )
        if method not in {"operations.get", "operations.cancel"}:
            raise ProtocolError("未开放任务提交接口", "PLUGIN_API_METHOD_NOT_FOUND")
        request = OperationGetRequest.model_validate(params)
        item = self._reference(request.operation_ref)
        if method == "operations.get":
            return self._view(request.operation_ref)
        snapshot = self._manager.owned_snapshot(item.operation_id, self._origin)
        if snapshot.phase == "committing":
            raise ProtocolError("任务已接受提交", "PLUGIN_COMMIT_STARTED")
        requested = self._manager.cancel_owned(item.operation_id, self._origin)
        view = self._view(request.operation_ref)
        if not requested and view["state"] == "committing":
            raise ProtocolError("任务已接受提交", "PLUGIN_COMMIT_STARTED")
        return {"requested": requested, "state": view["state"], "effect": view["effect"]}

    def refresh_permissions(self) -> None:
        """
        只对原 lease 已失效的自身未提交任务请求取消，不为旧任务换租约。
        """
        with self._lock:
            if self._closed:
                return
            items = tuple(self._references.values())
        for item in items:
            try:
                self._session.check_all(item.leases)
            except PluginGrantError:
                with self._lock:
                    if not self._closed:
                        self._manager.cancel_owned(item.operation_id, self._origin, reason="permission_changed")
        self._manager.owner_idle(self._origin)

    def close(self) -> None:
        """
        幂等阻止新引用并请求取消自身任务，已接受提交由宿主执行方收尾。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for item in self._references.values():
                self._manager.cancel_owned(item.operation_id, self._origin, reason="run_closed")
        self._pages.clear()

    def is_idle(self) -> bool:
        """
        确认实际线程和审计保存结束，关闭后才释放引用与宿主记录。

        :return: 当前运行没有未结束的宿主任务或审计收尾
        """
        if not self._manager.owner_idle(self._origin):
            return False
        with self._lock:
            if self._closed:
                if not self._manager.release_owner(self._origin):
                    return False
                self._references.clear()
        return True
