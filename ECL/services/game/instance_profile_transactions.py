# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：固定共享实例资料的原生目录对象并协调不覆盖提交及登记恢复。
#
# 公开接口：
#   - class InstanceProfileTarget — 宿主确认的根、实例及物理授权身份。
#   - class NativeProfileTarget — 相对固定目录打开资料，持有明确关闭归属。
#   - class InstanceProfileTransactions — 拥有实际资料事务、凭据及可信恢复。
# ============================================================

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ECL.plugin_api.contracts import JsonValue
from ECL.services.operations import OperationCommitRejectedError, OperationContext, OperationManager
from ECL.utils.instance_reading import DirectoryIdentity, InstanceReadBoundary, InstanceReadError
from ECL.utils.windows_file_handles import FileBoundaryError, FileDirectory, FileHandle, FileIdentity

from .instance_profile_models import InstanceProfilePatch, InstanceProfileSnapshot, read_profile_snapshot


@dataclass(frozen=True, slots=True)
class InstanceProfileTarget:
    """
    由宿主资源目录构造共享实例目标，Worker不能传入路径或物理身份。
    """

    root_path: Path
    version_id: str
    root_identity: DirectoryIdentity
    instance_identity: DirectoryIdentity


@dataclass(slots=True)
class NativeProfileTarget:
    """
    按创建顺序登记目录，失败关闭也保留对象以供真正收尾。
    """

    target: InstanceProfileTarget
    directories: list[FileDirectory] = field(default_factory=list)
    profile_directory: FileDirectory | None = None

    @staticmethod
    def identity(directory: FileDirectory) -> DirectoryIdentity:
        """
        将同一句柄交给当前Python解析目录身份，不依赖原生索引布局猜测。

        :param directory: 已经固定且无重解析的目录对象
        :return: 和资源目录使用同一模型的身份
        """
        info = directory.stat_information()
        return DirectoryIdentity(device=info.st_dev, inode=info.st_ino, created_ns=info.st_ctime_ns)

    def open(self, *, initialize: bool = False, admit: Callable[[], None] | None = None) -> None:
        """
        逐层相对固定根及实例；只有明确接受准备动作后才允许创建缺少的.ecl。

        :param initialize: 是否允许首次创建资料目录
        :param admit: 创建前重新校验调用准入的受信回调
        :raises InstanceReadError: 授权目录已变化或目标不是单个合法实例名
        :raises FileBoundaryError: 原生对象不可用，已登记目录仍由本对象负责关闭
        """
        name = self.target.version_id
        if not name or len(name) > 64 or name in {".", ".."} or name.endswith((".", " ")):
            raise InstanceReadError("实例标识无效", "PLUGIN_INSTANCE_METADATA_INVALID")
        FileDirectory._component(name)
        root = FileDirectory.open(self.target.root_path)
        self.directories.append(root)
        if self.identity(root) != self.target.root_identity:
            raise InstanceReadError("游戏目录身份已经变化")
        versions = root.child("versions")
        self.directories.append(versions)
        instance = versions.child(name)
        self.directories.append(instance)
        if self.identity(instance) != self.target.instance_identity:
            raise InstanceReadError("实例身份已经变化")
        try:
            directory = instance.child(".ecl")
        except FileBoundaryError as exc:
            if exc.error_code != "PLUGIN_RESOURCE_NOT_FOUND" or not initialize:
                raise
            if admit is None:
                raise InstanceReadError("资料初始化没有准入", "PLUGIN_PERMISSION_DENIED") from exc
            admit()
            directory = instance.child(".ecl", create=True)
        self.directories.append(directory)
        self.profile_directory = directory

    def close(self) -> bool:
        """
        只关闭本对象实际拥有的目录，关闭失败时保留归属供重试。

        :return: 全部目录句柄实际关闭
        """
        while self.directories:
            try:
                self.directories[-1].close()
            except FileBoundaryError:
                return False
            self.directories.pop()
        self.profile_directory = None
        return True


class _ProfileRecord(BaseModel):
    """
    登记一次有限共享资料提交的完整对象凭据，阶段不能单独授权删除。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    record_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_path: str = Field(min_length=1, max_length=32768)
    version_id: str = Field(min_length=1, max_length=64)
    python_root: DirectoryIdentity
    python_instance: DirectoryIdentity
    directories: tuple[FileIdentity, ...] = Field(min_length=4, max_length=4)
    old_identity: FileIdentity | None
    old_etag: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    old_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    new_identity: FileIdentity
    new_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temporary_name: str = Field(pattern=r"^\.profile-new-[0-9a-f]{32}$")
    backup_name: str = Field(pattern=r"^\.profile-old-[0-9a-f]{32}$")
    phase: Literal["prepared", "old_saved", "published"] = "prepared"
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    resolution: Literal["restore", "save_as", "discard"] | None = None
    saved_suffix: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def validate_record(self) -> _ProfileRecord:
        """
        只接受明确存在状态、完整旧凭据及合法共享实例组件。
        """
        FileDirectory._component(self.version_id)
        if self.version_id.endswith((".", " ")) or not Path(self.root_path).is_absolute():
            raise ValueError("资料位置无效")
        if (self.old_identity is None) != (self.old_etag is None) or (self.old_identity is None) != (
            self.old_sha256 is None
        ):
            raise ValueError("原资料凭据不完整")
        if (self.resolution == "save_as") != (self.saved_suffix is not None):
            raise ValueError("另存选择不完整")
        return self


@dataclass(slots=True)
class _ProfileTransaction:
    """
    持有真实目录、文件及通知收尾，不复制应用执行器的任务状态。
    """

    target: InstanceProfileTarget
    installation_id: str
    reference: str = field(default_factory=lambda: uuid4().hex)
    native: NativeProfileTarget | None = None
    record: _ProfileRecord | None = None
    original: FileHandle | None = None
    stream: FileHandle | None = None
    journal: FileHandle | None = None
    journal_identity: FileIdentity | None = None
    is_journal_verified: bool = False
    owner: Literal["queued", "worker", "cleanup"] = "queued"
    has_published: bool = False
    is_settled: bool = False
    needs_notification: bool = False
    has_notification_error: bool = False
    temporary_name: str = field(default_factory=lambda: ".profile-new-" + uuid4().hex)
    temporary_identity: FileIdentity | None = None
    decision_key: tuple[int, str, str, str | None] | None = None
    receipt: dict[str, str] | None = None
    manager: OperationManager | None = None


class InstanceProfileTransactions:
    """
    用资料存储的同一锁拥有共享文件事务，任务执行状态只由OperationManager维护。
    """

    def __init__(self, data_path: Path, lock: RLock, on_changed: Callable[[Path], None] | None = None) -> None:
        """
        绑定唯一资料存储，构造不打开目录或执行恢复。

        :param data_path: 宿主数据根，不能来自插件请求
        :param lock: InstanceProfileStore拥有的资料锁
        :param on_changed: 实际收尾后的缓存与通知入口，失败只重试通知
        """
        self._data_path = data_path.absolute()
        self._lock = lock
        self._on_changed = on_changed
        self._items: dict[str, _ProfileTransaction] = {}
        self._invalid: set[str] = set()
        self._receipts: dict[str, _ProfileTransaction] = {}
        self._records: FileDirectory | None = None
        self._data: FileDirectory | None = None
        self._is_loaded = False

    def _directory(self, initialize: bool = False) -> FileDirectory | None:
        """
        仅接受已获准事务初始化宿主固定记录目录，读取不创建首次数据。
        """
        if self._records is not None:
            return self._records
        records_path = self._data_path / "instance_profile_transactions"
        if not initialize and not records_path.exists() and not records_path.is_symlink():
            return None
        if not self._data_path.exists():
            if not initialize:
                return None
            self._data_path.mkdir(parents=True, exist_ok=True)
        if self._data is None:
            self._data = FileDirectory.open(self._data_path)
        try:
            self._records = self._data.child("instance_profile_transactions")
        except FileBoundaryError as exc:
            if exc.error_code != "PLUGIN_RESOURCE_NOT_FOUND":
                raise
            if initialize:
                self._records = self._data.child("instance_profile_transactions", create=True)
        return self._records

    @staticmethod
    def _parse(content: bytes) -> _ProfileRecord:
        """
        严格解析持久边界，拒绝重复字段及隐式结构转换。
        """

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError("事务字段重复")
                result[name] = value
            return result

        return _ProfileRecord.model_validate_json(json.dumps(json.loads(content, object_pairs_hook=unique)))

    def discover(self) -> None:
        """
        只发现当前格式登记记录，不恢复旧任务或扫描游戏目录残留。
        """
        with self._lock:
            if self._is_loaded or sys.platform != "win32":
                return
            directory = self._directory()
            if directory is not None:
                for name in directory.names(monotonic() + 5):
                    stream = None
                    try:
                        stream = directory.file(name)
                        if stream.information().size > 65536:
                            raise ValueError("事务记录超限")
                        record = self._parse(stream.read(0, 65537))
                        if name != record.record_id + ".json":
                            raise ValueError("事务名称不符")
                        target = InstanceProfileTarget(
                            Path(record.root_path), record.version_id, record.python_root, record.python_instance
                        )
                        item = _ProfileTransaction(
                            target,
                            record.installation_id,
                            record=record,
                            owner="cleanup",
                            temporary_name=record.temporary_name,
                            temporary_identity=record.new_identity,
                            needs_notification=True,
                        )
                        self._items[item.reference] = item
                    except (ValueError, RecursionError, FileBoundaryError):
                        self._invalid.add(uuid4().hex)
                    finally:
                        if stream is not None:
                            stream.close()
            self._is_loaded = True

    def has_pending(
        self, root_path: Path | None = None, version_id: str | None = None, installation_id: str | None = None
    ) -> bool:
        """
        查询实际未收尾资源；未知记录保持拒绝，不能从缺名推导首次未设置。

        :param root_path: 可选的具体根范围
        :param version_id: 可选的实例名称
        :param installation_id: 生命周期使用的原安装范围
        :return: 此范围仍需收尾或可信恢复
        """
        self.discover()
        with self._lock:
            return bool(self._invalid) or any(
                (root_path is None or item.target.root_path == root_path)
                and (version_id is None or item.target.version_id.casefold() == version_id.casefold())
                and (installation_id is None or item.installation_id == installation_id)
                for item in self._items.values()
            )

    def reserve(self, target: InstanceProfileTarget, installation_id: str) -> _ProfileTransaction:
        """
        在任务入队前占用同一资料，注册失败由调用方释放，不修改游戏目录。

        :param target: 宿主已解析的实际资源
        :param installation_id: 由宿主绑定的安装身份
        :return: 任务必须持有并最终收尾的资源
        :raises InstanceReadError: 待恢复、同目标占用或容量耗尽
        """
        with self._lock:
            if self.has_pending(target.root_path, target.version_id):
                raise InstanceReadError("实例资料正在处理或等待恢复", "PLUGIN_INSTANCE_PROFILE_RECOVERY_REQUIRED")
            if len(self._items) >= 32:
                raise InstanceReadError("实例资料任务资源已满", "PLUGIN_RATE_LIMITED")
            item = _ProfileTransaction(target, installation_id)
            self._items[item.reference] = item
            return item

    def _save(self, item: _ProfileTransaction) -> None:
        """
        原子保存完整凭据之后才允许移动正式原资料。
        """
        assert item.record is not None
        directory = self._directory(True)
        assert directory is not None
        content = item.record.model_dump_json().encode()
        if len(content) > 65536:
            raise InstanceReadError("事务记录超限", "PLUGIN_MESSAGE_TOO_LARGE")
        directory.save_record(item.record.record_id + ".json", content)

    @staticmethod
    def _matches(stream: FileHandle, identity: FileIdentity, digest: str, etag: str | None = None) -> bool:
        """
        用同一句柄验证普通对象、完整内容及可选的严格旧版本。
        """
        before = stream.information()
        if before.identity != identity or before.size > 65536:
            return False
        actual = stream.digest(monotonic() + 5, 65536)
        return before == stream.information() and actual == digest and (etag is None or before.etag(actual) == etag)

    @staticmethod
    def _new_matches(stream: FileHandle, record: _ProfileRecord) -> bool:
        """
        仅允许本次发布的新对象继承原名称创建时间，仍核对卷、文件ID及完整摘要。
        """
        identity = stream.information().identity
        created = {record.new_identity.created}
        if record.old_identity is not None:
            created.add(record.old_identity.created)
        if (identity.volume, identity.index) != (
            record.new_identity.volume,
            record.new_identity.index,
        ) or identity.created not in created:
            return False
        return InstanceProfileTransactions._matches(stream, identity, record.new_sha256)

    def patch(
        self,
        item: _ProfileTransaction,
        expected_etag: str,
        patch: InstanceProfilePatch,
        context: OperationContext,
        deadline: float,
        admit: Callable[[], None],
    ) -> InstanceProfileSnapshot | None:
        """
        在资料唯一锁内核对完整版本并接受有限提交，不返回原始资料或真实路径。

        :param item: 入队时登记的同一个资源
        :param expected_etag: 插件读取时的完整资料版本
        :param patch: 已严格验证的四字段增量
        :param context: 应用唯一执行器提供的真实取消及提交边界
        :param deadline: 从提交开始计算的总截止
        :param admit: 原始双许可的有限准备准入
        :return: 已发布后的稳定快照；后来竞争导致无法确认时隐藏结果
        :raises InstanceReadError: 版本、范围、格式或期限不满足
        """
        with self._lock:
            item.owner = "worker"
            try:
                context.check_cancelled()
                reader = InstanceReadBoundary(item.target.root_path, deadline)
                before = read_profile_snapshot(reader, item.target.version_id)
                if (
                    reader.identity != item.target.root_identity
                    or before.instance_identity != item.target.instance_identity
                ):
                    raise InstanceReadError("实例资源身份已变化")
                if before.etag != expected_etag:
                    raise InstanceReadError("实例资料版本已变化", "PLUGIN_INSTANCE_PROFILE_CONFLICT")
                document = {**before.document, **patch.model_dump(exclude_unset=True)}
                content = json.dumps(document, ensure_ascii=False, indent=2).encode()
                if len(content) > 65536:
                    raise InstanceReadError("实例资料超过写入上限", "PLUGIN_MESSAGE_TOO_LARGE")
                item.native = NativeProfileTarget(item.target)
                item.native.open(initialize=True, admit=admit)
                directory = item.native.profile_directory
                assert directory is not None
                if (
                    before.directory_identity is not None
                    and NativeProfileTarget.identity(directory) != before.directory_identity
                ):
                    raise InstanceReadError("实例资料目录已变化")
                if before.file_version is not None:
                    original = directory.delete_target(
                        "instance.json", lambda stream: setattr(item, "original", stream)
                    )
                    if original.information().attributes & 1:
                        raise InstanceReadError("原实例资料为只读文件", "PLUGIN_INSTANCE_PROFILE_WRITE_FAILED")
                    info = original.stat_information()
                    version = (
                        info.st_dev,
                        info.st_ino,
                        info.st_ctime_ns,
                        info.st_mtime_ns,
                        info.st_size,
                        info.st_nlink,
                    )
                    if version != before.file_version or original.digest(deadline, 65536) != before.sha256:
                        raise InstanceReadError("实例资料版本已变化", "PLUGIN_INSTANCE_PROFILE_CONFLICT")
                elif directory.entry_identity("instance.json", deadline) is not None:
                    raise InstanceReadError("实例资料已被创建", "PLUGIN_INSTANCE_PROFILE_CONFLICT")
                stream = directory.file(
                    item.temporary_name, create=True, on_create=lambda value: setattr(item, "stream", value)
                )
                item.temporary_identity = stream.identity
                stream.write(0, content)
                stream.flush()
                context.check_cancelled()
                admit()
                if monotonic() >= deadline:
                    raise InstanceReadError("实例资料任务已超时", "PLUGIN_DEADLINE_EXCEEDED")
                context.finalize(lambda: self._commit(item, before, hashlib.sha256(content).hexdigest()))
                try:
                    published = read_profile_snapshot(
                        InstanceReadBoundary(item.target.root_path, deadline), item.target.version_id
                    )
                    if published.sha256 == hashlib.sha256(content).hexdigest():
                        return published
                except InstanceReadError:
                    pass  # 发布已完成，结果无法确认只隐藏视图，不能重写或回滚业务。
                return None
            finally:
                item.owner = "cleanup"

    def _commit(self, item: _ProfileTransaction, before: InstanceProfileSnapshot, digest: str) -> None:
        """
        先持久登记，再按原句柄备份和不覆盖发布；失败不将原资料交给discard。
        """
        assert item.native is not None and item.stream is not None
        old = item.original.information() if item.original else None
        item.record = _ProfileRecord(
            record_id=uuid4().hex,
            installation_id=item.installation_id,
            root_path=str(item.target.root_path),
            version_id=item.target.version_id,
            python_root=item.target.root_identity,
            python_instance=item.target.instance_identity,
            directories=tuple(directory.identity for directory in item.native.directories),
            old_identity=old.identity if old else None,
            old_etag=old.etag(before.sha256) if old else None,
            old_sha256=before.sha256,
            new_identity=item.stream.information().identity,
            new_sha256=digest,
            temporary_name=item.temporary_name,
            backup_name=".profile-old-" + uuid4().hex,
        )
        try:
            self._save(item)
            if item.original is not None:
                item.original.move(item.record.backup_name)
                item.needs_notification = True
                item.record = item.record.model_copy(update={"phase": "old_saved", "revision": 1})
                self._save(item)
            item.stream.publish("instance.json")
            item.has_published = True
            item.needs_notification = True
            item.record = item.record.model_copy(update={"phase": "published", "revision": 2})
            self._save(item)
            item.stream.close()
        except Exception as exc:
            if not item.has_published and not item.stream.is_published:
                has_cleaned = self._cleanup_locked(item)
                if has_cleaned and not item.has_published:
                    raise OperationCommitRejectedError(
                        "实例资料未发布，原资料已保留", "PLUGIN_INSTANCE_PROFILE_WRITE_FAILED"
                    ) from exc
            raise

    def _reopen(self, item: _ProfileTransaction) -> FileDirectory:
        """
        新宿主仅按完整目录凭据重新固定原目标，不因路径相同重新授权。
        """
        assert item.record is not None
        if item.native is None or item.native.profile_directory is None:
            if item.native is not None and not item.native.close():
                raise FileBoundaryError("资料目录等待关闭", "PLUGIN_FILE_BUSY")
            item.native = NativeProfileTarget(item.target)
            item.native.open()
        if tuple(directory.identity for directory in item.native.directories) != item.record.directories:
            raise InstanceReadError("恢复资料目录身份不符", "PLUGIN_INSTANCE_PROFILE_RECOVERY_INVALID")
        assert item.native.profile_directory is not None
        return item.native.profile_directory

    def _old(self, item: _ProfileTransaction, directory: FileDirectory) -> FileHandle:
        """
        先登记句柄再验证原备份，未知对象不提供破坏性动作。
        """
        record = item.record
        assert record is not None and record.old_identity is not None and record.old_sha256 is not None
        if item.original is None or item.original.is_closed:
            directory.delete_target(record.backup_name, lambda stream: setattr(item, "original", stream))
        assert item.original is not None
        if not self._matches(item.original, record.old_identity, record.old_sha256, record.old_etag):
            raise InstanceReadError("原资料备份无法核实", "PLUGIN_INSTANCE_PROFILE_RECOVERY_INVALID")
        return item.original

    def _settle(self, item: _ProfileTransaction, directory: FileDirectory) -> bool:
        """
        按真实对象恢复旧资料或清理已发布备份，不继续旧上传或覆盖后来文件。
        """
        record = item.record
        assert record is not None
        backup = directory.entry_identity(record.backup_name, monotonic() + 1)
        target = directory.entry_identity("instance.json", monotonic() + 1)
        if backup is not None:
            if backup != record.old_identity:
                return False
            original = self._old(item, directory)
            if record.resolution == "discard":
                original.mark_deletion(True)
                original.close()
                return directory.entry_identity(record.backup_name, monotonic() + 1) != record.old_identity
            if record.resolution == "save_as":
                original.move("recovered-profile-" + record.saved_suffix + ".json")
                original.close()
                item.needs_notification = True
                return True
            if item.has_published or self._is_new_target(directory, record):
                original.mark_deletion(True)
                original.close()
                return directory.entry_identity(record.backup_name, monotonic() + 1) != record.old_identity
            if target is not None:
                return False
            original.move("instance.json")
            original.close()
            item.needs_notification = True
            return True
        if item.original is not None and not item.original.is_closed:
            item.original.close()
        if record.old_identity is None or record.resolution == "discard" or item.has_published:
            return True
        if self._is_new_target(directory, record):
            return True
        name = (
            "recovered-profile-" + record.saved_suffix + ".json" if record.resolution == "save_as" else "instance.json"
        )
        identity = directory.entry_identity(name, monotonic() + 1)
        if identity is None or (identity.volume, identity.index) != (
            record.old_identity.volume,
            record.old_identity.index,
        ):
            return False
        directory.delete_target(name, lambda stream: setattr(item, "original", stream))
        assert item.original is not None and record.old_sha256 is not None
        # 恢复到曾被外部占位的名称可能继承创建时间；这里只证明原数据仍被保留，不删除它。
        matches = self._matches(item.original, identity, record.old_sha256)
        item.original.close()
        return matches

    @staticmethod
    def _is_new_target(directory: FileDirectory, record: _ProfileRecord) -> bool:
        """
        只用实际新对象及完整内容证明曾经发布，阶段记录本身不是清理授权。
        """
        if directory.entry_identity("instance.json", monotonic() + 1) is None:
            return False
        stream = directory.file("instance.json")
        try:
            return InstanceProfileTransactions._new_matches(stream, record)
        finally:
            stream.close()

    def _remove_record(self, item: _ProfileTransaction) -> bool:
        """
        只有资料和暂存实际收尾后删除同一完整记录，关闭成功后仍确认目录项消失。
        """
        assert item.record is not None
        directory = self._directory()
        if directory is None:
            return True
        name = item.record.record_id + ".json"
        current = directory.entry_identity(name, monotonic() + 1)
        if current is None:
            return True
        if item.journal is None:
            directory.delete_target(name, lambda stream: setattr(item, "journal", stream))
            item.journal_identity = item.journal.identity
        if current != item.journal_identity:
            return False
        if not item.is_journal_verified:
            if item.journal.information().size > 65536:
                return False
            saved = self._parse(item.journal.read(0, 65537))
            changes = {"phase", "revision", "resolution", "saved_suffix"}
            if saved.model_dump(exclude=changes) != item.record.model_dump(exclude=changes):
                return False
            item.is_journal_verified = True
        if not item.journal.is_closed:
            item.journal.mark_deletion(True)
            item.journal.close()
        return directory.entry_identity(name, monotonic() + 1) != item.journal_identity

    def _cleanup_temporary(self, item: _ProfileTransaction, directory: FileDirectory) -> bool:
        """
        仅删除本事务登记的暂存对象，实际目录项消失才归还资源。
        """
        identity = directory.entry_identity(item.temporary_name, monotonic() + 1)
        if identity is not None:
            expected = item.record.new_identity if item.record else item.temporary_identity
            if identity != expected:
                return False
            if item.stream is None:
                directory.delete_target(item.temporary_name, lambda stream: setattr(item, "stream", stream))
            if not item.stream.is_closed:
                if item.record and not self._matches(item.stream, expected, item.record.new_sha256):
                    return False
                item.stream.mark_deletion(True)
                item.stream.close()
            if directory.entry_identity(item.temporary_name, monotonic() + 1) == expected:
                return False
        return True

    def _observe_publication(self, item: _ProfileTransaction, directory: FileDirectory) -> None:
        """
        原生发布确认丢失时，从仍持有的新对象及目标目录项证明真实效果，绝不按阶段猜测。
        """
        if item.stream is None or item.stream.is_closed or item.record is None:
            return
        identity = item.stream.information().identity
        if directory.entry_identity("instance.json", monotonic() + 1) == identity and self._new_matches(
            item.stream, item.record
        ):
            item.has_published = True
            item.needs_notification = True
            item.stream.close()

    def _cleanup_locked(self, item: _ProfileTransaction) -> bool:
        """
        持有资料锁时只处理实际对象，缓存通知由锁外的cleanup单独重试。
        """
        if item.is_settled:
            return True
        try:
            if item.stream is not None and item.stream.is_published:
                item.needs_notification = True
                item.stream.close()
            directory = item.native.profile_directory if item.native else None
            if item.record is not None:
                directory = self._reopen(item)
                self._observe_publication(item, directory)
                if not self._settle(item, directory):
                    return False
            if item.original is not None and not item.original.is_closed:
                item.original.close()
            if directory is not None and not self._cleanup_temporary(item, directory):
                return False
            if item.stream is not None:
                item.stream.close()
            if item.record is not None and not self._remove_record(item):
                return False
            if item.native is not None and not item.native.close():
                return False
            item.is_settled = True
            return True
        except (FileBoundaryError, InstanceReadError, ValueError):
            return False

    def cleanup(self, item: _ProfileTransaction) -> bool:
        """
        原对象、暂存、记录及目录句柄都结束后归还占用，通知失败仅重试通知。

        :param item: 原任务或可信恢复持有的同一资源
        :return: 实际资源与缓存通知均结束
        """
        with self._lock:
            if item.owner == "worker" or not self._cleanup_locked(item):
                return False
        if item.needs_notification and self._on_changed is not None:
            try:
                self._on_changed(item.target.root_path)
            except Exception:
                # 这是外部事件边界，业务提交已经完成，不能为通知失败重做写入。
                from ECL.utils import get_logger

                if not item.has_notification_error:
                    get_logger("InstanceProfileTransactions").warning("实例资料已保存，缓存通知等待重试")
                    item.has_notification_error = True
                return False
        with self._lock:
            item.needs_notification = False
            self._items.pop(item.reference, None)
            if item.receipt is not None:
                self._receipts[item.reference] = item
                if len(self._receipts) > 128:
                    self._receipts.pop(next(iter(self._receipts)))
            return True

    def refresh(self, installation_id: str | None = None) -> None:
        """
        只重试登记收尾，不创建旧Worker任务或重放资料修改。

        :param installation_id: 生命周期可限定原安装范围
        """
        self.discover()
        with self._lock:
            items = tuple(self._items.values())
        for item in items:
            if item.owner == "cleanup" and (installation_id is None or item.installation_id == installation_id):
                self.cleanup(item)

    def _view(self, item: _ProfileTransaction) -> dict[str, JsonValue]:
        """
        为主窗口构造当前实际对象快照，不投影路径、JSON内容或内部名称。
        """
        record = item.record
        assert record is not None
        if (
            item.receipt
            and item.manager
            and item.decision_key
            and record.revision == item.decision_key[0]
            and (item.manager.get(item.receipt["operationId"])["status"] in {"failed", "cancelled"})
        ):
            previous = record
            item.record = record.model_copy(update={"revision": record.revision + 1})
            try:
                self._save(item)
            except Exception:
                item.record = previous
                raise
            record = item.record
        facts: list[object] = [record.model_dump(mode="json")]
        actions: list[JsonValue] = []
        state = "invalid"
        if item.is_settled or item.has_published:
            state = "cleanup"
        else:
            directory = self._reopen(item)
            target = directory.entry_identity("instance.json", monotonic() + 1)
            facts.append(target.model_dump() if target else None)
            if target is not None:
                stream = directory.file("instance.json")
                try:
                    before = stream.information()
                    digest = stream.digest(monotonic() + 5, 65536)
                    if before != stream.information():
                        raise InstanceReadError("当前资料已变化")
                    facts.append(before.etag(digest))
                finally:
                    stream.close()
            if (
                record.old_identity is not None
                and directory.entry_identity(record.backup_name, monotonic() + 1) == record.old_identity
            ):
                original = self._old(item, directory)
                facts.append(original.information().etag(original.digest(monotonic() + 5, 65536)))
                if item.has_published or self._is_new_target(directory, record):
                    state = "cleanup"
                else:
                    state = "pending" if target is None else "conflict"
                    actions = ["restore", "save_as", "discard"] if target is None else ["save_as", "discard"]
        return {
            "recovery_ref": item.reference,
            "name": record.version_id,
            "revision": record.revision,
            "etag": hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest(),
            "state": state,
            "actions": actions,
        }

    def page(self, cursor: str | None = None, limit: int = 50) -> dict[str, JsonValue]:
        """
        有界查询全部登记资料恢复，包含已卸载插件或不再配置的原目录。

        :param cursor: 当前宿主的分页引用
        :param limit: 1至100项
        :return: 脱敏的恢复页面
        """
        self.refresh()
        with self._lock:
            references = [
                item.reference for item in self._items.values() if item.owner == "cleanup" and item.record
            ] + sorted(self._invalid)
            if cursor is not None and cursor not in references:
                raise InstanceReadError("恢复游标已失效", "PLUGIN_CURSOR_INVALID")
            start = references.index(cursor) + 1 if cursor else 0
            rows: list[JsonValue] = []
            for reference in references[start : start + limit]:
                item = self._items.get(reference)
                try:
                    row = self._view(item) if item else None
                except (FileBoundaryError, InstanceReadError, ValueError):
                    row = None
                rows.append(
                    row
                    or {
                        "recovery_ref": reference,
                        "name": item.target.version_id if item else "",
                        "revision": item.record.revision if item and item.record else 0,
                        "etag": "0" * 64,
                        "state": "invalid",
                        "actions": [],
                    }
                )
            next_cursor = references[start + limit - 1] if start + limit < len(references) else None
            return {"items": rows, "next_cursor": next_cursor}

    def resolve(
        self, reference: str, revision: int, etag: str, action: str, suffix: str | None, manager: OperationManager
    ) -> dict[str, str]:
        """
        接纳可信主窗口的原对象恢复选择；重复相同审阅返回原接纳回执。

        :param reference: 当前宿主的恢复引用
        :param revision: 审阅时的记录版本
        :param etag: 审阅时实际对象摘要
        :param action: restore、save_as或discard
        :param suffix: 另存受限文件名的显式后缀
        :param manager: 应用唯一任务执行器
        :return: 稳定的接纳回执，实际结果由任务查询确认
        """
        with self._lock:
            item = self._items.get(reference) or self._receipts.get(reference)
            if item is None or item.record is None:
                raise InstanceReadError("恢复引用已失效", "PLUGIN_RESOURCE_NOT_FOUND")
            key = (revision, etag, action, suffix)
            if item.receipt:
                if item.decision_key == key:
                    return item.receipt
                if item.is_settled or manager.get(item.receipt["operationId"])["status"] not in {"failed", "cancelled"}:
                    raise InstanceReadError("恢复状态已变化", "PLUGIN_INSTANCE_PROFILE_CONFLICT")

            def work(context: OperationContext) -> bool:
                with self._lock:
                    context.check_cancelled()
                    view = self._view(item)
                    if view["revision"] != revision or view["etag"] != etag or action not in view["actions"]:
                        raise InstanceReadError("恢复状态已变化", "PLUGIN_INSTANCE_PROFILE_CONFLICT")

                    def accept() -> None:
                        previous = item.record
                        item.record = _ProfileRecord.model_validate(
                            {
                                **previous.model_dump(),
                                "resolution": action,
                                "saved_suffix": suffix,
                                "revision": previous.revision + 1,
                            }
                        )
                        try:
                            self._save(item)
                        except Exception:
                            item.record = previous
                            raise
                        if not self._cleanup_locked(item):
                            raise InstanceReadError("资料恢复等待实际收尾", "PLUGIN_INSTANCE_PROFILE_RECOVERY_REQUIRED")

                    context.finalize(accept)
                if not self.cleanup(item):
                    raise InstanceReadError("资料恢复等待通知收尾", "PLUGIN_INSTANCE_PROFILE_RECOVERY_REQUIRED")
                return True

            item.decision_key = key
            item.manager = manager
            receipt = manager.submit("plugin_instance_profile_recovery", work)
            item.receipt = {"operationId": receipt["operationId"], "status": "pending"}
            return item.receipt

    def close(self) -> bool:
        """
        未收尾时保留真实资源，不在关闭路径初始化目录或放弃备份。

        :return: 当前已经登记的全部资源均可关闭
        """
        with self._lock:
            if self._items or self._invalid:
                return False
            for directory in (self._records, self._data):
                if directory is not None:
                    directory.close()
            return True
