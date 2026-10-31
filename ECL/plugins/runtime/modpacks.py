# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在本运行任务中准备、安装及导出整合包，隔离预览、网络与私有输出。
#
# 公开接口：
#   - class RuntimeModpacks — 当前运行整合包预览、安装与导出门面。
# ============================================================

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import httpcore
from pydantic import Field, field_validator

from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.network_targets import NetworkOrigin
from ECL.plugins.runtime.game_models import GameInstallRequest
from ECL.plugins.runtime.models import FileRef, InstanceGetRequest, NetworkRequest, RequestModel
from ECL.plugins.runtime.resources import ResourcePages
from ECL.utils.operation_logging import private_transport_scope
from ECL.utils.windows_file_handles import FileDirectory

if TYPE_CHECKING:
    from ECL.plugins.runtime.file_api import RuntimeFiles
    from ECL.plugins.runtime.network_api import RuntimeNetwork
    from ECL.plugins.runtime.operation_adapters import RuntimeOperations
    from ECL.plugins.runtime.resources import RuntimeResources
    from ECL.services.game import GameService
    from ECL.services.game.modpack import PackFileEntry
    from ECL.services.operations import OperationContext


class _Prepare(RequestModel):
    source: FileRef


class _PreparedQuery(RequestModel):
    preparation_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    cursor: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


class _Install(RequestModel):
    root_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    preparation_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    version_name: str = Field(min_length=1, max_length=64)
    excluded_file_ids: list[str] = Field(default_factory=list, max_length=1024)

    @field_validator("version_name")
    @classmethod
    def _name(cls, value: str) -> str:
        GameInstallRequest.safe_version_name(value)
        return value


class _Export(RequestModel):
    preview_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    selected_file_ids: list[str] = Field(max_length=1024)
    destination: FileRef
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=1000)


class _ExportFiles(RequestModel):
    preview_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    cursor: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


@dataclass(frozen=True, slots=True)
class _Prepared:
    preparation_id: str
    lease: GrantLease


@dataclass(frozen=True, slots=True)
class _ExportPreview:
    instance_ref: str
    revision: str
    lease: GrantLease
    files: tuple[dict[str, JsonValue], ...]


class RuntimeModpacks:
    """
    复用整合包业务工作函数，所有控制引用、文件输出和任务均属于本运行。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        game: GameService,
        resources: RuntimeResources,
        files: RuntimeFiles,
        operations: RuntimeOperations,
        network: RuntimeNetwork,
        data_path: Path,
    ) -> None:
        """
        绑定现有服务，不为插件创建另一个任务或下载管理器。

        :param session: 当前运行授权
        :param game: 应用唯一游戏及整合包服务
        :param resources: 当前运行的实例与目录引用
        :param files: 当前运行私有文件读写
        :param operations: 同一 Broker 的任务门面
        :param network: 同一运行的站点授权、DNS 和真实连接边界
        :param data_path: 宿主数据根，用于仅当前调用拥有的临时文件
        """
        self._session, self._game, self._resources = session, game, resources
        self._files, self._operations, self._network = files, operations, network
        self._data_path = data_path
        self._lock = RLock()
        self._prepared: dict[str, _Prepared] = {}
        self._exports: dict[str, _ExportPreview] = {}
        self._pages = ResourcePages()

    def _prepared_record(self, reference: str) -> _Prepared:
        with self._lock:
            record = self._prepared.get(reference)
        if record is None:
            raise ProtocolError("整合包预览不属于当前运行", "PLUGIN_RESOURCE_NOT_FOUND")
        self._session.check(record.lease)
        return record

    def _record_prepared(self, value: object, lease: GrantLease) -> JsonValue:
        if not isinstance(value, dict) or not isinstance(value.get("preparationId"), str):
            raise ProtocolError("整合包准备结果无效", "PLUGIN_SCHEMA_INVALID")
        self._session.check(lease)
        preparation_id = value["preparationId"]
        with self._lock:
            reference = next(
                (key for key, record in self._prepared.items() if record.preparation_id == preparation_id), None
            )
            if reference is None:
                if len(self._prepared) >= 32:
                    self._game.release_modpack_preparation(preparation_id)
                    raise ProtocolError("整合包预览数量已达上限", "PLUGIN_RATE_LIMITED")
                reference = uuid4().hex
                self._prepared[reference] = _Prepared(preparation_id, lease)
        return {"preparation_ref": reference}

    def _prepare(self, request: _Prepare, deadline: float) -> dict[str, JsonValue]:
        lease = self._session.authorize("plugin.base")
        task_deadline = monotonic() + 120

        def worker(context: OperationContext) -> object:
            with closing(FileDirectory.open(self._data_path)) as parent:
                parent.information()
                with TemporaryDirectory(prefix="plugin-pack-", dir=self._data_path) as temporary:
                    archive = Path(temporary) / "source.zip"
                    with self._files.read_source(request.source, task_deadline) as source:
                        size = source.information().size
                        with archive.open("xb") as output:
                            offset = 0
                            while offset < size:
                                context.check_cancelled()
                                block = source.read(offset, min(65536, size - offset))
                                if not block:
                                    raise ProtocolError("整合包源文件读取不完整", "PLUGIN_FILE_CHANGED")
                                output.write(block)
                                offset += len(block)
                    return self._game.prepare_modpack("local", archive, context=context)

        reference = self._operations.submit(
            "plugin_modpack_prepare",
            worker,
            lease,
            result_lease=lease,
            project_result=lambda value: self._record_prepared(value, lease),
            deadline=task_deadline,
            submission_deadline=deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    def _get(self, request: _PreparedQuery) -> dict[str, JsonValue]:
        record = self._prepared_record(request.preparation_ref)
        value = _json_value(self._game.get_modpack_preparation(record.preparation_id))
        if not isinstance(value, dict) or not isinstance(value.get("preview"), dict):
            raise ProtocolError("整合包预览尚未就绪", "PLUGIN_RESOURCE_NOT_FOUND")
        preview = value["preview"]
        files = preview.get("files")
        if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
            raise ProtocolError("整合包文件预览无效", "PLUGIN_SCHEMA_INVALID")
        page = self._pages.page(
            "pack:" + request.preparation_ref + str(request.limit),
            str(record.lease.grant_epoch),
            request.cursor,
            request.limit,
            lambda: tuple(files),
        )
        self._session.check(record.lease)
        return {
            "preparation_ref": request.preparation_ref,
            "status": value["status"],
            "format": preview.get("format"),
            "name": preview.get("name"),
            "minecraft_version": preview.get("minecraftVersion"),
            "loader": preview.get("loader"),
            "file_count": len(files),
            **page,
        }

    def _download(
        self,
        entry: PackFileEntry,
        output: Path,
        context: OperationContext,
        leases: tuple[GrantLease, ...],
        deadline: float,
    ) -> None:
        """
        清单 URL 必须逐跳通过站点、DNS 和连接检查，不进入普通共享下载器。
        """
        origin, url = NetworkOrigin.from_url(str(entry.url))
        lease = self._session.authorize("network.read", (origin.reference,))
        maximum = 128 * 1024 * 1024
        if entry.size is not None and entry.size > maximum:
            raise ProtocolError("整合包单个下载文件超过上限", "PLUGIN_RESPONSE_TOO_LARGE")
        with private_transport_scope():
            call = self._network.service.begin(
                self._network,
                lease,
                deadline,
                additional_leases=leases,
                cancel_event=context.cancel_event,
                wire_limit=maximum + 1024 * 1024,
            )
            try:
                with self._network.stream(call, NetworkRequest(method="GET", url=url)) as (_, response):
                    if response.status != 200 or any(
                        k.lower() == b"content-encoding" and v.lower() != b"identity" for k, v in response.headers
                    ):
                        raise ProtocolError("整合包文件响应无效", "PLUGIN_NETWORK_FAILED")
                    received = 0
                    with output.open("xb") as stream:
                        for chunk in response.iter_stream():
                            call.accept()
                            received += len(chunk)
                            if received > maximum or (entry.size is not None and received > entry.size):
                                raise ProtocolError("整合包文件响应超出预算", "PLUGIN_RESPONSE_TOO_LARGE")
                            stream.write(chunk)
                            context.check_cancelled()
                            call.check()
                        if entry.size is not None and received != entry.size:
                            raise ProtocolError("整合包文件响应不完整", "PLUGIN_NETWORK_FAILED")
            except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException):
                call.check()
                raise ProtocolError("整合包文件连接未完成", "PLUGIN_NETWORK_FAILED") from None
            finally:
                call.finish()
            call.check()
            if not call.is_idle():
                raise ProtocolError("整合包下载尚未实际关闭", "PLUGIN_NETWORK_FAILED")

    def _install(self, request: _Install, deadline: float) -> dict[str, JsonValue]:
        prepared = self._prepared_record(request.preparation_ref)
        target, lease = self._resources.game_target(request.root_ref, "instances.manage", deadline)
        if target.version_id is not None:
            raise ProtocolError("整合包安装需要游戏根目录", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        name = request.version_name
        task_deadline = monotonic() + 1800

        def worker(context: OperationContext) -> object:
            with closing(FileDirectory.open(target.root_path)) as root:
                versions_path = target.root_path / "versions"
                versions = root.child("versions", create=not versions_path.exists())
                try:
                    self._resources.verify_game_target(target, lease, min(task_deadline, monotonic() + 5))
                    context.report_effect("partial")
                    result = self._game.install_prepared_modpack(
                        prepared.preparation_id,
                        target.root_path,
                        name,
                        request.excluded_file_ids,
                        context=context,
                        download=lambda entry, path, ctx: self._download(
                            entry, path, ctx, (lease, prepared.lease), task_deadline
                        ),
                    )
                    return {"version_id": name, "format": result.get("format"), "installed": True}
                finally:
                    versions.close()

        reference = self._operations.submit(
            "plugin_modpack_install",
            worker,
            lease,
            additional_leases=(prepared.lease,),
            result_lease=lease,
            project_result=lambda value: value,
            deadline=task_deadline,
            submission_deadline=deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    def _export_preview(self, request: InstanceGetRequest, deadline: float) -> dict[str, JsonValue]:
        target, lease = self._resources.game_target(request.instance_ref, "instances.read", deadline)

        def project(value: object) -> JsonValue:
            result = _json_value(value)
            if not isinstance(result, dict) or not isinstance(result.get("sourceRevision"), str):
                raise ProtocolError("整合包导出预览无效", "PLUGIN_SCHEMA_INVALID")
            files = result.get("files")
            if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
                raise ProtocolError("整合包导出文件预览无效", "PLUGIN_SCHEMA_INVALID")
            self._session.check(lease)
            with self._lock:
                reference = next(
                    (
                        key
                        for key, entry in self._exports.items()
                        if entry.instance_ref == request.instance_ref
                        and entry.revision == result["sourceRevision"]
                        and entry.lease == lease
                    ),
                    None,
                )
                if reference is None:
                    if len(self._exports) >= 32:
                        raise ProtocolError("导出预览数量已达上限", "PLUGIN_RATE_LIMITED")
                    reference = uuid4().hex
                    self._exports[reference] = _ExportPreview(
                        request.instance_ref, result["sourceRevision"], lease, tuple(files)
                    )
            return {
                "preview_ref": reference,
                "file_count": len(files),
                **self._export_files(_ExportFiles(preview_ref=reference)),
            }

        reference = self._operations.submit(
            "plugin_modpack_export_preview",
            lambda context: self._game.preview_instance_export(target.root_path, target.version_id, context=context),
            lease,
            result_lease=lease,
            project_result=project,
            deadline=monotonic() + 120,
            submission_deadline=deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    def _export_files(self, request: _ExportFiles) -> dict[str, JsonValue]:
        with self._lock:
            preview = self._exports.get(request.preview_ref)
        if preview is None:
            raise ProtocolError("导出预览不属于当前运行", "PLUGIN_RESOURCE_NOT_FOUND")
        self._session.check(preview.lease)
        return self._pages.page(
            "export:" + request.preview_ref + str(request.limit),
            str(preview.lease.grant_epoch),
            request.cursor,
            request.limit,
            lambda: preview.files,
        )

    def _export(self, request: _Export, deadline: float) -> dict[str, JsonValue]:
        with self._lock:
            preview = self._exports.get(request.preview_ref)
        if preview is None:
            raise ProtocolError("导出预览不属于当前运行", "PLUGIN_RESOURCE_NOT_FOUND")
        self._session.check(preview.lease)
        target, read = self._resources.game_target(preview.instance_ref, "instances.read", deadline)
        write = self._session.authorize("plugin.base")
        if not request.destination.relative_path.endswith(".mrpack"):
            raise ProtocolError("整合包输出必须为 mrpack", "PLUGIN_SCHEMA_INVALID")
        task_deadline = monotonic() + 300

        def worker(context: OperationContext) -> object:
            self._resources.verify_game_target(target, read, min(task_deadline, monotonic() + 5))
            with closing(FileDirectory.open(self._data_path)) as parent:
                parent.information()
                with TemporaryDirectory(prefix="plugin-export-", dir=self._data_path) as temporary:
                    output = Path(temporary) / "export.mrpack"

                    def stage(action: Callable[[], object]) -> object:
                        """
                        只发布宿主临时导出文件；最终私有输出仍经原始任务提交闸门。
                        """
                        context.check_cancelled()
                        return action()

                    staged_context = replace(context, finalize_callback=stage)
                    self._game.export_instance_pack(
                        target.root_path,
                        target.version_id,
                        output,
                        "modrinth",
                        source_revision=preview.revision,
                        selected_file_ids=request.selected_file_ids,
                        pack_name=request.name,
                        pack_version=request.version,
                        summary=request.summary,
                        resource_mode="bundled",
                        context=staged_context,
                    )
                    size = output.stat().st_size
                    upload = self._files.prepare(
                        request.destination, write, size, size, task_deadline, additional_leases=(read,)
                    )
                    try:
                        with upload.io_lock:
                            upload.owner = "task"
                        digest = hashlib.sha256()
                        with output.open("rb") as source:
                            while block := source.read(32768):
                                context.check_cancelled()
                                self._files.write_staged(upload, block)
                                digest.update(block)
                        self._resources.verify_game_target(target, read, min(task_deadline, monotonic() + 5))
                        return self._files.publish(upload, digest.hexdigest(), context, deadline=task_deadline)
                    finally:
                        self._files.cleanup(upload)

        reference = self._operations.submit(
            "plugin_modpack_export",
            worker,
            write,
            additional_leases=(read, preview.lease),
            result_lease=write,
            project_result=self._files.project,
            deadline=task_deadline,
            submission_deadline=deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        严格分派整合包操作，不接受真实任务标识、路径或下载源覆盖。

        :param method: 明确开放的整合包方法
        :param params: 当前运行引用与已限定选项
        :param deadline: 当前 RPC 截止
        :return: 安全预览或自身任务回执
        :raises ProtocolError: 方法、参数、资源或权限不满足时抛出
        """
        if method == "modpacks.prepare":
            return self._prepare(_Prepare.model_validate(params), deadline)
        if method == "modpacks.get":
            return self._get(_PreparedQuery.model_validate(params))
        if method == "modpacks.install":
            return self._install(_Install.model_validate(params), deadline)
        if method == "modpacks.export_preview":
            return self._export_preview(InstanceGetRequest.model_validate(params), deadline)
        if method == "modpacks.export_files":
            return self._export_files(_ExportFiles.model_validate(params))
        if method == "modpacks.export":
            return self._export(_Export.model_validate(params), deadline)
        raise ProtocolError("整合包方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")

    def close(self) -> None:
        """
        释放本运行已登记的预览；执行中的记录由领域服务等待实际结束。
        """
        with self._lock:
            records = tuple(self._prepared.values())
            self._prepared.clear()
            self._exports.clear()
        for record in records:
            self._game.release_modpack_preparation(record.preparation_id)
        self._pages.clear()
