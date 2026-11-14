# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：持有当前进程的整合包预览快照，在准备和安装之间管理临时资源与互斥。
#
# 公开接口：
#   - class PreparedModpackFile — 可展示、可校验选择的安装文件。
#   - class PreparedModpack — 服务内部的整合包准备记录。
#   - class ModpackPreparationService — 管理准备记录、过期、执行占用和释放。
# ============================================================

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock, Timer
from time import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from ECL.utils import GameServiceError, get_logger

if TYPE_CHECKING:
    from .modpack import ModpackPlan, PackFileEntry


@dataclass(frozen=True, slots=True)
class PreparedModpackFile:
    """
    为一次预览中的清单文件提供固定身份，不向前端暴露下载地址。
    """

    file_id: str
    entry: PackFileEntry

    def preview(self) -> dict[str, object]:
        """
        返回只含展示字段的文件信息。

        :return: 文件标识、相对路径、大小和客户端适用性
        """
        return {
            "fileId": self.file_id,
            "path": self.entry.target_relative,
            "size": self.entry.size,
            "requirement": self.entry.env_client,
        }


@dataclass(slots=True)
class PreparedModpack:
    """
    保存服务拥有的私有目录和计划；可变字段只由准备服务在锁内更新。
    """

    preparation_id: str
    source: str
    directory: Path
    status: Literal["preparing", "ready", "installing", "consumed", "released", "failed"] = "preparing"
    plan: ModpackPlan | None = None
    files: tuple[PreparedModpackFile, ...] = ()
    legacy_root: Path | None = None
    expires_at: float = 0
    cancel_event: Event = field(default_factory=Event)
    active: bool = False
    target_path: Path | None = None
    cleanup_attempts: int = 0
    cleanup_pending: bool = False


class ModpackPreparationService:
    """
    管理当前进程的整合包快照，不提供历史恢复或用户目录清理。

    记录和目标占用由同一把锁保护；worker 使用目录期间，释放与过期只标记
    取消，待 worker 退出后才删除私有目录。
    """

    max_records: int = 4
    idle_seconds: int = 30 * 60
    cleanup_limit: int = 3

    def __init__(self) -> None:
        """
        创建进程内记录集合；目录与到期定时器在实际准备时创建。
        """
        self._records: dict[str, PreparedModpack] = {}
        self._targets: set[Path] = set()
        self._lock = RLock()
        self._timer: Timer | None = None
        self._closed = False
        self._logger = get_logger("ModpackPreparation")

    def create(self, source: str) -> PreparedModpack:
        """
        为一次准备创建私有临时目录，达到上限时不淘汰现有预览。

        :param source: 已验证的包来源
        :return: 新的准备记录
        :raises GameServiceError: 服务已关闭或准备数量达到上限
        """
        with self._lock:
            self._expire()
            if self._closed:
                raise GameServiceError("整合包服务已关闭", "PACK_PREPARATION_NOT_READY")
            if len(self._records) >= self.max_records:
                raise GameServiceError("请先关闭其他整合包预览", "PACK_PREPARATION_LIMIT_REACHED")
            record = PreparedModpack(uuid4().hex, source, Path(tempfile.mkdtemp(prefix="ecl-pack-preview-")))
            self._records[record.preparation_id] = record
            return record

    def begin(self, record: PreparedModpack, cancel_event: Event) -> None:
        """
        将执行方的取消事件接入记录并取得目录使用权。

        :param record: 准备或安装记录
        :param cancel_event: 实际操作的取消事件
        :raises GameServiceError: 记录已被释放或服务关闭
        """
        with self._lock:
            if self._closed or record.status not in {"preparing", "installing"}:
                cancel_event.set()
                raise GameServiceError("操作已取消", "OPERATION_CANCELLED")
            if record.cancel_event.is_set():
                cancel_event.set()
            record.cancel_event = cancel_event
            record.active = True

    def complete(
        self,
        record: PreparedModpack,
        plan: ModpackPlan,
        files: tuple[PreparedModpackFile, ...],
        legacy_root: Path | None = None,
    ) -> None:
        """
        发布同一份解析快照，释放中的记录不得重新变为可安装。

        :param record: worker 当前持有的记录
        :param plan: 已解析并校验的计划
        :param files: 已确定目标路径的文件集合
        :param legacy_root: ECL 包的原有内容目录
        :raises GameServiceError: 准备已取消或释放
        """
        with self._lock:
            if record.status != "preparing" or record.cancel_event.is_set() or self._closed:
                raise GameServiceError("操作已取消", "OPERATION_CANCELLED")
            record.plan = plan
            record.files = files
            record.legacy_root = legacy_root
            record.expires_at = time() + self.idle_seconds
            record.status = "ready"
            self._schedule()

    def get(self, preparation_id: str) -> dict[str, object]:
        """
        查询安全预览并延长空闲期限，不返回内部目录或文件下载地址。

        :param preparation_id: 当前进程创建的准备标识
        :return: 准备状态与可用时的包信息
        :raises GameServiceError: 标识不存在或已过期
        """
        with self._lock:
            self._expire()
            record = self._require(preparation_id)
            if record.status == "ready":
                record.expires_at = time() + self.idle_seconds
            result: dict[str, object] = {
                "preparationId": record.preparation_id,
                "status": record.status,
                "expiresAt": record.expires_at,
                "cleanupPending": record.cleanup_pending,
            }
            if record.plan is not None:
                plan = record.plan
                result["preview"] = {
                    "name": plan.pack_name,
                    "format": plan.format_name,
                    "source": record.source,
                    "summary": plan.summary,
                    "minecraftVersion": plan.minecraft_version,
                    "loader": plan.loader_type,
                    "loaderVersion": plan.loader_version,
                    "files": [item.preview() for item in record.files],
                    "warnings": [{"code": "packWarning", "message": item} for item in plan.warnings],
                }
            self._schedule()
            return result

    def claim(self, preparation_id: str, excluded_ids: list[str], target_path: Path) -> PreparedModpack:
        """
        原子校验排除集合并占用准备记录与目标，防止跨窗口重复安装。

        :param preparation_id: 已完成准备的标识
        :param excluded_ids: 用户排除的可选文件标识
        :param target_path: 已验证的实例绝对目录
        :return: 被本次安装独占的记录
        :raises GameServiceError: 选择不合法、记录不可用或目标被占用
        """
        with self._lock:
            self._expire()
            record = self._require(preparation_id)
            if record.status == "installing":
                raise GameServiceError("此整合包正在安装", "PACK_PREPARATION_IN_USE")
            if record.status != "ready":
                raise GameServiceError("请先重新解析整合包", "PACK_PREPARATION_NOT_READY")
            optional_ids = {item.file_id for item in record.files if item.entry.env_client == "optional"}
            if len(excluded_ids) != len(set(excluded_ids)) or not set(excluded_ids) <= optional_ids:
                raise GameServiceError("只能取消本次预览中的可选文件", "PACK_FILE_SELECTION_INVALID")
            if target_path in self._targets:
                raise GameServiceError("此目标已有安装任务", "PACK_TARGET_BUSY")
            if target_path.exists():
                raise GameServiceError("目标实例已存在", "INSTANCE_ALREADY_EXISTS")
            record.status = "installing"
            record.target_path = target_path
            self._targets.add(target_path)
            self._schedule()
            return record

    def rollback_claim(self, record: PreparedModpack) -> None:
        """
        任务提交失败时恢复预览，释放尚未执行的目标占用。

        :param record: 提交尚未成功的安装记录
        """
        with self._lock:
            if record.target_path is not None:
                self._targets.discard(record.target_path)
            record.target_path = None
            record.status = "ready"
            record.expires_at = time() + self.idle_seconds
            self._schedule()

    def finish_worker(self, record: PreparedModpack, *, prepared: bool = False) -> None:
        """
        执行方退出后释放目录使用权，仅保留成功准备且仍有效的预览。

        :param record: 已结束使用目录的记录
        :param prepared: 准备操作是否成功发布预览
        """
        with self._lock:
            record.active = False
            if prepared and record.status == "ready" and not self._closed:
                return
            if record.target_path is not None:
                self._targets.discard(record.target_path)
            record.status = "consumed" if record.status == "installing" else "failed"
            self._cleanup(record)
            self._schedule()

    def release(self, preparation_id: str) -> bool:
        """
        幂等释放预览；正在安装的资源继续归实际安装操作持有。

        :param preparation_id: 要释放的预览标识
        :return: 是否已接受释放
        """
        with self._lock:
            record = self._records.get(preparation_id)
            if record is None or record.status == "installing":
                return True
            record.cancel_event.set()
            record.status = "released"
            if not record.active:
                self._cleanup(record)
            self._schedule()
            return True

    def close(self) -> None:
        """
        阻止新准备并取消执行，活跃目录由 worker 的 finally 清理。
        """
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            for record in list(self._records.values()):
                record.cancel_event.set()
                record.status = "released"
                if not record.active:
                    self._cleanup(record)

    def _require(self, preparation_id: str) -> PreparedModpack:
        record = self._records.get(preparation_id)
        if record is None:
            raise GameServiceError("整合包预览已失效，请重新解析", "PACK_PREPARATION_EXPIRED")
        return record

    def _cleanup(self, record: PreparedModpack) -> None:
        """
        只删除本服务创建的单个目录；删除失败有界重试，不触碰用户来源文件。
        """
        try:
            shutil.rmtree(record.directory)
        except FileNotFoundError:
            pass
        except OSError:
            record.cleanup_attempts += 1
            record.cleanup_pending = True
            self._logger.warning("整合包预览临时文件清理失败，重试次数：%d", record.cleanup_attempts)
            return
        self._records.pop(record.preparation_id, None)

    def _expire(self) -> None:
        """
        清理到期的空闲记录和失败清理记录，不影响正在执行的目录。
        """
        now = time()
        for record in list(self._records.values()):
            if record.active or record.status == "installing":
                continue
            if record.status == "ready" and record.expires_at <= now:
                record.status = "released"
                self._cleanup(record)
            elif record.cleanup_pending and record.cleanup_attempts < self.cleanup_limit:
                self._cleanup(record)

    def _schedule(self) -> None:
        """
        用唯一服务定时器处理下一次到期；关闭后不再创建后台计时。
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._closed:
            return
        deadlines = [item.expires_at for item in self._records.values() if item.status == "ready"]
        if any(item.cleanup_pending and item.cleanup_attempts < self.cleanup_limit for item in self._records.values()):
            deadlines.append(time() + 5)
        if deadlines:
            self._timer = Timer(max(0.05, min(deadlines) - time()), self._on_timer)
            self._timer.daemon = True
            self._timer.start()

    def _on_timer(self) -> None:
        with self._lock:
            self._timer = None
            self._expire()
            self._schedule()
