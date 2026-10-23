# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：维护安装私有空间、上传和删除资源及配额，任务状态由应用 OperationManager 维护。
#
# 公开接口：
#   - class StagedFile — 上传与下载共用的唯一暂存和预留状态。
#   - class FileDeletion — 跟踪原目标句柄、回滚及实际移除确认。
#   - class PluginFileStorage — Worker 服务共同持有的私有文件存储。
# ============================================================

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ECL.plugins.grant_models import GrantLease
from ECL.plugins.runtime.file_replacement import FileReplacementStore
from ECL.plugins.runtime.models import FileRef
from ECL.utils import get_logger
from ECL.utils.windows_file_handles import FileBoundaryError, FileDirectory, FileHandle, FileIdentity


class _SpaceRecord(BaseModel):
    """
    区分尚未初始化和丢失/损坏空间，不根据同名目录猜测归属。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    state: Literal["preparing", "ready"]
    identity: FileIdentity | None = None


@dataclass(slots=True)
class _FileSpace:
    installation_id: str
    io_lock: RLock = field(default_factory=RLock)
    parent: FileDirectory | None = None
    directory: FileDirectory | None = None
    identity: FileIdentity | None = None


@dataclass(slots=True)
class StagedFile:
    """
    保存暂存预留、期望长度、实际偏移、原始写授权和句柄，不复制任务执行状态。
    """

    target: FileRef
    lease: GrantLease
    directory: FileDirectory
    reserved_bytes: int
    expected_bytes: int | None
    write_ref: str = field(default_factory=lambda: uuid4().hex)
    temporary_name: str = field(default_factory=lambda: f".upload-{uuid4().hex}")
    io_lock: RLock = field(default_factory=RLock)
    stream: FileHandle | None = None
    received_bytes: int = 0
    touched_at: float = field(default_factory=monotonic)
    owner: Literal["upload", "task", "cleanup", "closed"] = "upload"
    temporary_identity: FileIdentity | None = None
    expected_etag: str | None = None
    original: FileHandle | None = None


@dataclass(slots=True)
class FileDeletion:
    """
    只保存删除资源生命周期，不复制任务服务的状态或业务效果。
    """

    target: FileRef
    lease: GrantLease
    directory: FileDirectory
    root_identity: FileIdentity
    deletion_ref: str = field(default_factory=lambda: uuid4().hex)
    io_lock: RLock = field(default_factory=RLock)
    stream: FileHandle | None = None
    identity: FileIdentity | None = None
    needs_rollback: bool = False
    needs_observation: bool = False
    is_released: bool = False


class PluginFileStorage:
    """
    单个应用服务维护目录对象和限额；磁盘操作不持有全局状态锁。
    """

    max_file_bytes: ClassVar[int] = 8 * 1024 * 1024
    max_space_bytes: ClassVar[int] = 64 * 1024 * 1024
    max_run_bytes: ClassVar[int] = 32 * 1024 * 1024
    max_host_bytes: ClassVar[int] = 128 * 1024 * 1024
    max_run_uploads: ClassVar[int] = 4
    max_run_deletions: ClassVar[int] = 4
    max_host_deletions: ClassVar[int] = 32

    def __init__(self, data_path: Path) -> None:
        """
        绑定受信数据根；构造时不打开目录、不创建文件。

        :param data_path: 应用私有数据根
        """
        self._data_path = data_path.absolute()
        self._lock = RLock()
        self._bootstrap_lock = RLock()
        self._root: FileDirectory | None = None
        self._data: FileDirectory | None = None
        self._records: FileDirectory | None = None
        self._spaces: dict[str, _FileSpace] = {}
        self._uploads: dict[str, StagedFile] = {}
        self._deletions: dict[str, FileDeletion] = {}
        self._replacement_records: FileDirectory | None = None
        self.replacements: FileReplacementStore = FileReplacementStore(self)

    def replacement_records(self, *, initialize: bool = False) -> FileDirectory | None:
        """
        打开独立的替换事务目录，仅接受已接纳替换时允许首次创建。

        :param initialize: 是否允许创建缺失的事务容器
        :return: 宿主持有的目录或首次未创建时的None
        """
        with self._bootstrap_lock:
            if self._root is None:
                self._root = FileDirectory.open(self._data_path)
            if self._replacement_records is None:
                self._replacement_records = self._container("plugin_file_replacements", initialize)
            return self._replacement_records

    def _containers(self, *, initialize: bool) -> None:
        """
        在独立初始化锁中打开/创建固定宿主目录，不接收客户端路径。
        """
        with self._bootstrap_lock:
            if self._root is None:
                self._root = FileDirectory.open(self._data_path)
            if self._data is None:
                self._data = self._container("plugin_data", initialize)
            if self._records is None:
                self._records = self._container("plugin_file_spaces", initialize)

    def _container(self, name: str, initialize: bool) -> FileDirectory | None:
        """
        仅首次授权初始化允许新建固定宿主容器；其他失败不解释为目录缺失。
        """
        assert self._root is not None
        try:
            return self._root.child(name)
        except FileBoundaryError as exc:
            if exc.error_code != "PLUGIN_RESOURCE_NOT_FOUND":
                raise
            return self._root.child(name, create=True) if initialize else None

    def _record(self, installation_id: str) -> _SpaceRecord | None:
        if self._records is None:
            return None
        try:
            stream = self._records.file(f"{installation_id}.json")
        except FileBoundaryError as exc:
            if exc.error_code == "PLUGIN_RESOURCE_NOT_FOUND":
                return None
            raise
        try:
            if stream.information().size > 32768:
                raise ValueError("空间记录超限")
            record = _SpaceRecord.model_validate(
                json.loads(stream.read(0, 32769), object_pairs_hook=self._unique_fields)
            )
            if record.installation_id != installation_id:
                raise ValueError("空间主体不符")
            return record
        except (ValueError, RecursionError) as exc:
            raise FileBoundaryError("插件空间记录损坏", "PLUGIN_FILE_INVALID") from exc
        finally:
            stream.close()

    @staticmethod
    def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("空间记录字段重复")
            result[key] = value
        return result

    def _save(self, record: _SpaceRecord) -> None:
        """
        通过独立元数据目录句柄原子保存严格记录，不触碰插件目标文件。
        """
        assert self._records is not None
        self._records.save_record(f"{record.installation_id}.json", record.model_dump_json().encode())

    def _refresh(self, space: _FileSpace) -> None:
        """
        每次核对记录与实际根；损坏或目录变化不能重新绑定为首次运行。
        """
        self._containers(initialize=False)
        record = self._record(space.installation_id)
        if record is None:
            if space.directory is not None:
                raise FileBoundaryError("插件空间记录已丢失", "PLUGIN_FILE_INVALID")
            self._reject_unrecorded(space.installation_id)
            return
        if record.state != "ready" or record.identity is None:
            raise FileBoundaryError("插件空间准备未完成", "PLUGIN_STORAGE_FAILED")
        if self._data is None:
            raise FileBoundaryError("插件私有空间已丢失", "PLUGIN_FILE_INVALID")
        if space.parent is None:
            space.parent = self._data.child(space.installation_id)
        if space.directory is None:
            space.directory = space.parent.child("files")
        current = space.directory.information().identity
        if current != record.identity or (space.identity is not None and current != space.identity):
            raise FileBoundaryError("插件私有目录身份已变化", "PLUGIN_FILE_INVALID")
        space.identity = current

    def _reject_unrecorded(self, installation_id: str) -> None:
        """
        发现无记录的同名空间时明确拒绝，不能将历史或外部数据视为首次空空间。
        """
        if self._data is None:
            return
        try:
            directory = self._data.child(installation_id)
        except FileBoundaryError as exc:
            if exc.error_code == "PLUGIN_RESOURCE_NOT_FOUND":
                return
            raise
        directory.close()
        raise FileBoundaryError("未登记的插件空间不能自动接纳", "PLUGIN_FILE_INVALID")

    @contextmanager
    def access(
        self,
        installation_id: str,
        *,
        initialize: bool = False,
        admit: Callable[[], None] | None = None,
    ) -> Iterator[_FileSpace]:
        """
        按安装串行私有目录 I/O，状态短锁只用于取得对象。

        :param installation_id: 宿主活动安装身份
        :param initialize: 是否允许初始化首次空间
        :param admit: 初始化之前接受一次确定动作，回调不执行 I/O
        :return: 锁保护的空间；只读首次访问可没有物理目录
        """
        self.replacements.discover()
        with self._lock:
            space = self._spaces.setdefault(installation_id, _FileSpace(installation_id))
        with space.io_lock:
            self._refresh(space)
            if space.directory is None and initialize:
                assert admit is not None
                admit()
                self._containers(initialize=True)
                self._save(_SpaceRecord(installation_id=installation_id, state="preparing"))
                assert self._data is not None
                space.parent = self._data.child(installation_id, create=True)
                space.directory = space.parent.child("files", create=True)
                space.identity = space.directory.identity
                self._save(_SpaceRecord(installation_id=installation_id, state="ready", identity=space.identity))
            yield space

    def uploads(self, run_id: str) -> tuple[StagedFile, ...]:
        """
        返回当前 run 仍由服务持有的上传资源；调用方不得绕过其 I/O 锁。

        :param run_id: 宿主运行身份
        :return: 上传对象的固定集合
        """
        with self._lock:
            return tuple(item for item in self._uploads.values() if item.lease.run_id == run_id)

    def find(self, write_ref: str, run_id: str) -> StagedFile | None:
        """
        按引用和当前 run 查找，不透露其他来源是否存在。

        :param write_ref: 不可信随机引用
        :param run_id: 当前认证运行
        :return: 自己的资源或 None
        """
        with self._lock:
            item = self._uploads.get(write_ref)
            return item if item and item.lease.run_id == run_id else None

    def reserve(self, upload: StagedFile) -> None:
        """
        在短锁内占用精确目标及声明总量；调用方已经完成磁盘配额检查。

        :param upload: 尚未创建暂存的资源
        """
        with self._lock:
            if self.replacements.blocks(upload.lease.installation_id, upload.target.relative_path):
                raise FileBoundaryError("该目标需要完成恢复", "PLUGIN_FILE_RECOVERY_REQUIRED")
            items = tuple(self._uploads.values())
            own = tuple(item for item in items if item.lease.run_id == upload.lease.run_id)
            if len(own) >= self.max_run_uploads:
                raise FileBoundaryError("上传会话数量超限", "PLUGIN_RATE_LIMITED")
            if any(
                item.lease.installation_id == upload.lease.installation_id
                and item.target.relative_path.casefold() == upload.target.relative_path.casefold()
                for item in (*items, *self._deletions.values())
            ):
                raise FileBoundaryError("该目标正在上传", "PLUGIN_FILE_BUSY")
            if sum(item.reserved_bytes for item in own) + upload.reserved_bytes > self.max_run_bytes or (
                sum(item.reserved_bytes for item in items) + upload.reserved_bytes > self.max_host_bytes
            ):
                raise FileBoundaryError("上传预留空间不足", "PLUGIN_STORAGE_QUOTA_EXCEEDED")
            self._uploads[upload.write_ref] = upload

    def reserve_deletion(self, deletion: FileDeletion) -> None:
        """
        与上传共用精确名称占用；删除不占用上传字节配额。

        :param deletion: 已绑定当前根和原始授权的资源
        """
        with self._lock:
            if self.replacements.blocks(deletion.lease.installation_id, deletion.target.relative_path):
                raise FileBoundaryError("该目标需要完成恢复", "PLUGIN_FILE_RECOVERY_REQUIRED")
            own = sum(item.lease.run_id == deletion.lease.run_id for item in self._deletions.values())
            if own >= self.max_run_deletions or len(self._deletions) >= self.max_host_deletions:
                raise FileBoundaryError("删除资源数量超限", "PLUGIN_RATE_LIMITED")
            if any(
                item.lease.installation_id == deletion.lease.installation_id
                and item.target.relative_path.casefold() == deletion.target.relative_path.casefold()
                for item in (*self._uploads.values(), *self._deletions.values())
            ):
                raise FileBoundaryError("该目标正在处理", "PLUGIN_FILE_BUSY")
            self._deletions[deletion.deletion_ref] = deletion

    def deletions(self, run_id: str) -> tuple[FileDeletion, ...]:
        """
        返回当前运行尚未释放的删除资源。

        :param run_id: 宿主运行身份
        :return: 不可变资源集合
        """
        with self._lock:
            return tuple(item for item in self._deletions.values() if item.lease.run_id == run_id)

    def cleanup_deletion(self, deletion: FileDeletion) -> bool:
        """
        仅撤回原句柄标记或观察已关闭原对象，永不重新按路径删除。

        :param deletion: 当前服务持有的删除资源
        :return: 已确认所有资源释放；未知状态保持 False
        """
        with deletion.io_lock:
            if deletion.is_released:
                return True
            try:
                stream = deletion.stream
                if deletion.needs_rollback:
                    if stream is None or stream.is_closed:
                        return False
                    stream.mark_deletion(False)
                    if stream.deletion_pending():
                        return False
                    stream.close()
                    deletion.needs_rollback = False
                elif stream is not None and not stream.is_closed:
                    stream.close()
                if deletion.needs_observation:
                    if deletion.identity is None or deletion.directory.information().identity != deletion.root_identity:
                        return False
                    current = deletion.directory.entry_identity(deletion.target.relative_path, monotonic() + 0.1)
                    if current == deletion.identity:
                        return False
                    deletion.needs_observation = False
            except FileBoundaryError:
                return False
            with self._lock:
                self._deletions.pop(deletion.deletion_ref, None)
                deletion.is_released = True
            return True

    def check_quota(
        self,
        installation_id: str,
        directory: FileDirectory,
        deadline: float,
        additional: int = 0,
        *,
        is_new_upload: bool = False,
    ) -> None:
        """
        计入已完成与旧暂存，活跃暂存按声明总量预留；不扫描其他命名空间。

        :param installation_id: 当前安装
        :param directory: 实际私有根
        :param deadline: 枚举和核验截止时间
        :param additional: 尚未登记的新增预留
        :param is_new_upload: 本次还将新建一个暂存目录项
        """
        with self._lock:
            own = tuple(item for item in self._uploads.values() if item.lease.installation_id == installation_id)
            active_names = {item.temporary_name for item in own}
            total = sum(item.reserved_bytes for item in own) + additional
        names = directory.names(deadline)
        if is_new_upload and len(names) >= 1024:
            raise FileBoundaryError("私有目录项数超限", "PLUGIN_STORAGE_QUOTA_EXCEEDED")
        for name in names:
            if name in active_names:
                continue
            stream = directory.file(name)
            try:
                total += stream.information().size
            finally:
                stream.close()
            if total > self.max_space_bytes:
                raise FileBoundaryError("私有空间已满", "PLUGIN_STORAGE_QUOTA_EXCEEDED")
        if total > self.max_space_bytes:
            raise FileBoundaryError("私有空间已满", "PLUGIN_STORAGE_QUOTA_EXCEEDED")

    def cleanup(self, upload: StagedFile) -> bool:
        """
        实际释放唯一暂存或已发布句柄后才放开目标和配额；失败保留供重试。

        :param upload: 当前服务拥有的资源
        :return: 是否实际清理完成
        """
        replacement = self.replacements.for_upload(upload.write_ref)
        if replacement is not None:
            return self.replacements.cleanup(replacement)
        return self.cleanup_upload(upload)

    def cleanup_upload(self, upload: StagedFile) -> bool:
        """
        释放已完成其领域收尾的上传，新建或替换均仅在这里归还预留。

        :param upload: 没有待恢复备份的上传资源
        :return: 暂存或已发布句柄实际释放时为True
        """
        with upload.io_lock:
            if upload.owner == "closed":
                return True
            upload.owner = "cleanup"
            try:
                if upload.original is not None:
                    upload.original.close()
                    upload.original = None
                if upload.stream is not None:
                    if upload.stream.is_published:
                        upload.stream.close()
                    else:
                        if upload.temporary_identity is None:
                            upload.temporary_identity = upload.stream._native.information(
                                upload.stream._handle
                            ).identity
                        upload.stream.discard()
                        current = upload.directory.entry_identity(upload.temporary_name, monotonic() + 0.1)
                        if current == upload.temporary_identity:
                            return False
            except FileBoundaryError:
                get_logger("PluginFileStorage").warning("插件暂存清理未完成，保留资源等待重试")
                return False
            with self._lock:
                self._uploads.pop(upload.write_ref, None)
                upload.owner = "closed"
            return True

    def close(self) -> bool:
        """
        仅在全部上传与任务资源结束后关闭根目录句柄，不提前丢弃活动资源。

        :return: 是否关闭全部存储资源
        """
        if self.replacements.has_pending():
            return False
        with self._lock:
            if self._uploads or self._deletions:
                return False
            spaces = tuple(self._spaces.values())
        for space in spaces:
            with space.io_lock:
                if space.directory:
                    space.directory.close()
                if space.parent:
                    space.parent.close()
        with self._bootstrap_lock:
            for directory in (self._replacement_records, self._records, self._data, self._root):
                if directory is not None:
                    directory.close()
        return True
