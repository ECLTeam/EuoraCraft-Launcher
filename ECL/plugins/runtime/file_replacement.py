# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：维护私有文件替换的原对象备份、持久记录与恢复，不恢复旧Worker控制权。
#
# 公开接口：
#   - class ReplacementRecord / FileReplacement — 严格持久凭据及实际资源所有者。
#   - class FileReplacementStore — 协调已接纳替换、有限收尾及可信用户恢复。
# ============================================================

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ECL.plugin_api.contracts import JsonValue
from ECL.plugins.runtime.models import FileRef
from ECL.services.operations import OperationCommitRejectedError, OperationContext, OperationManager
from ECL.utils.windows_file_handles import FileBoundaryError, FileDirectory, FileHandle, FileIdentity, FileInformation

if TYPE_CHECKING:
    from ECL.plugins.runtime.file_storage import PluginFileStorage, StagedFile


class ReplacementRecord(BaseModel):
    """
    保存一次已接受事务的对象凭据，阶段不能独立作为删除授权。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_identity: FileIdentity
    target_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    old_identity: FileIdentity
    old_etag: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_identity: FileIdentity
    new_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temporary_name: str = Field(pattern=r"^\.upload-[0-9a-f]{32}$")
    backup_name: str = Field(pattern=r"^\.replace-[0-9a-f]{32}$")
    phase: Literal["prepared", "old_saved", "published"] = "prepared"
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    resolution: Literal["restore", "save_as", "discard"] | None = None
    saved_name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    @field_validator("target_name", "saved_name")
    @classmethod
    def valid_name(cls, value: str | None) -> str | None:
        """
        持久输入使用与公开文件引用相同的设备名及尾点拒绝规则。

        :param value: 已通过平铺ASCII语法的名称
        :return: 原名称或没有另存选择时的None
        """
        return FileRef.valid_name(value) if value is not None else None

    @model_validator(mode="after")
    def validate_resolution(self) -> ReplacementRecord:
        """
        拒绝无法明确执行的持久恢复选择，不从缺失字段推断目标名称。

        :return: 通过选择与名称一致性校验的记录
        """
        if (self.resolution == "save_as") != (self.saved_name is not None):
            raise ValueError("另存选择必须且只能指定新名称")
        return self


@dataclass(slots=True)
class FileReplacement:
    """
    持有真实对象和落盘收尾，执行状态和effect仍归OperationManager。
    """

    record: ReplacementRecord
    directory: FileDirectory | None = None
    upload: StagedFile | None = None
    original: FileHandle | None = None
    temporary: FileHandle | None = None
    journal: FileHandle | None = None
    journal_identity: FileIdentity | None = None
    is_journal_verified: bool = False
    reference: str = field(default_factory=lambda: uuid4().hex)
    io_lock: RLock = field(default_factory=RLock)
    owner: Literal["worker", "cleanup"] = "cleanup"
    has_published: bool = False
    is_original_discarded: bool = False
    is_resolved: bool = False
    resolution_key: tuple[int, str, str, str | None] | None = None
    resolution_task: dict[str, str] | None = None
    resolution_manager: OperationManager | None = None


class FileReplacementStore:
    """
    仅扫描登记事务目录；未登记残留不自动清理，所有修改锚定已验证根及对象。
    """

    def __init__(self, storage: PluginFileStorage) -> None:
        """
        绑定应用唯一存储，构造不打开文件或启动执行器。

        :param storage: 空间与上传预留的权威维护者
        """
        self._storage = storage
        self._lock = RLock()
        self._discovery_lock = RLock()
        self._is_loaded = False
        self._items: dict[str, FileReplacement] = {}
        self._invalid_references: set[str] = set()
        self._resolution_receipts: dict[str, FileReplacement] = {}

    def discover(self) -> None:
        """
        首次访问仅读取当前格式记录，损坏记录保留并阻止未经恢复的修改。
        """
        with self._discovery_lock:
            if self._is_loaded:
                return
            directory = self._storage.replacement_records()
            if directory is not None:
                for name in directory.names(monotonic() + 5):
                    stream = None
                    try:
                        if not name.endswith(".json") or len(name) != 37:
                            raise ValueError("事务记录名称无效")
                        stream = directory.file(name)
                        if stream.information().size > 16384:
                            raise ValueError("事务记录超限")
                        record = self._parse(stream.read(0, 16385))
                        if name != f"{record.record_id}.json":
                            raise ValueError("事务记录主体不符")
                        with self._lock:
                            self._items[record.record_id] = FileReplacement(record)
                    except (ValueError, RecursionError, FileBoundaryError):
                        with self._lock:
                            self._invalid_references.add(uuid4().hex)
                    finally:
                        if stream is not None:
                            stream.close()
            self._is_loaded = True

    @staticmethod
    def _parse(content: bytes) -> ReplacementRecord:
        """
        拒绝重复字段和未知结构，持久输入只在边界转换为严格记录。
        """

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("恢复记录字段重复")
                result[key] = value
            return result

        return ReplacementRecord.model_validate(json.loads(content, object_pairs_hook=unique))

    def has_pending(self, installation_id: str | None = None) -> bool:
        """
        查询已经发现的真实待恢复资源，不在关闭路径隐式初始化平台文件服务。

        :param installation_id: 指定安装，省略表示所有安装
        :return: 是否存在未收尾或无法核实的记录
        """
        with self._lock:
            return bool(self._invalid_references) or any(
                not item.is_resolved and (installation_id is None or item.record.installation_id == installation_id)
                for item in self._items.values()
            )

    def blocks(self, installation_id: str, name: str) -> bool:
        """
        在存储短锁内查询目标占用，不执行I/O或反向获取存储锁。

        :param installation_id: 当前安装
        :param name: 已验证的平铺目标
        :return: 该目标是否有未结束的替换
        """
        with self._lock:
            return bool(self._invalid_references) or any(
                not item.is_resolved
                and item.record.installation_id == installation_id
                and item.record.target_name.casefold() == name.casefold()
                for item in self._items.values()
            )

    def for_upload(self, write_ref: str) -> FileReplacement | None:
        """
        查找引用同一上传资源的事务，不复制上传状态。

        :param write_ref: 宿主上传引用
        :return: 已接纳事务或None
        """
        with self._lock:
            return next(
                (
                    item
                    for item in self._items.values()
                    if item.upload is not None and item.upload.write_ref == write_ref
                ),
                None,
            )

    def _save(self, item: FileReplacement) -> None:
        """
        在原子记录保存成功后才能进行后续破坏性步骤，不写用户正文或真实路径。
        """
        directory = self._storage.replacement_records(initialize=True)
        assert directory is not None
        directory.save_record(f"{item.record.record_id}.json", item.record.model_dump_json().encode())

    def commit(
        self,
        upload: StagedFile,
        original: FileHandle,
        old: FileInformation,
        new: FileInformation,
        digest: str,
        old_digest: str,
    ) -> None:
        """
        接管已经接受提交的原对象，先保存凭据再备份及不覆盖发布。

        :param upload: 同一个已登记上传及预留
        :param original: 已完成版本核验的独占原句柄
        :param old: 同一原句柄的稳定快照
        :param new: 新暂存的稳定快照
        :param digest: 新暂存完整SHA-256
        :param old_digest: 接受前在原句柄核对的完整SHA-256
        :raises FileBoundaryError: 事务需要恢复或原生/落盘失败
        """
        assert upload.expected_etag is not None and upload.stream is not None
        record = ReplacementRecord(
            record_id=uuid4().hex,
            installation_id=upload.lease.installation_id,
            root_identity=upload.directory.identity,
            target_name=upload.target.relative_path,
            old_identity=old.identity,
            old_etag=upload.expected_etag,
            old_sha256=old_digest,
            new_identity=new.identity,
            new_sha256=digest,
            temporary_name=upload.temporary_name,
            backup_name=f".replace-{uuid4().hex}",
        )
        item = FileReplacement(record, upload.directory, upload, original, owner="worker")
        upload.temporary_identity = new.identity
        with self._lock:
            self._items[record.record_id] = item
        upload.original = None
        try:
            self._save(item)
            original.move(record.backup_name)
            item.record = item.record.model_copy(update={"phase": "old_saved", "revision": 1})
            self._save(item)
            upload.stream.publish(record.target_name)
            item.has_published = True
            item.record = item.record.model_copy(update={"phase": "published", "revision": 2})
            self._save(item)
        except Exception as exc:
            item.owner = "cleanup"
            if not item.has_published and not upload.stream.is_published and self.cleanup(item):
                raise OperationCommitRejectedError("替换未发布，原文件已保留", "PLUGIN_STORAGE_FAILED") from exc
            raise
        finally:
            item.owner = "cleanup"

    @staticmethod
    def _matches(stream: FileHandle, identity: FileIdentity, etag: str) -> bool:
        """
        从同一句柄完整核对身份和版本，不以名称、尺寸或阶段代替凭据。
        """
        before = stream.information()
        if before.identity != identity or before.size > 8388608:
            return False
        digest = stream.digest(monotonic() + 5)
        return before == stream.information() and before.etag(digest) == etag

    def _original(self, item: FileReplacement, name: str) -> FileHandle:
        """
        只打开记录证明的原对象，先登记句柄归属再执行可能失败的校验。
        """
        assert item.directory is not None
        if item.original is None or item.original.is_closed:
            item.directory.delete_target(name, lambda stream: setattr(item, "original", stream))
        assert item.original is not None
        if not self._matches(item.original, item.record.old_identity, item.record.old_etag):
            raise FileBoundaryError("原文件版本无法确认，已保留", "PLUGIN_FILE_RECOVERY_INVALID")
        return item.original

    @staticmethod
    def _matches_new(stream: FileHandle, record: ReplacementRecord) -> bool:
        """
        确认新对象身份与完整内容；异常退出关闭写句柄可能最终更新修改时间。

        恢复凭据使用原SHA-256，不把崩溃前暂存的修改时间当作关闭后的不变量。
        """
        before = stream.information()
        if not FileReplacementStore._new_identity(before.identity, record) or before.size > 8388608:
            return False
        digest = stream.digest(monotonic() + 5)
        return before == stream.information() and digest == record.new_sha256

    @staticmethod
    def _new_identity(identity: FileIdentity | None, record: ReplacementRecord) -> bool:
        """
        仅识别本次新对象发布时NTFS可能继承原名称创建时间的明确过渡。
        """
        return (
            identity is not None
            and identity.volume == record.new_identity.volume
            and (
                identity.index == record.new_identity.index
                and identity.created in {record.new_identity.created, record.old_identity.created}
            )
        )

    def _temporary_cleanup(self, item: FileReplacement) -> bool:
        """
        仅删除未发布且身份和完整版本均吻合的自身新暂存。
        """
        assert item.directory is not None
        record = item.record
        identity = item.directory.entry_identity(record.temporary_name, monotonic() + 1)
        if identity is None:
            return True
        if identity != record.new_identity:
            return False
        if item.temporary is None:
            if item.upload is not None:
                item.temporary = item.upload.stream
            else:
                item.directory.delete_target(record.temporary_name, lambda stream: setattr(item, "temporary", stream))
        assert item.temporary is not None
        if not item.temporary.is_closed:
            if not self._matches_new(item.temporary, record):
                return False
            item.temporary.mark_deletion(True)
            item.temporary.close()
        return item.directory.entry_identity(record.temporary_name, monotonic() + 1) != record.new_identity

    def _remove_journal(self, item: FileReplacement) -> bool:
        """
        业务资源收尾后才清理已核实的记录，关闭成功仍需等待原目录项消失。
        """
        directory = self._storage.replacement_records()
        if directory is None:
            return True
        name = f"{item.record.record_id}.json"
        current = directory.entry_identity(name, monotonic() + 1)
        if current is None:
            return True
        if item.journal is None:
            directory.delete_target(name, lambda stream: setattr(item, "journal", stream))
            assert item.journal is not None
            item.journal_identity = item.journal.identity
        if not item.is_journal_verified:
            assert item.journal is not None
            if item.journal.information().size > 16384:
                return False
            saved = self._parse(item.journal.read(0, 16385))
            changes = {"phase", "revision", "resolution", "saved_name"}
            if saved.model_dump(exclude=changes) != item.record.model_dump(exclude=changes):
                return False
            item.is_journal_verified = True
        if current != item.journal_identity:
            return False
        assert item.journal is not None
        if not item.journal.is_closed:
            item.journal.mark_deletion(True)
            item.journal.close()
        return directory.entry_identity(name, monotonic() + 1) != item.journal_identity

    def _settle(self, item: FileReplacement) -> bool:
        """
        按真实命名与版本判断已发布、原样保留或可恢复，不依赖记录阶段猜测。
        """
        assert item.directory is not None
        record, directory = item.record, item.directory
        target = directory.entry_identity(record.target_name, monotonic() + 1)
        backup = directory.entry_identity(record.backup_name, monotonic() + 1)
        if backup is None:
            return self._without_backup(item, target)
        if backup != record.old_identity:
            return False
        original = self._original(item, record.backup_name)
        if record.resolution == "discard":
            original.mark_deletion(True)
            original.close()
            item.is_original_discarded = True
            return directory.entry_identity(record.backup_name, monotonic() + 1) != record.old_identity
        if record.resolution == "save_as":
            assert record.saved_name is not None
            original.move(record.saved_name)
            original.close()
            return True
        if item.has_published or self._new_identity(target, record):
            if not item.has_published and not self._new_target(item):
                return False
            original.mark_deletion(True)
            original.close()
            item.is_original_discarded = True
            return directory.entry_identity(record.backup_name, monotonic() + 1) != record.old_identity
        if target is not None:
            return False
        original.move(record.target_name)
        original.close()
        return True

    def _new_target(self, item: FileReplacement) -> bool:
        """
        优先复用尚未关闭的新暂存句柄确认实际发布，避免自身DELETE访问导致重开冲突。
        """
        assert item.directory is not None
        if item.upload is not None and item.upload.stream is not None and not item.upload.stream.is_closed:
            return self._matches_new(item.upload.stream, item.record)
        stream = item.directory.file(item.record.target_name)
        try:
            return self._matches_new(stream, item.record)
        finally:
            stream.close()

    def _without_backup(self, item: FileReplacement, target: FileIdentity | None) -> bool:
        """
        备份已不存在时确认原对象仍被保留、已明确丢弃或新版本实际发布。
        """
        assert item.directory is not None
        record = item.record
        if item.original is not None and not item.original.is_closed:
            item.original.close()
        if item.is_original_discarded or item.has_published or record.resolution == "discard":
            return True
        if self._new_identity(target, record):
            return self._new_target(item)
        kept_name = record.saved_name if record.resolution == "save_as" else record.target_name
        assert kept_name is not None
        identity = item.directory.entry_identity(kept_name, monotonic() + 1)
        if identity is None or (identity.volume, identity.index) != (
            record.old_identity.volume,
            record.old_identity.index,
        ):
            return False
        item.directory.delete_target(kept_name, lambda stream: setattr(item, "original", stream))
        assert item.original is not None
        before = item.original.information()
        matches = (
            before.identity == identity
            and before.size <= 8388608
            and item.original.digest(monotonic() + 5) == record.old_sha256
            and before == item.original.information()
        )
        item.original.close()
        return matches

    def cleanup(self, item: FileReplacement) -> bool:
        """
        重试一次恢复或清理；未知对象和外部后来文件保持不动。

        :param item: 当前服务拥有的资源
        :return: 实际资源和记录均已结束
        """
        if item.owner == "worker":
            return False
        try:
            with self._storage.access(item.record.installation_id) as space, item.io_lock:
                if item.is_resolved:
                    return True
                if space.identity != item.record.root_identity or space.directory is None:
                    return False
                item.directory = space.directory
                if item.upload is not None and item.upload.stream is not None and item.upload.stream.is_published:
                    # 新文件已经正式发布，旧备份的延迟删除不应继续独占新文件。
                    item.upload.stream.close()
                if not self._settle(item) or not self._temporary_cleanup(item):
                    return False
                if item.upload is not None and item.upload.stream is not None:
                    item.upload.stream.close()
                if not self._remove_journal(item):
                    return False
                if item.upload is not None and not self._storage.cleanup_upload(item.upload):
                    return False
                item.is_resolved = True
                with self._lock:
                    self._items.pop(item.record.record_id, None)
                    if item.resolution_task is not None:
                        self._resolution_receipts[item.reference] = item
                        if len(self._resolution_receipts) > 128:
                            self._resolution_receipts.pop(next(iter(self._resolution_receipts)))
                return True
        except (FileBoundaryError, ValueError):
            return False

    def refresh(self, installation_id: str | None = None) -> None:
        """
        只观察/恢复登记资源，不重放旧任务或发布旧上传。

        :param installation_id: 可选的单个安装范围
        """
        self.discover()
        with self._lock:
            items = tuple(self._items.values())
        for item in items:
            if installation_id is not None and item.record.installation_id != installation_id:
                continue
            if item.owner != "worker" and item.io_lock.acquire(blocking=False):
                item.io_lock.release()
                self.cleanup(item)

    def _view(self, item: FileReplacement) -> dict[str, JsonValue]:
        """
        主窗口快照包含当前真实对象版本，不返回内部备份名称或宿主路径。
        """
        with self._storage.access(item.record.installation_id) as space, item.io_lock:
            if (
                item.resolution_task is not None
                and item.resolution_manager is not None
                and item.resolution_key is not None
                and item.record.revision == item.resolution_key[0]
                and item.resolution_manager.get(item.resolution_task["operationId"])["status"]
                in {"failed", "cancelled"}
            ):
                # 接受前失败的同一请求仍返回旧回执；刷新后的新版本允许用户明确重试。
                previous = item.record
                item.record = item.record.model_copy(update={"revision": item.record.revision + 1})
                try:
                    self._save(item)
                except Exception:
                    item.record = previous
                    raise
            record = item.record
            actions: list[JsonValue] = []
            facts: list[object] = [record.model_dump(mode="json")]
            state = "invalid"
            if space.identity == record.root_identity and space.directory is not None:
                item.directory = space.directory
                target = space.directory.entry_identity(record.target_name, monotonic() + 1)
                backup = space.directory.entry_identity(record.backup_name, monotonic() + 1)
                facts.extend([target.model_dump() if target else None, backup.model_dump() if backup else None])
                if target is not None:
                    current = space.directory.file(record.target_name)
                    try:
                        before = current.information()
                        digest = current.digest(monotonic() + 5)
                        if before != current.information():
                            raise FileBoundaryError("当前文件版本已变化", "PLUGIN_FILE_CHANGED")
                        facts.append(before.etag(digest))
                    finally:
                        current.close()
                if backup == record.old_identity:
                    original = self._original(item, record.backup_name)
                    facts.append(original.information().etag(original.digest(monotonic() + 5)))
                    if item.has_published or (self._new_identity(target, record) and self._new_target(item)):
                        state = "cleanup"
                    else:
                        state = "conflict" if target is not None else "pending"
                        actions = ["restore", "save_as", "discard"] if target is None else ["save_as", "discard"]
                elif item.has_published or item.is_original_discarded:
                    state = "cleanup"
            etag = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
            return {
                "recovery_ref": item.reference,
                "name": record.target_name,
                "revision": record.revision,
                "etag": etag,
                "state": state,
                "actions": actions,
            }

    def page(self, cursor: str | None = None, limit: int = 50) -> dict[str, JsonValue]:
        """
        返回当前宿主的有界恢复引用，跨重启引用及游标不能复用。

        :param cursor: 上一页返回的当前宿主引用
        :param limit: 1至100项
        :return: 恢复页面及下一游标
        """
        self.refresh()
        with self._lock:
            items = tuple(self._items.values())
            invalid = tuple(self._invalid_references)
        references = [item.reference for item in items] + list(invalid)
        start = 0
        if cursor is not None:
            if cursor not in references:
                raise FileBoundaryError("恢复游标已失效", "PLUGIN_CURSOR_INVALID")
            start = references.index(cursor) + 1
        views: list[JsonValue] = []
        for reference in references[start : start + limit]:
            item = next((value for value in items if value.reference == reference), None)
            if item is None:
                views.append(
                    {
                        "recovery_ref": reference,
                        "name": "",
                        "revision": 0,
                        "etag": "0" * 64,
                        "state": "invalid",
                        "actions": [],
                    }
                )
            elif item.owner != "worker":
                try:
                    views.append(self._view(item))
                except FileBoundaryError:
                    views.append(
                        {
                            "recovery_ref": reference,
                            "name": item.record.target_name,
                            "revision": item.record.revision,
                            "etag": "0" * 64,
                            "state": "invalid",
                            "actions": [],
                        }
                    )
        next_cursor = references[start + limit - 1] if start + limit < len(references) else None
        return {"items": views, "next_cursor": next_cursor}

    def resolve(
        self, reference: str, revision: int, etag: str, action: str, saved_name: str | None, manager: OperationManager
    ) -> dict[str, str]:
        """
        接纳主窗口已审阅选择，重试同一选择返回同一任务，执行前重新核对真实状态。

        :param reference: 当次宿主恢复引用
        :param revision: 审阅的记录版本
        :param etag: 审阅的当前对象摘要
        :param action: restore、save_as或discard
        :param saved_name: 仅save_as允许的明确新名称
        :param manager: 应用唯一任务执行器
        :return: 用户任务回执，不赋予旧Worker控制权
        """
        with self._lock:
            item = next((value for value in self._items.values() if value.reference == reference), None)
            if item is None:
                item = self._resolution_receipts.get(reference)
        if item is None:
            raise FileBoundaryError("恢复引用已失效", "PLUGIN_RESOURCE_NOT_FOUND")
        key = (revision, etag, action, saved_name)
        with item.io_lock:
            if item.resolution_task is not None:
                if item.resolution_key == key:
                    return item.resolution_task
                previous = manager.get(item.resolution_task["operationId"])
                if item.is_resolved or previous["status"] not in {"failed", "cancelled"}:
                    raise FileBoundaryError("恢复状态已变化，请刷新", "PLUGIN_FILE_CHANGED")

            def work(context: OperationContext) -> bool:
                with self._storage.access(item.record.installation_id), item.io_lock:
                    context.check_cancelled()
                    view = self._view(item)
                    if view["etag"] != etag or view["revision"] != revision or action not in view["actions"]:
                        raise FileBoundaryError("恢复状态已变化，请刷新", "PLUGIN_FILE_CHANGED")

                    def accept() -> None:
                        previous = item.record
                        item.record = item.record.model_copy(
                            update={
                                "resolution": action,
                                "saved_name": saved_name,
                                "revision": item.record.revision + 1,
                            }
                        )
                        try:
                            self._save(item)
                        except Exception:
                            item.record = previous
                            raise
                        if not self.cleanup(item):
                            raise FileBoundaryError("恢复仍需实际收尾", "PLUGIN_FILE_RECOVERY_REQUIRED")

                    context.finalize(accept)
                    return True

            item.resolution_key = key
            item.resolution_manager = manager
            submitted = manager.submit("plugin_file_recovery", work)
            # 这是稳定的接纳回执；极短任务可能在submit返回前已有实际终态。
            item.resolution_task = {"operationId": submitted["operationId"], "status": "pending"}
            return item.resolution_task
