# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：维护单次游戏安装的阶段快照，归集 Core 事实并隔离并发任务。
#
# 公开接口：
#   - class InstallationProgress — 维护阶段、修订号、取消请求及执行方终态。
#   - class InstallationSnapshot — 安装事件和整合包操作共用的快照类型。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from threading import RLock
from time import monotonic, time
from typing import Literal, TypedDict

from ECL.game import InstallEvent


class _StageProgress(TypedDict):
    """
    表示同一单位的步骤完成量，总量未知时保留空值。
    """

    unit: Literal["bytes", "files", "items", "unknown"]
    completed: int
    total: int | None


class _StageDownload(TypedDict):
    """
    保存当前下载步骤的文件计数、传输量与速度。
    """

    completedFiles: int
    totalFiles: int | None
    transferredBytes: int
    speedBytesPerSecond: int


class _InstallationStage(TypedDict):
    """
    保存有序安装步骤的状态及可量化进度。
    """

    id: str
    status: str
    action: str
    progress: _StageProgress
    download: _StageDownload | None
    startedAt: int | None
    updatedAt: int


class InstallationSnapshot(TypedDict):
    """
    定义安装事件与整合包操作共同消费的完整快照。
    """

    revision: int
    status: str
    cancellationRequested: bool
    currentStageId: str | None
    stages: list[_InstallationStage]
    errorCode: str | None
    message: str


class InstallationProgress:
    """
    维护一次安装的有序阶段，在线程间归集事实并生成独立快照。

    记录只属于当前安装，回调在锁外执行；消费者按修订号拒绝乱序投递。
    """

    def __init__(self, loader: str, include_fabric_api: bool, report: Callable[..., None]) -> None:
        """
        按加载器执行顺序建立步骤，不为未知耗时分配虚构权重。

        :param loader: 已校验的加载器类型
        :param include_fabric_api: Fabric 是否附带 API
        :param report: 安装事件或外层整合包操作的进度入口
        """
        stage_ids = ["resolve_metadata"]
        if loader in {"forge", "neoforge"}:
            stage_ids.extend(["download_client", "download_loader", "download_loader_libraries", "install_loader"])
        stage_ids.append("check_resources")
        if loader == "fabric" and include_fabric_api:
            stage_ids.append("resolve_fabric_api")
        stage_ids.extend(["download_game_files", "finalize_install"])
        self._lock = RLock()
        self._report = report
        self._last_emit = 0.0
        self._snapshot: InstallationSnapshot = {
            "revision": 0,
            "status": "pending",
            "cancellationRequested": False,
            "currentStageId": None,
            "stages": [
                {
                    "id": stage_id,
                    "status": "pending",
                    "action": "working",
                    "progress": {"unit": "unknown", "completed": 0, "total": None},
                    "download": None,
                    "startedAt": None,
                    "updatedAt": 0,
                }
                for stage_id in stage_ids
            ],
            "errorCode": None,
            "message": "",
        }

    def accept(self, event: InstallEvent) -> None:
        """
        接收 Core 的阶段事实，阶段切换立即发送，普通指标最多每秒五次。

        :param event: 当前调用产生的结构化事实
        :raises ValueError: 生产方报告不属于本次安装的阶段时抛出
        """
        with self._lock:
            if self._snapshot["status"] in {"completed", "error", "canceled"}:
                return
            stages = self._snapshot["stages"]
            stage = next((item for item in stages if item["id"] == event.stage), None)
            if stage is None:
                raise ValueError(f"安装步骤未声明：{event.stage}")
            previous_id = self._snapshot["currentStageId"]
            has_changed = (
                previous_id != event.stage or stage["status"] != event.status or stage["action"] != event.action
            )
            if previous_id != event.stage:
                index = stages.index(stage)
                for previous in stages[:index]:
                    if previous["status"] == "running":
                        previous["status"] = "completed"
                    elif previous["status"] == "pending":
                        previous["status"] = "skipped"
                    if previous["download"] is not None:
                        previous["download"]["speedBytesPerSecond"] = 0
            now = int(time() * 1000)
            stage["startedAt"] = stage["startedAt"] or now
            stage["updatedAt"] = now
            stage["status"] = event.status
            stage["action"] = event.action
            stage["progress"] = {"unit": event.unit, "completed": event.completed, "total": event.total}
            stage["download"] = None
            if event.stage.startswith("download_"):
                stage["download"] = {
                    "completedFiles": event.completed_files,
                    "totalFiles": event.total_files,
                    "transferredBytes": event.transferred_bytes,
                    "speedBytesPerSecond": event.speed if event.status == "running" else 0,
                }
            self._snapshot["status"] = "running"
            self._snapshot["currentStageId"] = event.stage
            self._snapshot["revision"] += 1
            if not has_changed and monotonic() - self._last_emit < 0.2:
                return
            snapshot = self._copy_locked()
        self._report("install", "", installation=snapshot)

    def request_cancel(self) -> None:
        """
        立即报告取消请求，最终状态仍由实际执行方结束后确认。
        """
        with self._lock:
            if self._snapshot["status"] in {"completed", "error", "canceled"}:
                return
            self._snapshot["cancellationRequested"] = True
            self._snapshot["revision"] += 1
            snapshot = self._copy_locked()
        self._report("install", "", installation=snapshot)

    def finish(self, status: str, message: str, error_code: str | None = None) -> None:
        """
        在执行方退出后提交最终结果，并结束活动步骤和速度。

        :param status: completed、error 或 canceled
        :param message: 最终结果说明
        :param error_code: 失败或取消的稳定错误码
        """
        with self._lock:
            if self._snapshot["status"] in {"completed", "error", "canceled"}:
                return
            self._snapshot["status"] = status
            self._snapshot["message"] = message
            self._snapshot["errorCode"] = error_code
            self._snapshot["revision"] += 1
            for stage in self._snapshot["stages"]:
                if stage["status"] == "running":
                    stage["status"] = status
                elif stage["status"] == "pending" and status == "completed":
                    stage["status"] = "skipped"
                if stage["download"] is not None:
                    stage["download"]["speedBytesPerSecond"] = 0
            snapshot = self._copy_locked()
        self._report("done" if status == "completed" else "error", message, installation=snapshot)

    def _copy_locked(self) -> InstallationSnapshot:
        self._last_emit = monotonic()
        return deepcopy(self._snapshot)
