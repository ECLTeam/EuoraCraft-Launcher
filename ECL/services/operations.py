# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：长任务操作管理：后台应用操作 的提交/查询/取消。
#
# 公开接口：
#   - class OperationOrigin / OperationAdmission / OperationSnapshot — 可信来源、准入与内部快照。
#   - class OperationCommitRejectedError — 已证明没有执行提交的明确失败。
#   - class OperationContext — 向长任务工作函数提供取消状态和统一进度上报。
#       - check_cancelled() -> None — 在不会留下半成品的步骤检查取消请求，并用固定错误码结束任务。
#       - progress(percent, message, **details) -> None — 发送统一的应用长任务进度事件。
#   - class OperationManager — 管理可取消的应用下载、实例复制及资源处理任务。
#       - submit(kind, worker) -> dict[str, str] — 登记并异步执行一个长任务，立即返回稳定的任务标识。
#       - get(operation_id) -> dict[str, object] — 返回当前进程内存中的任务状态。
#       - cancel(operation_id) -> bool — 请求取消尚未完成的任务；工作函数在下一个安全检查点退出。
#       - close() -> None — 阻止新任务、取消未完成任务并释放线程池。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, RLock
from time import monotonic
from types import MappingProxyType
from typing import ClassVar, Literal
from uuid import uuid4

from ECL.utils import EventBus, get_logger
from ECL.utils.errors import GameServiceError
from ECL.utils.operation_logging import OperationTrace, trace_scope

OperationWorker = Callable[["OperationContext"], object]


class OperationCommitRejectedError(GameServiceError):
    """
    由受信提交原语确认未产生业务效果的拒绝；不能用于未确认结果的异常。
    """


@dataclass
class OperationContext:
    """
    向长任务工作函数提供取消状态和统一进度上报。
    """

    operation_id: str
    event_bus: EventBus
    cancel_event: Event
    update_callback: Callable[[float, str, dict[str, object]], dict[str, object] | None] | None = None
    kind: str = ""
    # 直接构造的用户上下文仍可使用；宿主任务提供授权检查点。
    finalize_callback: Callable[[Callable[[], object]], object] | None = None

    check_callback: Callable[[], None] | None = None
    effect_callback: Callable[[OperationEffect], None] | None = None
    should_emit_progress: bool = True

    def check_cancelled(self) -> None:
        """
        在不会留下半成品的步骤检查取消请求，并用固定错误码结束任务。
        """
        if self.check_callback is not None:
            self.check_callback()
        if self.cancel_event.is_set():
            raise GameServiceError("操作已取消", "OPERATION_CANCELLED")

    def progress(self, percent: float, message: str, **details: object) -> None:
        """
        发送统一的应用长任务进度事件。
        """
        payload = {
            "kind": self.kind,
            "operationId": self.operation_id,
            "status": "running",
            "percent": max(0.0, min(100.0, float(percent))),
            "message": message,
            **details,
        }
        if self.update_callback is not None:
            snapshot = self.update_callback(payload["percent"], message, details)
            if snapshot is not None:
                payload.update(snapshot)
        if self.should_emit_progress:
            self.event_bus.emit("game:operation_progress", payload)

    def report_effect(self, effect: OperationEffect) -> None:
        """
        在产生暂存或增量副作用前报告风险，已提交结果只能由 finalize 更新。

        :param effect: 工作函数可报告 none、staged、partial 或 unknown
        :raises ValueError: 尝试伪造提交阶段或覆盖已接受提交时抛出
        """
        if effect not in {"none", "staged", "partial", "unknown"}:
            raise ValueError("工作函数不能声明提交完成")
        self.check_cancelled()
        if self.effect_callback is not None:
            self.effect_callback(effect)

    def finalize(self, action: Callable[[], object]) -> object:
        """
        开始保存最终结果后不再接受取消，避免已保存的数据被当作失败清理。

        :param action: 不含网络请求的短时原子提交操作
        :return: 提交操作结果
        """
        if self.finalize_callback is not None:
            return self.finalize_callback(action)
        self.check_cancelled()
        return action()


@dataclass(frozen=True, slots=True)
class OperationOrigin:
    """
    标识受信宿主确认的插件安装、内容和运行，不接受客户端构造。
    """

    installation_id: str
    content_digest: str
    run_id: str


@dataclass(frozen=True, slots=True)
class OperationAdmission:
    """
    将领域任务接到宿主授权检查和短时提交登记，不执行插件代码。
    """

    check: Callable[[], None]
    accept: Callable[[Callable[[], None]], None]
    cleanup: Callable[[], bool] | None = None
    initial_effect: Literal["none", "staged"] = "none"


OperationEffect = Literal["none", "staged", "committing", "committed", "partial", "unknown"]
OperationPhase = Literal["queued", "executing", "committing", "terminal"]
OperationCancellationReason = Literal[
    "user_requested", "plugin_requested", "permission_changed", "run_closed", "application_closed"
]


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    """
    返回可信适配器使用的不可变任务快照，原始结果不能直接交付 Worker。
    """

    operation_id: str
    origin: OperationOrigin
    kind: str
    phase: OperationPhase
    status: str
    percent: float
    effect: OperationEffect
    can_cancel: bool
    cancellation_requested: bool
    result: object
    error_code: str | None
    cleanup_pending: bool
    business_stage: str | None = None


@dataclass
class _Operation:
    operation_id: str
    kind: str
    created_at: str
    cancel_event: Event = field(default_factory=Event)
    status: str = "pending"
    percent: float = 0.0
    message: str = "等待执行"
    result: object = None
    error: str | None = None
    error_code: str | None = None
    future: Future[object] | None = None
    details: dict[str, object] = field(default_factory=dict)
    can_cancel: bool = True
    trace: OperationTrace | None = None
    last_log_at: float = 0.0
    last_log_message: str = ""
    has_logged_progress: bool = False
    revision: int = 0
    origin: OperationOrigin | None = None
    admission: OperationAdmission | None = None
    phase: OperationPhase = "queued"
    effect: OperationEffect = "none"
    accepted_at: str | None = None
    io_lock: RLock = field(default_factory=RLock)
    cancellation_reason: OperationCancellationReason | None = None
    is_cleanup_complete: bool = False


class OperationManager:
    """
    管理可取消的应用下载、实例复制及资源处理任务。
    """

    max_plugin_operations: ClassVar[int] = 1024
    max_owner_operations: ClassVar[int] = 128
    max_terminal_operations: ClassVar[int] = 200
    visible_plugin_kinds: ClassVar[frozenset[str]] = frozenset(
        {
            "plugin_game_install",
            "plugin_game_launch",
            "plugin_game_stop",
            "plugin_java_install",
            "plugin_resource_install",
            "plugin_resource_delete",
            "plugin_mod_enabled",
            "plugin_modpack_prepare",
            "plugin_modpack_install",
            "plugin_modpack_export_preview",
            "plugin_modpack_export",
        }
    )

    kind_titles: ClassVar[MappingProxyType[str, str]] = MappingProxyType(
        {
            "custom_download": "自定义下载",
            "java_install": "安装 Java",
            "plugin_game_install": "插件安装游戏",
            "plugin_java_install": "插件安装 Java",
            "plugin_resource_install": "插件安装游戏资源",
            "plugin_resource_delete": "插件删除游戏资源",
            "plugin_mod_enabled": "插件调整模组状态",
            "plugin_modpack_prepare": "插件识别整合包",
            "plugin_modpack_install": "插件安装整合包",
            "plugin_modpack_export_preview": "插件预览整合包导出",
            "plugin_modpack_export": "插件导出整合包",
            "plugin_game_launch": "插件启动游戏",
            "plugin_game_stop": "插件停止游戏",
            "java_remove": "移除托管 Java",
            "java_cleanup": "清理 Java 遗留文件",
            "instance_import": "导入实例",
            "instance_export": "导出实例",
            "instance_export_preview": "扫描整合包导出文件",
            "modpack_prepare": "读取整合包安装信息",
            "modpack_online_install": "在线安装整合包",
            "resource_install": "安装资源",
            "resource_update": "更新资源",
            "world_copy": "复制存档",
            "world_export": "导出存档",
            "world_import": "导入存档",
            "world_backup": "备份存档",
            "world_restore": "恢复存档",
            "instance_clone": "复制实例",
            "instance_repair": "修复实例",
            "plugin_file_write": "插件私有文件写入",
            "plugin_download": "插件私有文件下载",
            "plugin_file_delete": "插件私有文件删除",
            "plugin_file_replace": "插件私有文件替换",
            "plugin_instance_profile_patch": "修改实例资料",
            "plugin_instance_profile_recovery": "恢复实例原资料",
            "plugin_file_recovery": "恢复插件私有文件",
        }
    )

    def __init__(self, event_bus: EventBus, max_workers: int = 3) -> None:
        """
        创建应用唯一的操作注册表和工作线程池。

        任务记录只存在于本进程内存；结束的任务按数量上限回收，不写入磁盘。

        :param event_bus: 应用事件总线
        :param max_workers: 同时执行的后台任务上限
        """
        self._events = event_bus
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ECL-ApplicationOperation")
        self._operations: dict[str, _Operation] = {}
        self._lock = RLock()
        self._closing = False
        self._logger = get_logger("OperationManager")

    def submit(self, kind: str, worker: OperationWorker) -> dict[str, str]:
        """
        登记应用用户任务，不赋予插件所有权。

        :param kind: 任务类型
        :param worker: 受信领域工作函数
        :return: 桌面任务标识与状态
        """
        return self._submit(kind, worker)

    def submit_owned(
        self, kind: str, worker: OperationWorker, origin: OperationOrigin, admission: OperationAdmission
    ) -> str:
        """
        为受信适配器登记插件任务，调用方必须在原子授权接受中调用。

        此入口不执行 I/O；出队与提交时仍独立复核原始权限。

        :param kind: 宿主定义的任务类型
        :param worker: 受信宿主工作函数
        :param origin: 宿主持有的来源
        :param admission: 原始授权检查与短提交登记
        :return: 仅供宿主保存的全局任务标识
        """
        return self._submit(kind, worker, origin, admission)["operationId"]

    def _submit(
        self,
        kind: str,
        worker: OperationWorker,
        origin: OperationOrigin | None = None,
        admission: OperationAdmission | None = None,
    ) -> dict[str, str]:
        """
        登记并异步执行一个长任务，立即返回稳定的任务标识。
        """
        with self._lock:
            if self._closing:
                raise GameServiceError("启动器正在关闭，无法创建新任务", "OPERATION_MANAGER_CLOSED")
            if origin is not None and (
                sum(item.origin is not None for item in self._operations.values()) >= self.max_plugin_operations
                or sum(item.origin == origin for item in self._operations.values()) >= self.max_owner_operations
            ):
                raise GameServiceError("插件任务记录已达上限", "PLUGIN_RATE_LIMITED")
            operation_id = uuid4().hex
            operation = _Operation(operation_id, kind, datetime.now(UTC).isoformat())
            operation.origin = origin
            operation.admission = admission
            if admission is not None:
                operation.effect = admission.initial_effect
            operation.trace = OperationTrace(self.kind_titles.get(kind, kind), monotonic(), True)
            self._operations[operation_id] = operation
            try:
                operation.future = self._executor.submit(copy_context().run, self._run, operation, worker)
            except RuntimeError as exc:
                self._operations.pop(operation_id, None)
                raise GameServiceError("任务执行器未接纳操作", "OPERATION_MANAGER_CLOSED") from exc
        self._emit(operation)
        return {"operationId": operation_id, "status": operation.status}

    def _run(self, operation: _Operation, worker: OperationWorker) -> None:
        with trace_scope(operation.trace):
            self._execute(operation, worker)

    def _execute(self, operation: _Operation, worker: OperationWorker) -> None:
        with self._lock:
            operation.status = "running"
            operation.phase = "executing"
            if operation.origin is None:
                operation.effect = "unknown"
            operation.message = "正在执行"
            operation.revision += 1
        self._emit(operation)
        title = self.kind_titles.get(operation.kind, operation.kind)
        if operation.trace is not None:
            operation.trace.log(self._logger, f"后台任务开始执行；任务类型：{title}", include_duration=False)
        else:
            self._logger.info("后台任务开始执行；任务类型：%s", title)

        context = OperationContext(
            operation.operation_id,
            self._events,
            operation.cancel_event,
            lambda percent, message, details: self._update(operation, percent, message, details),
            operation.kind,
            lambda action: self._finalize(operation, action),
            lambda: self._checkpoint(operation),
            lambda effect: self._report_effect(operation, effect),
            operation.origin is None or operation.kind in self.visible_plugin_kinds,
        )
        try:
            context.check_cancelled()
            # worker 返回即代表副作用已经落盘；此处若再检查取消，会把已完成的任务
            # 标成「已取消」，导致用户重复执行同一操作。
            result = worker(context)
        except Exception as exc:
            with self._lock:
                is_cancelled = getattr(exc, "error_code", None) == "OPERATION_CANCELLED" and operation.can_cancel
                operation.status = "cancelled" if is_cancelled else "failed"
                operation.message = "操作已取消" if is_cancelled else str(exc)
                operation.error = str(exc)
                operation.error_code = getattr(exc, "error_code", "GAME_OPERATION_FAILED")
                operation.phase = "terminal"
                operation.can_cancel = False
                operation.revision += 1
            if operation.status == "cancelled":
                self._logger.info(
                    "后台任务已取消并停止执行；任务类型：%s", self.kind_titles.get(operation.kind, operation.kind)
                )
            elif isinstance(exc, GameServiceError):
                self._logger.warning(
                    "后台任务执行失败；任务类型：%s；错误码：%s；原因：%s",
                    self.kind_titles.get(operation.kind, operation.kind),
                    operation.error_code,
                    exc,
                )
            else:
                self._logger.exception(
                    "后台任务执行失败；任务类型：%s；错误码：%s",
                    self.kind_titles.get(operation.kind, operation.kind),
                    operation.error_code,
                )
        else:
            with self._lock:
                operation.status = "completed"
                operation.percent = 100.0
                operation.message = "操作完成"
                operation.result = result
                operation.phase = "terminal"
                operation.can_cancel = False
                operation.revision += 1
            if operation.trace is not None:
                operation.trace.log(self._logger, "后台任务执行完成")
        self._cleanup_owned(operation)
        self._log_terminal(operation)
        self._evict_terminal_operations()
        self._emit(operation)

    def _log_terminal(self, operation: _Operation) -> None:
        """
        为插件任务记录终态审计日志，替代此前的磁盘任务摘要文件。

        只写入来源标识与阶段结果，不记录参数、路径或业务返回值。

        :param operation: 已进入终态的任务
        """
        if operation.origin is None:
            return
        with self._lock:
            origin = operation.origin
            status = operation.status
            effect = operation.effect
            fields = (
                operation.kind,
                operation.phase,
                status,
                effect,
                operation.error_code or "-",
                operation.cancellation_reason or "-",
            )
        # 只对有实际副作用或失败的任务输出可检索的审计行，其余保持在调试级别。
        is_auditable = status == "failed" or effect in {"committing", "committed", "partial"}
        log = self._logger.info if is_auditable else self._logger.debug
        log(
            "插件任务终态；任务类型：%s；阶段：%s；状态：%s；副作用：%s；错误码：%s；取消原因：%s；"
            "安装：%s；内容：%s；运行：%s",
            *fields,
            origin.installation_id,
            origin.content_digest,
            origin.run_id,
        )

    def _evict_terminal_operations(self) -> None:
        """
        按创建顺序回收超额的非插件终态任务，避免长时间运行时无限累积。

        插件任务由运行关闭释放与总量上限管理，不参与此处驱逐。
        """
        with self._lock:
            terminal = [item for item in self._operations.values() if item.origin is None and item.phase == "terminal"]
            overflow = len(terminal) - self.max_terminal_operations
            if overflow <= 0:
                return
            terminal.sort(key=lambda item: item.created_at)
            for item in terminal[:overflow]:
                self._operations.pop(item.operation_id, None)

    def _cleanup_owned(self, operation: _Operation) -> bool:
        """
        在线程实际结束或排队取消后释放领域资源；失败只重试清理，不重放业务。

        清理成功是受信适配器对暂存实际消失的确认，不再检查已撤销的执行权限。
        已提交或未知的业务效果不能被清理结果覆盖。
        """
        with operation.io_lock:
            if operation.is_cleanup_complete:
                return True
            cleanup = operation.admission.cleanup if operation.admission else None
            if cleanup is not None:
                try:
                    if not cleanup():
                        return False
                except Exception:
                    self._logger.exception("后台任务资源清理失败，保留任务等待重试")
                    return False
            with self._lock:
                operation.is_cleanup_complete = True
                if cleanup is not None and operation.effect in {"none", "staged"}:
                    operation.effect = "none"
                    operation.revision += 1
            return True

    def _update(
        self, operation: _Operation, percent: float, message: str, details: dict[str, object]
    ) -> dict[str, object]:
        with self._lock:
            self._check_active(operation)
            operation.percent = percent
            operation.message = message
            operation.details.update(details)
            operation.revision += 1
            now = monotonic()
            stage = details.get("stage") or details.get("subtask")
            has_stage_change = isinstance(stage, str) and stage != operation.last_log_message
            should_log = not operation.has_logged_progress or has_stage_change or now - operation.last_log_at >= 5.0
            if should_log:
                operation.has_logged_progress = True
                operation.last_log_message = stage if isinstance(stage, str) else ""
                operation.last_log_at = now
        if should_log:
            safe_message = message if operation.origin is None else operation.phase
            self._logger.debug(
                "后台任务进度：%.0f%%；阶段：%s", percent, safe_message, extra={"has_operation_duration": True}
            )
        return self._payload(operation)

    def _checkpoint(self, operation: _Operation) -> None:
        with self._lock:
            self._check_active(operation)
            should_check = operation.can_cancel and operation.admission is not None
        if should_check:
            assert operation.admission is not None
            operation.admission.check()

    @staticmethod
    def _check_active(operation: _Operation) -> None:
        if operation.phase == "terminal":
            raise GameServiceError("任务已经结束", "OPERATION_FINISHED")

    def _report_effect(self, operation: _Operation, effect: OperationEffect) -> None:
        """
        报告增量副作用，不允许覆盖已接受提交的结果。
        """
        with self._lock:
            self._check_active(operation)
            if not operation.can_cancel:
                raise ValueError("不能覆盖已接受提交的副作用状态")
            operation.effect = effect
            operation.revision += 1

    def _accept_commit(self, operation: _Operation) -> None:
        """
        在短任务锁中排序取消与提交接受，不执行磁盘或领域动作。
        """
        with self._lock:
            self._check_active(operation)
            if operation.accepted_at is not None:
                raise GameServiceError("当前任务已经在提交", "OPERATION_COMMIT_STARTED")
            if operation.cancel_event.is_set():
                raise GameServiceError("操作已取消", "OPERATION_CANCELLED")
            operation.can_cancel = False
            operation.phase = "committing"
            operation.effect = "committing"
            operation.accepted_at = datetime.now(UTC).isoformat()
            operation.revision += 1

    def _finalize(self, operation: _Operation, action: Callable[[], object]) -> object:
        """
        接受一次有限提交，并在所有全局锁外执行领域动作。
        """
        previous_effect = operation.effect
        if operation.admission is not None:
            operation.admission.accept(lambda: self._accept_commit(operation))
        else:
            self._accept_commit(operation)
        try:
            result = action()
        except Exception as exc:
            with self._lock:
                operation.effect = previous_effect if isinstance(exc, OperationCommitRejectedError) else "unknown"
            raise
        with self._lock:
            operation.effect = "committed"
            operation.revision += 1
        return result

    def _payload(self, operation: _Operation) -> dict[str, object]:
        with self._lock:
            return {
                **operation.details,
                "operationId": operation.operation_id,
                "kind": operation.kind,
                "status": operation.status,
                "percent": operation.percent,
                "message": operation.message,
                "createdAt": operation.created_at,
                "result": None
                if operation.origin is not None and operation.kind in self.visible_plugin_kinds
                else operation.result,
                "error": operation.error,
                "errorCode": operation.error_code,
                "canCancel": operation.can_cancel and operation.status in {"pending", "running"},
                "cancellationRequested": operation.cancel_event.is_set(),
                "revision": operation.revision,
            }

    def _emit(self, operation: _Operation) -> None:
        if operation.origin is not None and operation.kind not in self.visible_plugin_kinds:
            return
        with self._lock:
            snapshot = self._payload(operation)
        self._events.emit("game:operation_progress", snapshot)

    def owned_snapshot(self, operation_id: str, origin: OperationOrigin) -> OperationSnapshot:
        """
        仅返回当前进程中精确来源的任务，不从历史文件恢复控制权。

        :param operation_id: 宿主内部任务标识
        :param origin: 适配器绑定的来源
        :return: 最新不可变快照
        :raises GameServiceError: 来源不符或任务不存在时抛出
        """
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation.origin != origin:
                raise GameServiceError("未找到自身任务", "PLUGIN_RESOURCE_NOT_FOUND")
        self._retry_terminal(operation)
        with self._lock:
            return OperationSnapshot(
                operation_id,
                origin,
                operation.kind,
                operation.phase,
                operation.status,
                operation.percent,
                operation.effect,
                operation.can_cancel,
                operation.cancel_event.is_set(),
                operation.result,
                operation.error_code,
                operation.phase == "terminal"
                and (operation.future is None or not operation.future.done() or not operation.is_cleanup_complete),
                business_stage=(
                    operation.details["stage"]
                    if operation.kind in self.visible_plugin_kinds and isinstance(operation.details.get("stage"), str)
                    else None
                ),
            )

    def cancel_owned(
        self,
        operation_id: str,
        origin: OperationOrigin,
        *,
        reason: OperationCancellationReason = "plugin_requested",
    ) -> bool:
        """
        在同一任务锁中校验来源并请求取消，不能取消别人的任务。

        :param operation_id: 内部标识
        :param origin: 可信来源
        :param reason: 宿主定义的取消原因，不来自插件载荷
        :return: 是否接受取消
        """
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation.origin != origin:
                raise GameServiceError("未找到自身任务", "PLUGIN_RESOURCE_NOT_FOUND")
            return self.cancel(operation_id, reason=reason)

    def _retry_terminal(self, operation: _Operation) -> bool:
        """
        对已结束 future 尝试一次资源清理；其他查询正收尾时立即返回。
        """
        if operation.future is None or not operation.future.done():
            return False
        if not operation.io_lock.acquire(blocking=False):
            return False
        try:
            return self._cleanup_owned(operation)
        finally:
            operation.io_lock.release()

    def owner_idle(self, origin: OperationOrigin) -> bool:
        """
        查询实际 future 与资源清理是否全部结束，不重复执行业务动作。

        :param origin: 当前收尾来源
        :return: 所有工作线程与必要清理均已结束
        """
        with self._lock:
            operations = tuple(item for item in self._operations.values() if item.origin == origin)
        is_idle = True
        for operation in operations:
            if not self._retry_terminal(operation):
                is_idle = False
        return is_idle

    def release_owner(self, origin: OperationOrigin) -> bool:
        """
        仅在实际执行与清理完成后释放已关闭运行的内存记录。

        :param origin: 已关闭来源
        :return: 是否已安全释放
        """
        if not self.owner_idle(origin):
            return False
        with self._lock:
            self._operations = {key: item for key, item in self._operations.items() if item.origin != origin}
        return True

    def get(self, operation_id: str) -> dict[str, object]:
        """
        返回当前进程内登记的任务状态。

        任务记录不落盘，因此进程重启后同一标识一律视为不存在。

        :param operation_id: 宿主任务标识
        :return: 任务状态载荷
        :raises GameServiceError: 标识格式非法或当前进程没有该任务时抛出
        """
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is not None:
                return self._payload(operation)
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(c not in "0123456789abcdef" for c in operation_id)
        ):
            raise GameServiceError("任务标识无效", "OPERATION_NOT_FOUND")
        raise GameServiceError("未找到指定任务", "OPERATION_NOT_FOUND")

    def cancel(self, operation_id: str, *, reason: OperationCancellationReason = "user_requested") -> bool:
        """
        请求取消尚未完成的任务；工作函数在下一个安全检查点退出。

        :param operation_id: 宿主任务标识
        :param reason: 受信调用方的原因类别，不包含原始参数
        :return: 是否接受取消请求
        """
        with self._lock:
            operation = self._operations.get(operation_id)
            if (
                operation is None
                or not operation.can_cancel
                or operation.status in {"completed", "failed", "cancelled"}
            ):
                return False
            operation.cancel_event.set()
            if operation.cancellation_reason is None:
                operation.cancellation_reason = reason
            operation.message = "正在取消"
            operation.revision += 1
        self._emit(operation)
        self._logger.info(
            "已请求取消后台任务，等待安全检查点；任务类型：%s", self.kind_titles.get(operation.kind, operation.kind)
        )
        return True

    def close(self) -> None:
        """
        阻止新任务、取消未完成任务并释放线程池。
        """
        with self._lock:
            self._closing = True
            operations = tuple(self._operations.values())
            for operation in operations:
                if (
                    operation.can_cancel
                    and operation.status in {"pending", "running"}
                    and not operation.cancel_event.is_set()
                ):
                    operation.cancel_event.set()
                    operation.cancellation_reason = "application_closed"
                    operation.revision += 1
        self._executor.shutdown(wait=False, cancel_futures=True)
        for operation in operations:
            if operation.future is not None and operation.future.cancelled():
                with self._lock:
                    if operation.status == "cancelled":
                        continue
                    operation.status = "cancelled"
                    operation.phase = "terminal"
                    operation.can_cancel = False
                    operation.message = "操作已取消"
                    operation.error_code = "OPERATION_CANCELLED"
                    operation.revision += 1
                with trace_scope(operation.trace):
                    self._logger.info("排队中的后台任务已取消，未开始执行")
                    self._cleanup_owned(operation)
                    self._log_terminal(operation)
                    self._emit(operation)
