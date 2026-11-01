# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：持久记录 POSIX 删除隔离与不覆盖恢复，未知对象始终保留。
#
# 公开接口：
#   - FileDeletion — 原对象删除与中断恢复事务，由文件或目录对象持有。
# ============================================================

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ECL.utils.file_models import FileBoundaryError, FileIdentity
from ECL.utils.posix_file_native import file_errors

if TYPE_CHECKING:
    from ECL.utils.posix_file_handles import FileDirectory


class _DeletionRecord(BaseModel):
    """
    只持久化名称和物理身份，不持久化授权或恢复旧业务任务。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    directory: FileIdentity
    source_name: str
    original: FileIdentity
    quarantine: FileIdentity | None

    @staticmethod
    def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """
        拒绝重复键，记录不能依赖 JSON 解析器覆盖字段来确定恢复对象。

        :param pairs: 解码器保留的原始字段顺序
        :return: 不含重复键的字段集合，随后转换为严格模型
        """
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("删除记录包含重复字段")
            result[key] = value
        return result


class FileDeletion:
    """
    记录位于父目录，隔离目录移除后才移除记录，避免丢失恢复依据。
    """

    prefix: ClassVar[str] = ".ecl-delete-"

    def __init__(self, directory: FileDirectory, name: str, identity: FileIdentity, identifier: str) -> None:
        self.directory = directory
        self._name = name
        self._identity = identity
        self._record_name = self.prefix + identifier + ".json"
        self._folder_name = self.prefix + identifier + ".d"
        self._stage_name = self.prefix + identifier + ".pending"
        self._record_fd = -1
        self._folder_fd = -1
        self._stage_fd = -1
        self._record_identity: FileIdentity | None = None
        self._folder_identity: FileIdentity | None = None
        self._stage_identity: FileIdentity | None = None
        self._observed_identity: FileIdentity | None = None
        self._is_moved = False
        self._record_removed = False
        self._folder_removed = False
        self._stage_removed = False
        self.has_unlinked: bool = False
        directory._deletions.append(self)

    @classmethod
    def is_record_name(cls, name: str) -> bool:
        """
        只识别本实现的完整记录命名，不接管宽泛前缀或任意用户文件。

        :param name: 当前目录项名称
        :return: 是否符合严格随机标识和记录后缀
        """
        value = name.removeprefix(cls.prefix).removesuffix(".json")
        return name == cls.prefix + value + ".json" and len(value) == 32 and all(c in "0123456789abcdef" for c in value)

    @classmethod
    def is_internal_name(cls, name: str) -> bool:
        """
        识别事务自身保留名称，不允许用它们充当待删除业务目标。

        :param name: 单个目录项
        :return: 是否位于事务命名空间
        """
        return name.startswith(cls.prefix)

    @classmethod
    def start(cls, directory: FileDirectory, name: str, identity: FileIdentity) -> FileDeletion:
        """
        先交接事务归属再产生磁盘副作用，后续失败由同一所有者收尾。

        :param directory: 原目录对象
        :param name: 原对象的单个名称
        :param identity: 已持有描述符确认的原身份
        :return: 尚未产生文件副作用的事务
        """
        directory._component(name)
        if cls.is_internal_name(name):
            raise FileBoundaryError("事务元数据不是业务删除目标", "PLUGIN_FILE_INVALID")
        return cls(directory, name, identity, uuid4().hex)

    def _check(self) -> None:
        """
        每次命名空间副作用前核对父目录及事务自己的已创建对象。
        """
        directory = self.directory
        directory.information()
        if (
            self._record_fd >= 0
            and not self._record_removed
            and directory._native.entry(directory._handle, self._record_name).identity != self._record_identity
        ):
            raise FileBoundaryError("删除记录绑定已经变化", "PLUGIN_FILE_CHANGED")
        if (
            self._folder_fd >= 0
            and not self._folder_removed
            and directory._native.entry(directory._handle, self._folder_name).identity != self._folder_identity
        ):
            raise FileBoundaryError("隔离目录绑定已经变化", "PLUGIN_FILE_CHANGED")

    def _record(self, quarantine: FileIdentity | None) -> bytes:
        return (
            _DeletionRecord(
                directory=self.directory.identity,
                source_name=self._name,
                original=self._identity,
                quarantine=quarantine,
            )
            .model_dump_json()
            .encode()
        )

    @staticmethod
    def _lock(descriptor: int) -> None:
        """
        活动事务保持独占锁，其他来源不能将正在进行的记录当作中断恢复。
        """
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _write(descriptor: int, content: bytes) -> None:
        if os.pwrite(descriptor, content, 0) != len(content):
            raise FileBoundaryError("删除记录写入未完成", "PLUGIN_STORAGE_FAILED")
        os.fsync(descriptor)

    def _prepare(self) -> None:
        """
        写前记录先于任何原文件移动；发布新记录前先锁住新描述符。
        """
        directory = self.directory
        self._check()
        self._record_fd = os.open(
            self._record_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory._handle,
        )
        self._record_identity = directory._native.information(self._record_fd).identity
        self._lock(self._record_fd)
        self._write(self._record_fd, self._record(None))
        os.fsync(directory._handle)
        os.mkdir(self._folder_name, 0o700, dir_fd=directory._handle)
        self._folder_fd = os.open(
            self._folder_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory._handle
        )
        self._folder_identity = directory._native.information(self._folder_fd).identity
        self._stage_fd = os.open(
            self._stage_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory._handle,
        )
        self._stage_identity = directory._native.information(self._stage_fd).identity
        self._lock(self._stage_fd)
        self._write(self._stage_fd, self._record(self._folder_identity))
        self._check()
        directory._native.rename(
            directory._handle, self._stage_name, directory._handle, self._record_name, replace=True
        )
        self._stage_removed = True
        old = self._record_fd
        self._record_fd, self._stage_fd = self._stage_fd, -1
        self._record_identity = self._stage_identity
        os.close(old)
        os.fsync(directory._handle)

    def _object_identity(self) -> FileIdentity | None:
        if self._folder_fd < 0 or self._folder_removed:
            return None
        try:
            return self.directory._native.entry(self._folder_fd, "object").identity
        except FileBoundaryError as error:
            if error.error_code == "PLUGIN_RESOURCE_NOT_FOUND":
                return None
            raise

    def remove(self) -> None:
        """
        隔离后再次验证原对象身份，只有相符对象才会被实际移除。
        """
        directory = self.directory
        with file_errors():
            if self._record_fd < 0:
                self._prepare()
            self._check()
            if not self._is_moved:
                if directory._native.entry(directory._handle, self._name).identity != self._identity:
                    raise FileBoundaryError("删除目标绑定已变化", "PLUGIN_FILE_CHANGED")
                directory._native.rename(directory._handle, self._name, self._folder_fd, "object", replace=False)
                self._is_moved = True
                self._observed_identity = self._object_identity()
                os.fsync(self._folder_fd)
                os.fsync(directory._handle)
            if self._object_identity() != self._identity:
                raise FileBoundaryError("隔离对象不是原删除目标", "PLUGIN_FILE_CHANGED")
            os.unlink("object", dir_fd=self._folder_fd)
            self.has_unlinked = True
            os.fsync(self._folder_fd)

    def rollback(self) -> None:
        """
        不覆盖恢复尚存对象；实际移除后不能伪造回滚成功。
        """
        with file_errors():
            if self.has_unlinked:
                raise FileBoundaryError("原文件已经移除，等待实际清理", "PLUGIN_FILE_DELETE_PENDING")
            self._check()
            current = self._object_identity()
            if current is not None:
                expected = self._observed_identity or self._identity
                if current != expected:
                    raise FileBoundaryError("隔离对象身份不明确，保留待恢复", "PLUGIN_FILE_CHANGED")
                self.directory._native.rename(
                    self._folder_fd, "object", self.directory._handle, self._name, replace=False
                )
                self._is_moved = False
                os.fsync(self._folder_fd)
                os.fsync(self.directory._handle)
            self.finish()

    def _remove_stage(self) -> None:
        if self._stage_fd >= 0:
            if not self._stage_removed:
                if (
                    self.directory._native.entry(self.directory._handle, self._stage_name).identity
                    != self._stage_identity
                ):
                    raise FileBoundaryError("暂存记录身份不明确", "PLUGIN_FILE_CHANGED")
                os.unlink(self._stage_name, dir_fd=self.directory._handle)
                self._stage_removed = True
            os.close(self._stage_fd)
            self._stage_fd = -1

    def finish(self) -> None:
        """
        仅移除自身元数据和已确认空目录，保留任何未知对象或失败责任。
        """
        directory = self.directory
        with file_errors():
            self._check()
            if self._folder_fd >= 0:
                if not self._folder_removed:
                    with os.scandir(self._folder_fd) as entries:
                        if next(entries, None) is not None:
                            raise FileBoundaryError("隔离目录仍有对象，不能清理", "PLUGIN_FILE_BUSY")
                    os.fsync(self._folder_fd)
                    os.rmdir(self._folder_name, dir_fd=directory._handle)
                    self._folder_removed = True
                    os.fsync(directory._handle)
                os.close(self._folder_fd)
                self._folder_fd = -1
            self._remove_stage()
            if self._record_fd >= 0:
                if not self._record_removed:
                    os.unlink(self._record_name, dir_fd=directory._handle)
                    self._record_removed = True
                    os.fsync(directory._handle)
                os.close(self._record_fd)
                self._record_fd = -1
            directory._deletions.remove(self)

    def _load(self) -> None:
        """
        锁住严格记录并核对目录及隔离对象，不依据名称猜测可接管数据。
        """
        directory = self.directory
        self._record_fd = os.open(
            self._record_name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory._handle
        )
        self._lock(self._record_fd)
        info = os.fstat(self._record_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 8192
        ):
            raise FileBoundaryError("删除记录不满足私有边界", "PLUGIN_FILE_INVALID")
        self._record_identity = directory._native.information(self._record_fd).identity
        try:
            record = _DeletionRecord.model_validate(
                json.loads(os.pread(self._record_fd, 8193, 0), object_pairs_hook=_DeletionRecord.unique_fields)
            )
        except (ValueError, RecursionError):
            raise FileBoundaryError("删除记录损坏，保留待恢复", "PLUGIN_STORAGE_FAILED") from None
        if record.directory != directory.identity:
            raise FileBoundaryError("删除记录不属于当前物理目录", "PLUGIN_FILE_CHANGED")
        directory._component(record.source_name)
        if self.is_internal_name(record.source_name):
            raise FileBoundaryError("删除记录目标无效", "PLUGIN_FILE_INVALID")
        self._name, self._identity, self._folder_identity = record.source_name, record.original, record.quarantine
        self._load_folder()
        self._check()

    def _load_folder(self) -> None:
        """
        缺失隔离目录不等同丢失业务文件；未知同名目录不能被删除。
        """
        directory = self.directory
        try:
            entry = directory._native.entry(directory._handle, self._folder_name)
        except FileBoundaryError as error:
            if error.error_code == "PLUGIN_RESOURCE_NOT_FOUND":
                self._folder_removed = True
                return
            raise
        if entry.identity != self._folder_identity or entry.attributes != 0x10:
            raise FileBoundaryError("隔离目录身份不明确，保留待恢复", "PLUGIN_FILE_CHANGED")
        self._folder_fd = os.open(
            self._folder_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory._handle
        )
        held = os.fstat(self._folder_fd)
        if held.st_uid != os.geteuid() or held.st_mode & 0o077:
            raise FileBoundaryError("隔离目录不满足私有边界", "PLUGIN_FILE_INVALID")
        if directory._native.information(self._folder_fd).identity != self._folder_identity:
            raise FileBoundaryError("隔离目录打开期间被替换", "PLUGIN_FILE_CHANGED")

    @classmethod
    def recover(cls, directory: FileDirectory, name: str) -> None:
        """
        恢复一次中断的尚存对象；冲突时保留记录和全部数据，不恢复业务任务。

        :param directory: 当前明确访问的受控目录
        :param name: 已符合本实现格式的记录名称
        """
        identifier = name[len(cls.prefix) : -5]
        transaction = cls(directory, "", directory.identity, identifier)
        try:
            with file_errors():
                transaction._load()
                transaction.rollback()
        finally:
            # 失败的恢复不能持有已拒绝目录的隐式资源，也不能清理未知磁盘对象。
            for attribute in ("_record_fd", "_folder_fd", "_stage_fd"):
                descriptor = getattr(transaction, attribute)
                if descriptor >= 0:
                    os.close(descriptor)
                    setattr(transaction, attribute, -1)
            if transaction in directory._deletions:
                directory._deletions.remove(transaction)
