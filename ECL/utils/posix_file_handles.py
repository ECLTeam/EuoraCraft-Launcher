# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：以 POSIX 目录描述符固定本地对象，提供有界文件事务及显式清理。
#
# 公开接口：
#   - FileDirectory / FileHandle — 目录相对文件操作与资源生命周期。
# ============================================================

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from ECL.utils.file_models import FileBoundaryError, FileIdentity, FileInformation
from ECL.utils.posix_file_native import PosixFiles, file_errors

if TYPE_CHECKING:
    from ECL.utils.posix_file_transactions import FileDeletion


class FileDirectory:
    """
    保留祖先描述符及名称绑定，路径被替换后拒绝对旧位置继续提交。
    """

    def __init__(
        self,
        native: PosixFiles,
        descriptor: int,
        *,
        anchors: tuple[tuple[int, str, int], ...] = (),
        ancestors: tuple[int, ...] = (),
        parent: FileDirectory | None = None,
    ) -> None:
        self._native = native
        self._handle = descriptor
        self._anchors = anchors
        self._ancestors = ancestors
        self._parent = parent
        self._lock = RLock()
        self._files = 0
        self._children = 0
        self._deletions: list[FileDeletion] = []
        self.identity: FileIdentity = native.information(descriptor).identity
        self.information()

    @classmethod
    def open(cls, directory_path: Path) -> FileDirectory:
        """
        逐组件打开受信绝对目录，拒绝链接并保留祖先绑定。

        :param directory_path: 宿主指定的现有本地目录
        :return: 调用方必须显式关闭的目录
        :raises FileBoundaryError: 平台、路径、文件系统或待恢复记录不满足边界
        """
        native = PosixFiles()
        if not directory_path.is_absolute() or any(part in {".", ".."} for part in directory_path.parts):
            raise FileBoundaryError("受控文件需要绝对本地目录", "PLUGIN_FILE_INVALID")
        descriptors: list[int] = []
        anchors: list[tuple[int, str, int]] = []
        try:
            with file_errors():
                descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                descriptors.append(descriptor)
                for part in directory_path.parts[1:]:
                    cls._component(part)
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
                    )
                    descriptors.append(child)
                    anchors.append((descriptor, part, child))
                    descriptor = child
                native.check_local(descriptor)
                result = cls(native, descriptor, anchors=tuple(anchors), ancestors=tuple(descriptors[:-1]))
                result._recover()
                return result
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @staticmethod
    def _component(name: str) -> None:
        if not name or name in {".", ".."} or any(character in name for character in "/\\:\0"):
            raise FileBoundaryError("文件名称必须是单个安全组件", "PLUGIN_FILE_INVALID")

    def information(self) -> FileInformation:
        """
        核对目录对象及全部仍持有的祖先名称绑定。

        :return: 当前目录的稳定物理信息
        :raises FileBoundaryError: 目录已关闭、被替换或跨越原对象
        """
        with file_errors():
            if self._handle < 0:
                raise FileBoundaryError("文件目录已关闭", "PLUGIN_FILE_INVALID")
            for parent, name, child in self._anchors:
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                held = os.fstat(child)
                if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (held.st_dev, held.st_ino):
                    raise FileBoundaryError("文件目录绑定已经变化", "PLUGIN_FILE_CHANGED")
            result = self._native.information(self._handle)
            if not result.attributes & 0x10 or result.identity != self.identity:
                raise FileBoundaryError("文件目录身份已经变化", "PLUGIN_FILE_CHANGED")
            return result

    def stat_information(self) -> os.stat_result:
        """
        从同一已核对描述符读取 Python 元数据。

        :return: 当前目录对象的 fstat 结果
        """
        self.information()
        with file_errors():
            return os.fstat(self._handle)

    def child(self, name: str, *, create: bool = False) -> FileDirectory:
        """
        相对原目录打开或独占创建子目录，不跟随链接或跨设备。

        :param name: 单个目录组件
        :param create: 是否要求创建新目录，存在时拒绝
        :return: 调用方负责关闭的子目录，父目录必须保持打开
        """
        self._component(name)
        with self._lock, file_errors():
            self.information()
            if create:
                os.mkdir(name, 0o700, dir_fd=self._handle)
                os.fsync(self._handle)
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._handle)
            try:
                if os.fstat(descriptor).st_dev != self.identity.volume:
                    raise FileBoundaryError("子目录不属于原文件设备", "PLUGIN_FILE_INVALID")
                result = FileDirectory(
                    self._native, descriptor, anchors=(*self._anchors, (self._handle, name, descriptor)), parent=self
                )
                result._recover()
                self._children += 1
                return result
            except BaseException:
                os.close(descriptor)
                raise

    def _recover(self) -> None:
        """
        仅处理自身格式的中断记录；未知或冲突对象保留并拒绝继续。
        """
        from ECL.utils.posix_file_transactions import FileDeletion

        for name in self.names(monotonic() + 5):
            if FileDeletion.is_record_name(name):
                FileDeletion.recover(self, name)

    def file(
        self, name: str, *, create: bool = False, on_create: Callable[[FileHandle], None] | None = None
    ) -> FileHandle:
        """
        打开单链接普通文件；新建先交接所有者，再执行可能失败的校验。

        :param name: 单个受控名称
        :param create: 是否独占创建可写暂存
        :param on_create: 创建副作用产生后的唯一资源所有者
        :return: 持有独占合作锁的文件对象
        """
        self._component(name)
        with self._lock, file_errors():
            self.information()
            flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            flags |= (os.O_RDWR | os.O_CREAT | os.O_EXCL) if create else os.O_RDONLY
            descriptor = os.open(name, flags, 0o600, dir_fd=self._handle)
            stream = FileHandle(self, descriptor, name)
            try:
                if create and on_create is not None:
                    on_create(stream)
                import fcntl

                fcntl.flock(descriptor, (fcntl.LOCK_EX if create else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                stream._identity = stream.information().identity
                return stream
            except BaseException:
                if not create:
                    stream.close()
                elif on_create is None:
                    stream.discard()
                raise

    def delete_target(self, name: str, own: Callable[[FileHandle], None]) -> FileHandle:
        """
        交接原目标后验证普通单链接对象，失败仍由原所有者收尾。

        :param name: 目标组件
        :param own: 原对象资源所有者
        :return: 持有原始身份及合作锁的目标
        """
        self._component(name)
        with self._lock, file_errors():
            self.information()
            descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._handle)
            stream = FileHandle(self, descriptor, name)
            own(stream)
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream._identity = stream.information().identity
            return stream

    def names(self, deadline: float, limit: int = 1024) -> tuple[str, ...]:
        """
        从目录描述符有界枚举，不重新解析原路径。

        :param deadline: 整次枚举的单调截止
        :param limit: 总项数上限，包含内部暂存及记录
        :return: 当前直属名称
        """
        with self._lock, file_errors():
            self.information()
            names = []
            with os.scandir(self._handle) as entries:
                for entry in entries:
                    if monotonic() >= deadline:
                        raise FileBoundaryError("目录枚举超时", "PLUGIN_DEADLINE_EXCEEDED")
                    names.append(entry.name)
                    if len(names) > limit:
                        raise FileBoundaryError("目录项数量超限", "PLUGIN_STORAGE_QUOTA_EXCEEDED")
            return tuple(names)

    def entry_identity(self, name: str, deadline: float) -> FileIdentity | None:
        """
        查询名称本身的当前身份，调用方不得据此补删同名其他对象。

        :param name: 单个名称
        :param deadline: 查询截止
        :return: 目录项身份；确实不存在时为 None
        """
        self._component(name)
        self.information()
        if monotonic() >= deadline:
            raise FileBoundaryError("文件查询超时", "PLUGIN_DEADLINE_EXCEEDED")
        try:
            return self._native.entry(self._handle, name).identity
        except FileBoundaryError as error:
            if error.error_code == "PLUGIN_RESOURCE_NOT_FOUND":
                return None
            raise

    def save_record(self, name: str, content: bytes) -> None:
        """
        原子替换宿主元数据，不能用于绕过插件目标的版本检查。

        :param name: 受信记录名称
        :param content: 已校验的有限记录字节
        """
        stream = self.file(f".record-{uuid4().hex}", create=True)
        try:
            stream.write(0, content)
            stream.flush()
            stream.replace(name)
        finally:
            if stream.is_published:
                stream.close()
            else:
                stream.discard()

    def close(self) -> None:
        """
        全部所属文件和子目录结束后释放描述符；未知清理状态保留责任。
        """
        with self._lock, file_errors():
            if self._handle < 0:
                return
            if self._files or self._children:
                raise FileBoundaryError("目录仍有活动文件或子目录", "PLUGIN_FILE_BUSY")
            for deletion in tuple(self._deletions):
                deletion.finish()
            os.close(self._handle)
            self._handle = -1
            for descriptor in reversed(self._ancestors):
                os.close(descriptor)
            self._ancestors = ()
            if self._parent is not None:
                with self._parent._lock:
                    self._parent._children -= 1
                self._parent = None


class FileHandle:
    """
    持有原对象描述符及唯一事务，发布后禁止按暂存删除。
    """

    def __init__(self, directory: FileDirectory, descriptor: int, name: str) -> None:
        self.directory: FileDirectory = directory
        self._native = directory._native
        self._handle = descriptor
        self._name = name
        self._identity: FileIdentity | None = None
        self.is_published: bool = False
        self._delete_pending = False
        self._deletion: FileDeletion | None = None
        self._has_unlinked = False
        directory._files += 1

    def object_identity(self) -> FileIdentity:
        """
        查询已接管描述符的原始身份，供初始校验失败后的清理使用。

        :return: 对象身份，不表示它已经通过业务文件验证
        """
        return self._native.information(self._handle).identity

    @property
    def identity(self) -> FileIdentity:
        """
        返回初次通过验证的稳定物理身份。

        :return: 原对象身份
        """
        if self._identity is None:
            raise FileBoundaryError("文件身份尚未确认", "PLUGIN_FILE_INVALID")
        return self._identity

    def information(self) -> FileInformation:
        """
        拒绝非普通、硬链接、跨设备及已解除链接的对象。

        :return: 原文件当前元数据
        """
        self.directory.information()
        result = self._native.information(self._handle)
        if (
            result.attributes & (0x400 | 0x10)
            or result.links != 1
            or result.identity.volume != self.directory.identity.volume
        ):
            raise FileBoundaryError("文件不是同设备单链接普通对象", "PLUGIN_FILE_INVALID")
        if self._identity is not None and result.identity != self._identity:
            raise FileBoundaryError("原文件身份已经变化", "PLUGIN_FILE_CHANGED")
        return result

    def stat_information(self) -> os.stat_result:
        """
        从同一已验证描述符读取 Python 文件信息。

        :return: 原文件的 fstat 结果
        """
        self.information()
        with file_errors():
            return os.fstat(self._handle)

    def read(self, offset: int, count: int) -> bytes:
        """
        对原对象执行有界偏移读取，不修改共享游标。

        :param offset: 非负字节偏移
        :param count: 本次最多读取的字节数
        :return: 实际读取内容
        """
        self.information()
        if offset < 0 or not 0 <= count <= 8 * 1024 * 1024:
            raise FileBoundaryError("文件读取范围无效", "PLUGIN_FILE_INVALID")
        with file_errors():
            return os.pread(self._handle, count, offset)

    def digest(self, deadline: float, max_bytes: int = 8388608) -> str:
        """
        在同一描述符上有界计算全部字节摘要。

        :param deadline: 单调截止
        :param max_bytes: 文件最大长度
        :return: 完整 SHA-256
        """
        size = self.information().size
        if size > max_bytes:
            raise FileBoundaryError("文件大小超限", "PLUGIN_WRITE_SIZE_EXCEEDED")
        digest, offset = hashlib.sha256(), 0
        while offset < size:
            if monotonic() >= deadline:
                raise FileBoundaryError("文件查询超时", "PLUGIN_DEADLINE_EXCEEDED")
            block = self.read(offset, min(32768, size - offset))
            if not block:
                raise FileBoundaryError("文件内容已经变化", "PLUGIN_FILE_CHANGED")
            digest.update(block)
            offset += len(block)
        return digest.hexdigest()

    def write(self, offset: int, content: bytes) -> None:
        """
        仅向未发布暂存写入有限内容，部分写入明确失败。

        :param offset: 已核对偏移
        :param content: 已校验的有限字节
        """
        self.information()
        if self.is_published or offset < 0 or len(content) > 8 * 1024 * 1024:
            raise FileBoundaryError("文件写入状态无效", "PLUGIN_FILE_INVALID")
        with file_errors():
            if os.pwrite(self._handle, content, offset) != len(content):
                raise FileBoundaryError("文件写入未完成", "PLUGIN_STORAGE_FAILED")

    def flush(self) -> None:
        """
        发布前刷新原暂存对象，失败时不继续发布。
        """
        self.information()
        with file_errors():
            os.fsync(self._handle)

    def _rename(self, name: str, *, replace: bool) -> None:
        """
        复核原绑定并原子发布；副作用后保留发布状态，禁止误删正式对象。
        """
        directory = self.directory
        directory._component(name)
        with directory._lock:
            self.information()
            if directory.entry_identity(self._name, monotonic() + 1) != self.identity:
                raise FileBoundaryError("文件名称绑定已经变化", "PLUGIN_FILE_CHANGED")
            if replace:
                try:
                    target = self._native.entry(directory._handle, name)
                    if target.attributes & 1:
                        raise FileBoundaryError("目标文件为只读，不能替换", "PLUGIN_FILE_INVALID")
                except FileBoundaryError as error:
                    if error.error_code != "PLUGIN_RESOURCE_NOT_FOUND":
                        raise
            self._native.rename(directory._handle, self._name, directory._handle, name, replace=replace)
            self._name = name
            self.is_published = True
            with file_errors():
                os.fsync(directory._handle)
            if directory.entry_identity(name, monotonic() + 1) != self.identity:
                raise FileBoundaryError("发布目标身份无法确认", "PLUGIN_FILE_CHANGED")

    def publish(self, name: str) -> None:
        """
        原子发布新文件，目标存在时绝不覆盖。

        :param name: 正式单个名称
        """
        if self.is_published:
            raise FileBoundaryError("文件已经发布", "PLUGIN_FILE_INVALID")
        self._rename(name, replace=False)

    def replace(self, name: str) -> None:
        """
        在调用方已完成版本检查后原子替换目标，不保留备份。

        :param name: 正式单个名称
        """
        if self.is_published:
            raise FileBoundaryError("文件已经发布", "PLUGIN_FILE_INVALID")
        self._rename(name, replace=True)

    def move(self, name: str) -> None:
        """
        不覆盖移动已验证对象，移动后禁止作为暂存清理。

        :param name: 所有者批准的目标名称
        """
        if self.is_closed or self.deletion_pending():
            raise FileBoundaryError("文件移动状态无效", "PLUGIN_FILE_INVALID")
        self._rename(name, replace=False)

    def mark_deletion(self, pending: bool) -> None:
        """
        改变本对象删除请求；只有关闭阶段才执行实际移除。

        :param pending: 请求删除或撤回；撤回失败保留中断记录
        """
        if self.is_closed or self._has_unlinked:
            raise FileBoundaryError("原对象不能撤回删除", "PLUGIN_FILE_DELETE_PENDING")
        if pending and self._native.information(self._handle).attributes & 1:
            raise FileBoundaryError("原文件为只读，不能删除", "PLUGIN_FILE_INVALID")
        if not pending and self._deletion is not None:
            self._deletion.rollback()
            self._deletion = None
        self._delete_pending = pending

    def deletion_pending(self) -> bool:
        """
        返回原对象持有的删除请求，不通过同名路径猜测状态。

        :return: 关闭时是否需要实际移除
        """
        return self._delete_pending

    def discard(self) -> None:
        """
        只清理本对象拥有的未发布暂存，不能删除已发布文件。
        """
        if self.is_published:
            raise FileBoundaryError("不能删除已发布文件", "PLUGIN_FILE_INVALID")
        self.mark_deletion(True)
        self.close()

    @property
    def has_pending_cleanup(self) -> bool:
        """
        查询原对象或事务元数据是否仍有本所有者必须完成的清理。

        :return: 仍需重试清理时为 True，不把描述符关闭等同事务资源释放
        """
        return not self.is_closed or self._deletion is not None

    @property
    def is_closed(self) -> bool:
        """
        返回原对象描述符的关闭状态，事务记录可能仍需要清理。

        :return: 描述符是否已关闭
        """
        return self._handle < 0

    def close(self) -> None:
        """
        删除请求先隔离核对原对象再移除；清理失败仍保留记录供重试。
        """
        from ECL.utils.posix_file_transactions import FileDeletion

        with self.directory._lock, file_errors():
            if not self.is_closed:
                if self._delete_pending:
                    if self._deletion is None:
                        self._deletion = FileDeletion.start(self.directory, self._name, self.object_identity())
                    try:
                        self._deletion.remove()
                    finally:
                        if self._deletion.has_unlinked:
                            self._has_unlinked = True
                            self._close_descriptor()
                else:
                    self._close_descriptor()
            if self._deletion is not None:
                self._deletion.finish()
                self._deletion = None

    def _close_descriptor(self) -> None:
        """
        关闭原描述符后立即放弃编号，失败也不能误关可能已被复用的其他对象。
        """
        descriptor, self._handle = self._handle, -1
        self.directory._files -= 1
        os.close(descriptor)
