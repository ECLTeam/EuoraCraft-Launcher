# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：长任务操作管理：后台应用操作 的提交/查询/取消。
#
# 公开接口：
#   - class OperationContext — 向长任务工作函数提供取消状态和统一进度上报。
#       - check_cancelled() -> None — 在安全边界检查取消状态，并使用稳定错误码中断任务。
#       - progress(percent, message, **details) -> None — 发送统一的应用长任务进度事件。
#   - class OperationManager — 管理可取消的应用下载、实例复制及资源处理任务。
#       - submit(kind, worker) -> dict[str, str] — 登记并异步执行一个长任务，立即返回稳定的任务标识。
#       - get(operation_id) -> dict[str, object] — 返回当前进程中的任务状态或上次持久化的最终结果。
#       - cancel(operation_id) -> bool — 请求取消尚未完成的任务；工作函数在下一个安全检查点退出。
#       - close() -> None — 阻止新任务、取消未完成任务并释放线程池。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock
from uuid import uuid4

from ECL.events import EventBus
from ECL.utils import atomic_write_text, get_logger

OperationWorker = Callable[["OperationContext"], object]


@dataclass
class OperationContext:
    """
    向长任务工作函数提供取消状态和统一进度上报。
    """

    operation_id: str
    event_bus: EventBus
    cancel_event: Event
    update_callback: Callable[[float, str, dict[str, object]], None] | None = None
    kind: str = ""
    finalize_callback: Callable[[Callable[[], object]], object] | None = None

    def check_cancelled(self) -> None:
        """
        在安全边界检查取消状态，并使用稳定错误码中断任务。
        """
        if self.cancel_event.is_set():
            from ECL.utils.errors import GameServiceError

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
            self.update_callback(payload["percent"], message, details)
        self.event_bus.emit("game:operation_progress", payload)

    def finalize(self, action: Callable[[], object]) -> object:
        """
        在取消互斥边界内完成最终落盘，落盘成功后不再接受取消。

        :param action: 不含网络请求的短时原子提交操作
        :return: 提交操作结果
        """
        if self.finalize_callback is not None:
            return self.finalize_callback(action)
        self.check_cancelled()
        return action()


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


class OperationManager:
    """
    管理可取消的应用下载、实例复制及资源处理任务。
    """

    def __init__(self, data_path: Path, event_bus: EventBus, max_workers: int = 3) -> None:
        """
        创建应用唯一的操作注册表和工作线程池。

        :param data_path: 最终任务结果的持久化根目录
        :param event_bus: 应用事件总线
        :param max_workers: 同时执行的后台任务上限
        """
        self._data_path = data_path / "operations"
        self._events = event_bus
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ECL-GameOperation")
        self._operations: dict[str, _Operation] = {}
        self._lock = RLock()
        self._closing = False
        self._logger = get_logger("OperationManager")

    def submit(self, kind: str, worker: OperationWorker) -> dict[str, str]:
        """
        登记并异步执行一个长任务，立即返回稳定的任务标识。
        """
        with self._lock:
            if self._closing:
                from ECL.utils.errors import GameServiceError

                raise GameServiceError("启动器正在关闭，无法创建新任务", "OPERATION_MANAGER_CLOSED")
            operation_id = uuid4().hex
            operation = _Operation(operation_id, kind, datetime.now(UTC).isoformat())
            self._operations[operation_id] = operation
            operation.future = self._executor.submit(self._run, operation, worker)
        self._emit(operation)
        return {"operationId": operation_id, "status": operation.status}

    def _run(self, operation: _Operation, worker: OperationWorker) -> None:
        with self._lock:
            operation.status = "running"
            operation.message = "正在执行"
        self._emit(operation)

        def update(percent: float, message: str, details: dict[str, object]) -> None:
            with self._lock:
                operation.percent = percent
                operation.message = message
                operation.details.update(details)

        def finalize(action: Callable[[], object]) -> object:
            with self._lock:
                context.check_cancelled()
                result = action()
                operation.can_cancel = False
                return result

        context = OperationContext(
            operation.operation_id, self._events, operation.cancel_event, update, operation.kind, finalize
        )
        try:
            context.check_cancelled()
            result = worker(context)
            context.check_cancelled()
        except Exception as exc:
            with self._lock:
                operation.status = "cancelled" if operation.cancel_event.is_set() else "failed"
                operation.message = "操作已取消" if operation.cancel_event.is_set() else str(exc)
                operation.error = str(exc)
                operation.error_code = getattr(exc, "error_code", "GAME_OPERATION_FAILED")
            self._logger.warning(
                "游戏长任务失败: id=%s, kind=%s, error=%s", operation.operation_id, operation.kind, exc
            )
        else:
            with self._lock:
                operation.status = "completed"
                operation.percent = 100.0
                operation.message = "操作完成"
                operation.result = result
        self._persist(operation)
        self._emit(operation)

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
                "result": operation.result,
                "error": operation.error,
                "errorCode": operation.error_code,
            }

    def _emit(self, operation: _Operation) -> None:
        self._events.emit("game:operation_progress", self._payload(operation))

    def _persist(self, operation: _Operation) -> None:
        try:
            import json

            self._data_path.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._data_path / f"{operation.operation_id}.json",
                json.dumps(self._payload(operation), ensure_ascii=False, indent=2),
            )
        except OSError:
            self._logger.exception("持久化游戏长任务结果失败: %s", operation.operation_id)

    def get(self, operation_id: str) -> dict[str, object]:
        """
        返回当前进程中的任务状态或上次持久化的最终结果。
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
            from ECL.utils.errors import GameServiceError

            raise GameServiceError("任务标识无效", "OPERATION_NOT_FOUND")
        result_path = self._data_path / f"{operation_id}.json"
        if result_path.is_file():
            import json

            try:
                return json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                pass
        from ECL.utils.errors import GameServiceError

        raise GameServiceError("未找到指定任务", "OPERATION_NOT_FOUND")

    def cancel(self, operation_id: str) -> bool:
        """
        请求取消尚未完成的任务；工作函数在下一个安全检查点退出。
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
            operation.message = "正在取消"
        self._emit(operation)
        return True

    def close(self) -> None:
        """
        阻止新任务、取消未完成任务并释放线程池。
        """
        with self._lock:
            self._closing = True
            operations = tuple(self._operations.values())
            for operation in operations:
                if operation.can_cancel and operation.status in {"pending", "running"}:
                    operation.cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
