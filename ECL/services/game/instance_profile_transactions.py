# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：固定共享实例资料的原生目录对象并协调原子替换提交。
#
# 公开接口：
#   - class InstanceProfileTarget — 宿主确认的根、实例及物理授权身份。
#   - class NativeProfileTarget — 相对固定目录打开资料，持有明确关闭归属。
#   - class InstanceProfileTransactions — 拥有实际资料事务、版本核对与原子替换。
# ============================================================

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from ECL.services.operations import OperationCommitRejectedError, OperationContext
from ECL.utils.file_handles import FileBoundaryError, FileDirectory, FileHandle
from ECL.utils.instance_reading import DirectoryIdentity, InstanceReadBoundary, InstanceReadError

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


@dataclass(slots=True)
class _ProfileTransaction:
    """
    持有真实目录、文件及通知收尾，不复制应用执行器的任务状态。
    """

    target: InstanceProfileTarget
    installation_id: str
    reference: str = field(default_factory=lambda: uuid4().hex)
    native: NativeProfileTarget | None = None
    original: FileHandle | None = None
    stream: FileHandle | None = None
    owner: Literal["queued", "worker", "cleanup"] = "queued"
    has_published: bool = False
    is_settled: bool = False
    needs_notification: bool = False
    has_notification_error: bool = False
    temporary_name: str = field(default_factory=lambda: ".profile-new-" + uuid4().hex)


class InstanceProfileTransactions:
    """
    用资料存储的同一锁拥有共享文件事务，任务执行状态只由OperationManager维护。
    """

    def __init__(self, data_path: Path, lock: RLock, on_changed: Callable[[Path], None] | None = None) -> None:
        """
        绑定唯一资料存储，构造不打开目录。

        :param data_path: 宿主数据根，不能来自插件请求
        :param lock: InstanceProfileStore拥有的资料锁
        :param on_changed: 实际收尾后的缓存与通知入口，失败只重试通知
        """
        self._data_path = data_path.absolute()
        self._lock = lock
        self._on_changed = on_changed
        self._items: dict[str, _ProfileTransaction] = {}
        self._data: FileDirectory | None = None

    def reserve(self, target: InstanceProfileTarget, installation_id: str) -> _ProfileTransaction:
        """
        在任务入队前占用同一资料，注册失败由调用方释放，不修改游戏目录。

        :param target: 宿主已解析的实际资源
        :param installation_id: 由宿主绑定的安装身份
        :return: 任务必须持有并最终收尾的资源
        :raises InstanceReadError: 同目标占用或容量耗尽
        """
        with self._lock:
            if any(
                item.target.root_path == target.root_path
                and item.target.version_id.casefold() == target.version_id.casefold()
                for item in self._items.values()
            ):
                raise InstanceReadError("实例资料正在处理", "PLUGIN_INSTANCE_PROFILE_BUSY")
            if len(self._items) >= 32:
                raise InstanceReadError("实例资料任务资源已满", "PLUGIN_RATE_LIMITED")
            item = _ProfileTransaction(target, installation_id)
            self._items[item.reference] = item
            return item

    def has_active(
        self, root_path: Path | None = None, version_id: str | None = None, installation_id: str | None = None
    ) -> bool:
        """
        查询是否存在占用同一资料的在途事务。

        :param root_path: 可选的具体根范围
        :param version_id: 可选的实例名称
        :param installation_id: 生命周期使用的原安装范围
        :return: 该范围内是否仍有未收尾的事务
        """
        with self._lock:
            return any(
                (root_path is None or item.target.root_path == root_path)
                and (version_id is None or item.target.version_id.casefold() == version_id.casefold())
                and (installation_id is None or item.installation_id == installation_id)
                for item in self._items.values()
            )

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
                stream.write(0, content)
                stream.flush()
                context.check_cancelled()
                admit()
                if monotonic() >= deadline:
                    raise InstanceReadError("实例资料任务已超时", "PLUGIN_DEADLINE_EXCEEDED")
                digest = hashlib.sha256(content).hexdigest()
                context.finalize(lambda: self._commit(item, digest))
                try:
                    published = read_profile_snapshot(
                        InstanceReadBoundary(item.target.root_path, deadline), item.target.version_id
                    )
                    if published.sha256 == digest:
                        return published
                except InstanceReadError:
                    pass  # 发布已完成，结果无法确认只隐藏视图，不能重写或回滚业务。
                return None
            finally:
                item.owner = "cleanup"

    def _commit(self, item: _ProfileTransaction, digest: str) -> None:
        """
        按原子替换发布实例资料，覆盖原文件且不保留备份。

        替换是最后一步：要么旧资料完整保留，要么新资料完整可见。替换成功后旧内容
        不可找回，因此不再登记恢复，也不阻塞后续提交。
        """
        assert item.native is not None and item.stream is not None
        try:
            # 版本已核对：释放原对象独占句柄，替换才能进行。
            if item.original is not None:
                item.original.close()
                item.original = None
            item.stream.replace("instance.json")
            item.has_published = True
            item.needs_notification = True
            item.stream.close()
        except Exception as exc:
            if not item.has_published and self._target_holds(item):
                item.has_published = True
                item.needs_notification = True
            if not item.has_published and not item.stream.is_published:
                has_cleaned = self._cleanup_locked(item)
                if has_cleaned and not item.has_published:
                    raise OperationCommitRejectedError(
                        "实例资料未发布，原资料已保留", "PLUGIN_INSTANCE_PROFILE_WRITE_FAILED"
                    ) from exc
            raise

    @staticmethod
    def _target_holds(item: _ProfileTransaction) -> bool:
        """
        按实际目录项判断本次新对象是否已经占用资料名称。

        替换成功但确认丢失时不能让上层误报「未生效」。
        """
        if item.stream is None or item.native is None or item.native.profile_directory is None:
            return False
        try:
            current = item.native.profile_directory.entry_identity("instance.json", monotonic() + 1)
        except (FileBoundaryError, InstanceReadError):
            return False
        return current is not None and current == item.stream.identity

    def _cleanup_locked(self, item: _ProfileTransaction) -> bool:
        """
        持有资料锁时只处理实际对象，缓存通知由锁外的cleanup单独重试。
        """
        if item.is_settled:
            return True
        try:
            if item.stream is not None and not item.stream.is_closed:
                # has_published 是本次提交按实际目录项得出的结论，优先于句柄自身的标记
                if item.has_published or item.stream.is_published:
                    item.needs_notification = True
                    item.stream.close()
                else:
                    item.stream.discard()
            if item.original is not None and not item.original.is_closed:
                item.original.close()
            if item.native is not None and not item.native.close():
                return False
            item.is_settled = True
            return True
        except (FileBoundaryError, InstanceReadError, ValueError):
            return False

    def cleanup(self, item: _ProfileTransaction) -> bool:
        """
        原对象、暂存与目录句柄都结束后归还占用，通知失败仅重试通知。

        :param item: 原任务持有的同一资源
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
            return True

    def close(self) -> bool:
        """
        未收尾时保留真实资源，不在关闭路径初始化目录。

        :return: 当前已经登记的全部资源均可关闭
        """
        with self._lock:
            if self._items:
                return False
            if self._data is not None:
                self._data.close()
            return True
