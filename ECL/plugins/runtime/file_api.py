# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：将当前 Worker 的私有文件请求接到句柄存储和应用任务服务，不开放任意路径。
#
# 公开接口：
#   - class RuntimeFiles — 校验私有根引用、有限上传、发布及运行结束清理。
# ============================================================

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from threading import RLock
from time import monotonic, sleep
from uuid import uuid4

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.runtime.file_handles import FileBoundaryError, FileDirectory, FileHandle, FileIdentity, FileInformation
from ECL.plugins.runtime.file_storage import FileDeletion, PluginFileStorage, StagedFile
from ECL.plugins.runtime.models import (
    FileDeleteRequest,
    FileListRequest,
    FileReadRequest,
    FileRef,
    FileStatRequest,
    FileWriteBeginRequest,
    FileWriteChunkRequest,
    FileWriteCommitRequest,
    FileWriteRef,
    ResourceListRequest,
)
from ECL.plugins.runtime.operation_adapters import RuntimeOperations
from ECL.plugins.runtime.resources import ResourcePages
from ECL.services.operations import OperationCommitRejectedError, OperationContext


@dataclass(frozen=True, slots=True)
class _PublishedFile:
    target: FileRef
    root_identity: FileIdentity
    information: FileInformation
    digest: str


class RuntimeFiles:
    """
    一个运行持有一组代次引用；上传字节由共享存储负责，执行状态由任务服务负责。
    """

    def __init__(self, session: PluginGrantSession, storage: PluginFileStorage, operations: RuntimeOperations) -> None:
        """
        绑定宿主持有的来源和存储，不从请求中接受安装身份。

        :param session: 当前运行授权
        :param storage: 应用唯一文件存储
        :param operations: 当前运行的应用任务适配器
        """
        self._session = session
        self._storage = storage
        self._operations = operations
        self._installation_id = session.view().installation_id
        self._lock = RLock()
        self._closed = False
        self._epoch = -1
        self._root_ref = ""
        self._root_identity: FileIdentity | None = None
        self._pages = ResourcePages()

    def _ensure_open(self) -> None:
        """
        在内存短锁内拒绝关闭后的新动作，不等待磁盘或上传锁。
        """
        with self._lock:
            if self._closed:
                raise ProtocolError("插件文件会话已关闭", "PLUGIN_SESSION_EXPIRED")

    def _accept(self, lease: GrantLease) -> None:
        """
        将仍有效的原始权限与未关闭状态排序，实际 I/O 在接受返回后执行。
        """
        self._session.accept(lease, self._ensure_open)

    def _check(self, lease: GrantLease, deadline: float) -> None:
        """
        每个实际动作和结果交付检查原始权限与预算，不能通过重新授权延续旧上传。
        """
        self._ensure_open()
        self._session.check(lease)
        if monotonic() >= deadline:
            raise FileBoundaryError("插件文件操作超时", "PLUGIN_DEADLINE_EXCEEDED")

    def _resolve(self, target: FileRef, lease: GrantLease, identity: FileIdentity | None) -> None:
        """
        旧代次或根对象变化统一失效，不接受重新绑定旧引用。
        """
        with self._lock:
            if target.root_ref != self._root_ref or lease.grant_epoch != self._epoch:
                raise FileBoundaryError("未找到自身文件引用", "PLUGIN_RESOURCE_NOT_FOUND")
            if identity != self._root_identity:
                raise FileBoundaryError("私有根对象已变化", "PLUGIN_FILE_CHANGED")

    @staticmethod
    def _view(target: FileRef, information: FileInformation, digest: str) -> dict[str, JsonValue]:
        return {
            "file_ref": target.model_dump(mode="json"),
            "name": target.relative_path,
            "kind": "file",
            "size": information.size,
            "modified_ms": information.modified_ns // 1_000_000,
            "etag": information.etag(digest),
        }

    def _resources(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        只发现当前安装的逻辑根；首次只读发现不创建任何目录或记录。
        """
        query = ResourceListRequest.model_validate(params)
        if query.capability not in {"filesystem.read", "filesystem.write", "filesystem.delete"}:
            raise ProtocolError("此资源类别不属于私有文件", "PLUGIN_SCHEMA_INVALID")
        lease = self._session.authorize(query.capability)
        with self._storage.access(self._installation_id) as space:
            self._check(lease, deadline)
            with self._lock:
                if self._epoch != lease.grant_epoch or self._root_identity != space.identity:
                    self._root_ref = uuid4().hex
                    self._root_identity = space.identity
                    self._epoch = lease.grant_epoch
                reference = self._root_ref
            result = self._pages.page(
                query.model_dump_json(exclude={"cursor"}),
                str(lease.grant_epoch),
                query.cursor,
                query.limit,
                lambda: ({"kind": "private_directory", "root_ref": reference, "label": "plugin_data"},),
            )
            self._check(lease, deadline)
            return result

    def _read(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        持有安装 I/O 锁与稳定文件句柄计算完整版本，交付前再次复核权限和根。
        """
        query = (
            FileReadRequest.model_validate(params) if method == "files.read" else FileStatRequest.model_validate(params)
        )
        lease = self._session.authorize("filesystem.read")
        target = query.target
        with self._storage.access(self._installation_id) as space:
            self._resolve(target, lease, space.identity)
            self._check(lease, deadline)
            if not target.relative_path:
                if method == "files.read":
                    raise FileBoundaryError("只能读取普通文件", "PLUGIN_SCHEMA_INVALID")
                root_etag = hashlib.sha256(str(space.identity).encode()).hexdigest()
                return {
                    "file_ref": target.model_dump(mode="json"),
                    "name": "plugin_data",
                    "kind": "directory",
                    "size": 0,
                    "modified_ms": 0,
                    "etag": root_etag,
                }
            if space.directory is None:
                raise FileBoundaryError("插件文件不存在", "PLUGIN_RESOURCE_NOT_FOUND")
            stream = space.directory.file(target.relative_path)
            try:
                information = stream.information()
                digest = stream.digest(deadline)
                if isinstance(query, FileReadRequest):
                    etag = information.etag(digest)
                    if query.expected_etag is not None and query.expected_etag != etag:
                        raise FileBoundaryError("插件文件版本已变化", "PLUGIN_FILE_CHANGED")
                    block = stream.read(query.offset, query.count)
                    result: dict[str, JsonValue] = {
                        "data_b64": base64.b64encode(block).decode("ascii"),
                        "eof": query.offset + len(block) >= information.size,
                        "etag": etag,
                    }
                else:
                    result = self._view(target, information, digest)
                self._check(lease, deadline)
                space.directory.information()
                return result
            finally:
                stream.close()

    def _list(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        快照只保存有限名称，后续每页从实际文件重新投影，隐藏内部暂存名称。
        """
        query = FileListRequest.model_validate(params)
        if query.directory.relative_path:
            raise FileBoundaryError("只能枚举私有根", "PLUGIN_SCHEMA_INVALID")
        lease = self._session.authorize("filesystem.read")
        with self._storage.access(self._installation_id) as space:
            self._resolve(query.directory, lease, space.identity)
            self._check(lease, deadline)

            def load() -> tuple[dict[str, JsonValue], ...]:
                if space.directory is None:
                    return ()
                names = sorted(space.directory.names(deadline))
                return tuple(
                    {"target": FileRef(root_ref=query.directory.root_ref, relative_path=name).model_dump()}
                    for name in names
                    if not name.startswith(".upload-")
                )

            def project(entry: dict[str, JsonValue]) -> dict[str, JsonValue]:
                return self._read("files.stat", entry, deadline)

            result = self._pages.page(
                query.model_dump_json(exclude={"cursor"}),
                str(lease.grant_epoch),
                query.cursor,
                query.limit,
                load,
                project=project,
            )
            self._check(lease, deadline)
            return result

    @staticmethod
    def _absent(directory: FileDirectory, name: str) -> None:
        """
        只提供提前失败提示；真正的不覆盖保证由最终原生重命名执行。
        """
        try:
            stream = directory.file(name)
        except FileBoundaryError as exc:
            if exc.error_code == "PLUGIN_RESOURCE_NOT_FOUND":
                return
            raise
        stream.close()
        raise FileBoundaryError("文件已存在", "PLUGIN_FILE_EXISTS")

    def _begin(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        先验证根和精确目标，再登记预留并独占创建暂存；清理失败保留资源归属。
        """
        query = FileWriteBeginRequest.model_validate(params)
        lease = self._session.authorize("filesystem.write")
        upload = self.prepare(query.target, lease, query.total_bytes, query.total_bytes, deadline)
        return {"write_ref": upload.write_ref, "max_chunk_bytes": 32768}

    def prepare(
        self,
        target: FileRef,
        lease: GrantLease,
        reserved_bytes: int,
        expected_bytes: int | None,
        deadline: float,
        *,
        additional_leases: tuple[GrantLease, ...] = (),
    ) -> StagedFile:
        """
        为上传或下载绑定精确私有目标、预留容量并创建拥有唯一归属的原生暂存。

        :param target: 当前运行的非空文件引用
        :param lease: 原始写准入
        :param reserved_bytes: 共享配额中的预留上限
        :param expected_bytes: 已知精确长度；未知长度下载为 None
        :param deadline: 准备截止
        :param additional_leases: 准备动作同时需要的原始执行准入
        :return: 已登记归属的暂存
        :raises FileBoundaryError: 目标、配额或原生文件边界不满足时抛出
        """
        if not target.relative_path:
            raise FileBoundaryError("新文件必须有名称", "PLUGIN_SCHEMA_INVALID")
        leases = (lease, *additional_leases)

        def accept() -> None:
            self._session.accept_all(leases, self._ensure_open)

        self._session.check_all(leases)
        # 根创建是先接受的有限准备动作；随后仍需接受具体上传，不借初始化自动授权发布。
        with self._storage.access(self._installation_id) as existing:
            self._resolve(target, lease, existing.identity)
            self._check(lease, deadline)
        with self._storage.access(self._installation_id, initialize=True, admit=accept) as space:
            assert space.directory is not None
            with self._lock:
                self._root_identity = space.identity
            self._resolve(target, lease, space.identity)
            self._check(lease, deadline)
            self._absent(space.directory, target.relative_path)
            self._storage.check_quota(
                self._installation_id, space.directory, deadline, reserved_bytes, is_new_upload=True
            )
            upload = StagedFile(target, lease, space.directory, reserved_bytes, expected_bytes)
            with upload.io_lock:

                def reserve() -> None:
                    self._ensure_open()
                    self._storage.reserve(upload)

                self._session.accept_all(leases, reserve)
                try:

                    def own_stream(stream: FileHandle) -> None:
                        upload.stream = stream

                    space.directory.file(upload.temporary_name, create=True, on_create=own_stream)
                    self._check(lease, deadline)
                except BaseException:
                    self._storage.cleanup(upload)
                    raise
            return upload

    def cleanup(self, upload: StagedFile) -> bool:
        """
        只减少当前运行暂存资源，不要求已经撤销的写权限。

        :param upload: 受信适配器持有的暂存
        :return: 真实句柄和暂存已释放时为 True
        """
        return self._storage.cleanup(upload)

    def write_staged(self, upload: StagedFile, block: bytes) -> None:
        """
        为已接纳任务顺序写入有限块，调用方已在网络接受点核验原始双权限。

        :param upload: 任务拥有的原生暂存
        :param block: 至多 32 KiB 的实体字节
        :raises FileBoundaryError: 归属、长度或原生写入失败时抛出
        """
        with upload.io_lock:
            if upload.owner != "task" or upload.stream is None:
                raise FileBoundaryError("暂存已交付或释放", "PLUGIN_RESOURCE_NOT_FOUND")
            if len(block) > 32768 or upload.received_bytes + len(block) > upload.reserved_bytes:
                raise FileBoundaryError("插件下载文件超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
            upload.stream.write(upload.received_bytes, block)
            upload.received_bytes += len(block)

    def _upload(self, reference: str) -> StagedFile:
        """
        只允许当前宿主 run 查找上传，其他来源和已释放引用无法区分。
        """
        upload = self._storage.find(reference, self._session.run_id)
        if upload is None:
            raise FileBoundaryError("未找到自身上传", "PLUGIN_RESOURCE_NOT_FOUND")
        return upload

    def _check_upload(self, upload: StagedFile, deadline: float) -> None:
        """
        调用方持有上传 I/O 锁，旧租约或空闲过期不得换新后继续。
        """
        if upload.owner != "upload":
            raise FileBoundaryError("上传已交付或释放", "PLUGIN_RESOURCE_NOT_FOUND")
        if monotonic() - upload.touched_at >= 60:
            self._storage.cleanup(upload)
            raise FileBoundaryError("上传已过期", "PLUGIN_RESOURCE_NOT_FOUND")
        self._check(upload.lease, deadline)

    def _chunk(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        严格校验编码和顺序后接受一个有限块；实际写入失败不放弃资源跟踪。
        """
        query = FileWriteChunkRequest.model_validate(params)
        try:
            block = base64.b64decode(query.data_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise FileBoundaryError("上传块编码无效", "PLUGIN_SCHEMA_INVALID") from exc
        if not block or len(block) > 32768 or base64.b64encode(block).decode("ascii") != query.data_b64:
            raise FileBoundaryError("上传块大小或编码无效", "PLUGIN_SCHEMA_INVALID")
        upload = self._upload(query.write_ref)
        with upload.io_lock:
            self._check_upload(upload, deadline)
            if query.offset != upload.received_bytes:
                raise FileBoundaryError("上传偏移不连续", "PLUGIN_WRITE_SEQUENCE_INVALID")
            if upload.expected_bytes is None or upload.received_bytes + len(block) > upload.expected_bytes:
                raise FileBoundaryError("上传超过声明大小", "PLUGIN_WRITE_SIZE_EXCEEDED")
            self._accept(upload.lease)
            assert upload.stream is not None
            try:
                upload.stream.write(query.offset, block)
                upload.received_bytes += len(block)
                upload.touched_at = monotonic()
                self._check(upload.lease, deadline)
            except BaseException:
                self._storage.cleanup(upload)
                raise
            return {"received_bytes": upload.received_bytes}

    def publish(
        self, upload: StagedFile, digest: str, context: OperationContext, *, deadline: float | None = None
    ) -> _PublishedFile:
        """
        先取得暂存最终快照，再接受一次原生发布；发布后只交付捕获的快照。

        :param upload: 已交付任务的唯一暂存
        :param digest: 接收过程的完整摘要
        :param context: 任务真实取消与提交上下文
        :param deadline: 可选任务总截止，与本地 30 秒预算取较早值
        :return: 发布前捕获的不可变快照
        :raises FileBoundaryError: 长度、摘要、身份或不覆盖发布失败时抛出
        """
        deadline = min(deadline, monotonic() + 30) if deadline is not None else monotonic() + 30
        # 统一锁序为安装目录 → 上传，避免与 begin 的配额查询、退出清理相互等待。
        with self._storage.access(self._installation_id) as space, upload.io_lock:
            context.check_cancelled()
            assert upload.stream is not None and space.directory is not None and space.identity is not None
            if upload.directory.identity != space.identity:
                raise FileBoundaryError("上传根身份变化", "PLUGIN_FILE_CHANGED")
            self._storage.check_quota(self._installation_id, space.directory, deadline)
            information = upload.stream.information()
            if information.identity != upload.stream.identity or information.size != upload.received_bytes:
                raise FileBoundaryError("暂存大小或身份变化", "PLUGIN_FILE_CHANGED")
            actual = upload.stream.digest(deadline)
            if actual != digest:
                raise FileBoundaryError("文件摘要不符", "PLUGIN_HASH_MISMATCH")
            upload.stream.flush()
            information = upload.stream.information()
            self._check(upload.lease, deadline)
            context.check_cancelled()
            published = _PublishedFile(upload.target, space.identity, information, actual)

            def publish() -> None:
                assert upload.stream is not None
                try:
                    upload.stream.publish(upload.target.relative_path)
                except FileBoundaryError as exc:
                    if exc.error_code == "PLUGIN_FILE_EXISTS":
                        raise OperationCommitRejectedError("目标已存在，文件未发布", exc.error_code) from exc
                    raise

            context.finalize(publish)
            return published

    def project(self, result: object) -> JsonValue:
        """
        仅从已发布快照构建视图；根核验失败隐藏结果，不改变已提交任务状态。

        :param result: 任务保存的已发布快照
        :return: 不含真实路径的文件视图
        :raises FileBoundaryError: 快照类型或根身份无效时抛出
        """
        if not isinstance(result, _PublishedFile):
            raise FileBoundaryError("文件结果类型无效", "PLUGIN_FILE_INVALID")
        with self._storage.access(self._installation_id) as space:
            if space.identity != result.root_identity:
                raise FileBoundaryError("发布根身份变化", "PLUGIN_FILE_CHANGED")
            return self._view(result.target, result.information, result.digest)

    def _commit(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        持有上传锁完成任务归属转移；拒绝接纳时仍保留可 abort 的上传资源。
        """
        query = FileWriteCommitRequest.model_validate(params)
        upload = self._upload(query.write_ref)
        with upload.io_lock:
            self._check_upload(upload, deadline)
            if upload.received_bytes != upload.expected_bytes:
                raise FileBoundaryError("上传尚未完成", "PLUGIN_WRITE_SEQUENCE_INVALID")
            try:
                result_lease = self._session.authorize("filesystem.read")
            except PluginGrantError as exc:
                if exc.error_code != "PLUGIN_PERMISSION_DENIED":
                    raise
                result_lease = None
            upload.owner = "task"
            try:
                reference = self._operations.submit(
                    "plugin_file_write",
                    lambda context: self.publish(upload, query.sha256, context),
                    upload.lease,
                    project_result=self.project,
                    result_lease=result_lease,
                    cleanup=lambda: self._storage.cleanup(upload),
                    initial_effect="staged",
                )
            except BaseException:
                upload.owner = "upload"
                raise
            return {"operation_ref": reference, "state": "queued"}

    def _abort(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        减少自身上传资源无需执行权限或新增回执，已交付任务必须走任务取消。
        """
        query = FileWriteRef.model_validate(params)
        upload = self._storage.find(query.write_ref, self._session.run_id)
        if upload is None:
            return {"released": False}
        with upload.io_lock:
            if upload.owner in {"task", "closed"}:
                return {"released": False}
            return {"released": self._storage.cleanup(upload)}

    def _delete(self, params: dict[str, JsonValue], submission_deadline: float) -> dict[str, JsonValue]:
        """
        只登记名称占用和原始准入，不在 RPC 中打开或哈希正式目标。
        """
        deadline = monotonic() + 30
        query = FileDeleteRequest.model_validate(params)
        lease = self._session.authorize("filesystem.delete")
        with self._storage.access(self._installation_id) as space:
            self._resolve(query.target, lease, space.identity)
            self._check(lease, submission_deadline)
            if space.directory is None or space.identity is None:
                raise FileBoundaryError("插件文件不存在", "PLUGIN_RESOURCE_NOT_FOUND")
            deletion = FileDeletion(query.target, lease, space.directory, space.identity)
            self._session.accept(lease, lambda: self._storage.reserve_deletion(deletion))
        try:
            reference = self._operations.submit(
                "plugin_file_delete",
                lambda context: self._execute_delete(deletion, query.expected_etag, deadline, context),
                lease,
                project_result=lambda result: {"deleted": True} if result is True else None,
                result_lease=lease,
                cleanup=lambda: self._storage.cleanup_deletion(deletion),
                deadline=deadline,
                submission_deadline=submission_deadline,
            )
        except BaseException:
            self._storage.cleanup_deletion(deletion)
            raise
        return {"operation_ref": reference, "state": "queued"}

    @staticmethod
    def _mark_delete(deletion: FileDeletion, stream: FileHandle) -> None:
        """
        标记并关闭原对象；只有确认撤回且关闭后才将失败归类为零效果。
        """
        deletion.needs_rollback = True
        try:
            stream.mark_deletion(True)
            if not stream.deletion_pending():
                raise FileBoundaryError("文件删除标记未确认", "PLUGIN_FILE_IO_FAILED")
            stream.close()
        except FileBoundaryError as exc:
            if not stream.is_closed:
                try:
                    stream.mark_deletion(False)
                    if stream.deletion_pending():
                        raise FileBoundaryError("删除撤回未确认", "PLUGIN_FILE_IO_FAILED")
                    stream.close()
                    deletion.needs_rollback = False
                except FileBoundaryError as rollback_error:
                    raise exc from rollback_error
                raise OperationCommitRejectedError("删除未执行，原文件已保留", exc.error_code) from exc
            raise
        deletion.needs_rollback = False
        deletion.needs_observation = True

    def _execute_delete(
        self, deletion: FileDeletion, expected_etag: str, deadline: float, context: OperationContext
    ) -> bool:
        """
        在同一独占原对象上核对完整版本，接受后只观察其真实移除。
        """
        with self._storage.access(self._installation_id) as space, deletion.io_lock:
            self._resolve(deletion.target, deletion.lease, space.identity)
            self._check(deletion.lease, deadline)
            context.check_cancelled()
            if space.identity != deletion.root_identity or space.directory is not deletion.directory:
                raise FileBoundaryError("私有根对象已变化", "PLUGIN_FILE_CHANGED")
            stream = deletion.directory.delete_target(
                deletion.target.relative_path, lambda opened: setattr(deletion, "stream", opened)
            )
            information = stream.information()
            deletion.identity = information.identity
            if information.size > self._storage.max_file_bytes:
                raise FileBoundaryError("插件文件大小超限", "PLUGIN_FILE_TOO_LARGE")
            digest = stream.digest(deadline)
            final = stream.information()
            if final != information or information.etag(digest) != expected_etag:
                raise FileBoundaryError("插件文件版本已变化", "PLUGIN_FILE_CHANGED")
            deletion.directory.information()
            self._check(deletion.lease, deadline)
            context.check_cancelled()

        def remove() -> None:
            with self._storage.access(self._installation_id) as current, deletion.io_lock:
                if current.identity != deletion.root_identity or stream.information() != information:
                    raise OperationCommitRejectedError("删除前原对象已变化", "PLUGIN_FILE_CHANGED")
                self._mark_delete(deletion, stream)
            while monotonic() < deadline:
                current_identity = deletion.directory.entry_identity(deletion.target.relative_path, deadline)
                if current_identity != deletion.identity:
                    deletion.needs_observation = False
                    return
                sleep(min(0.025, max(0, deadline - monotonic())))
            raise FileBoundaryError("原文件仍等待内核删除", "PLUGIN_FILE_DELETE_PENDING")

        context.finalize(remove)
        return True

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        分派严格白名单，只允许固定私有根中的普通文件和新建事务。

        :param method: 宿主已接线方法
        :param params: 不可信 JSON 输入
        :param deadline: 已限制的本次调用截止时间
        :return: 白名单结果或任务接纳回执
        """
        self._ensure_open()
        if method == "resources.list":
            return self._resources(params, deadline)
        if method in {"files.stat", "files.read"}:
            return self._read(method, params, deadline)
        if method == "files.list":
            return self._list(params, deadline)
        if method == "files.write_begin":
            return self._begin(params, deadline)
        if method == "files.write_chunk":
            return self._chunk(params, deadline)
        if method == "files.write_commit":
            return self._commit(params, deadline)
        if method == "files.write_abort":
            return self._abort(params)
        if method == "files.delete":
            return self._delete(params, deadline)
        raise ProtocolError("此文件方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")

    def refresh_permissions(self) -> None:
        """
        只清理已失效、过期或待重试的上传；活动 I/O 不阻塞监控线程。
        """
        epoch = self._session.view().grant_epoch
        with self._lock:
            is_closed = self._closed
        for upload in self._storage.uploads(self._session.run_id):
            if not upload.io_lock.acquire(blocking=False):
                continue
            try:
                if upload.owner == "task":
                    continue
                if (
                    is_closed
                    or upload.owner == "cleanup"
                    or upload.lease.grant_epoch != epoch
                    or (monotonic() - upload.touched_at >= 60)
                ):
                    self._storage.cleanup(upload)
            finally:
                upload.io_lock.release()

    def close(self) -> None:
        """
        停止接纳并尝试清理空闲上传，实际 I/O 及任务仍由原归属者确认结束。
        """
        with self._lock:
            self._closed = True
        self._pages.clear()
        self.refresh_permissions()

    def is_idle(self) -> bool:
        """
        关闭期间重试清理，资源实际释放之前始终报告未结束。

        :return: 当前运行已无上传、暂存或待关闭的已发布句柄
        """
        self.refresh_permissions()
        return not self._storage.uploads(self._session.run_id) and not self._storage.deletions(self._session.run_id)
