# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：把获准实例内的模组、资源包和光影操作绑定到文件版本及自身任务。
#
# 公开接口：
#   - class RuntimeGameResources — 当前运行的资源列表、安装、启停和删除。
# ============================================================

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import Field, StrictBool, field_validator

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.runtime.game_models import GameInstallRequest
from ECL.plugins.runtime.models import FileRef, InstanceGetRequest, RequestModel
from ECL.plugins.runtime.resources import ResourcePages
from ECL.services.operations import OperationCommitRejectedError, OperationContext
from ECL.utils.windows_file_handles import FileBoundaryError, FileDirectory, FileHandle

if TYPE_CHECKING:
    from ECL.plugins.runtime.file_api import RuntimeFiles
    from ECL.plugins.runtime.operation_adapters import RuntimeOperations
    from ECL.plugins.runtime.resources import RuntimeResources
    from ECL.services.game.plugin_games import GameResourceTarget, PluginGameCoordinator


class _List(InstanceGetRequest):
    cursor: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=50, ge=1, le=100, strict=True)


class _Install(InstanceGetRequest):
    source: FileRef
    filename: str = Field(min_length=1, max_length=120)

    @field_validator("filename")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        GameInstallRequest.safe_version_name(value)
        return value


class _Reference(RequestModel):
    resource_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


class _Enabled(_Reference):
    enabled: StrictBool


@dataclass(frozen=True, slots=True)
class _Resource:
    instance_ref: str
    kind: str
    name: str
    etag: str


class RuntimeGameResources:
    """
    只解析当前运行的实例与文件引用，不接收主机路径或任意目标文件。
    """

    def __init__(
        self,
        resources: RuntimeResources,
        game: PluginGameCoordinator,
        files: RuntimeFiles,
        operations: RuntimeOperations,
    ) -> None:
        """
        注入应用现有的领域服务、私有源文件和同一运行的任务门面。

        :param resources: 当前运行的授权实例引用
        :param game: 游戏数据位置的唯一业务解析服务
        :param files: 当前运行的私有文件来源
        :param operations: 当前运行的任务创建与控制入口
        """
        self._resources = resources
        self._game = game
        self._files = files
        self._operations = operations
        self._lock = RLock()
        self._references: dict[str, _Resource] = {}
        self._pages = ResourcePages()

    def _verify(
        self, target: GameResourceTarget, lease: GrantLease, kind: str, directory_path: Path, deadline: float
    ) -> None:
        self._resources.verify_game_target(target, lease, deadline)
        if self._game.plugin_resource_path(target, kind) != directory_path:
            raise ProtocolError("实例资源位置已经变化", "PLUGIN_RESOURCE_CHANGED")

    @contextmanager
    def _directory(
        self, target: GameResourceTarget, lease: GrantLease, kind: str, deadline: float, *, create: bool = False
    ) -> Iterator[FileDirectory]:
        """
        从已固定游戏根逐层打开真实数据目录，拒绝链接及途中换绑。
        """
        directory_path = self._game.plugin_resource_path(target, kind)
        relative = directory_path.relative_to(target.root_path)
        self._verify(target, lease, kind, directory_path, deadline)
        with ExitStack() as stack:
            directory = FileDirectory.open(target.root_path)
            stack.callback(directory.close)
            for index, component in enumerate(relative.parts):
                should_create = (
                    create
                    and index == len(relative.parts) - 1
                    and not directory_path.exists()
                    and not directory_path.is_symlink()
                )
                directory = directory.child(component, create=should_create)
                stack.callback(directory.close)
            self._verify(target, lease, kind, directory_path, deadline)
            yield directory

    @staticmethod
    def _accepted_name(kind: str, name: str) -> bool:
        suffix = ".jar" if kind == "mods" else ".zip"
        return name.casefold().removesuffix(".disabled").endswith(suffix) and not name.startswith(".")

    def _reference(self, value: _Resource) -> str:
        with self._lock:
            reference = next((key for key, previous in self._references.items() if previous == value), None)
            if reference is None:
                if len(self._references) >= 1024:
                    raise ProtocolError("游戏资源引用数量已达上限", "PLUGIN_RATE_LIMITED")
                reference = uuid4().hex
                self._references[reference] = value
            return reference

    def _list(self, kind: str, query: _List, deadline: float) -> dict[str, JsonValue]:
        target, lease = self._resources.game_target(query.instance_ref, "instances.read", deadline)
        directory_path = self._game.plugin_resource_path(target, kind)

        def load() -> tuple[dict[str, JsonValue], ...]:
            if not directory_path.exists() and not directory_path.is_symlink():
                self._resources.verify_game_target(target, lease, deadline)
                return ()
            entries: list[dict[str, JsonValue]] = []
            with self._directory(target, lease, kind, deadline) as directory:
                for name in sorted(directory.names(deadline)):
                    if not self._accepted_name(kind, name):
                        continue
                    stream = directory.file(name)
                    try:
                        info = stream.information()
                        etag = info.etag(stream.digest(deadline, max_bytes=64 * 1024 * 1024))
                        reference = self._reference(_Resource(query.instance_ref, kind, name, etag))
                        entries.append(
                            {
                                "resource_ref": reference,
                                "filename": name,
                                "bytes": info.size,
                                "enabled": not name.endswith(".disabled"),
                                "etag": etag,
                            }
                        )
                    finally:
                        stream.close()
            return tuple(entries)

        result = self._pages.page(
            f"{kind}:{query.instance_ref}:{query.limit}", str(lease.grant_epoch), query.cursor, query.limit, load
        )
        self._resources.verify_game_target(target, lease, deadline)
        return result

    def _install(self, kind: str, query: _Install, submission_deadline: float) -> dict[str, JsonValue]:
        if not self._accepted_name(kind, query.filename) or query.filename.endswith(".disabled"):
            raise ProtocolError("安装文件类型不符合资源种类", "PLUGIN_SCHEMA_INVALID")
        target, lease = self._resources.game_target(query.instance_ref, "instances.manage", submission_deadline)
        directory_path = self._game.plugin_resource_path(target, kind)
        deadline = monotonic() + 300

        def worker(context: OperationContext) -> dict[str, JsonValue]:
            context.check_cancelled()
            self._verify(target, lease, kind, directory_path, deadline)
            with (
                self._files.read_source(query.source, deadline) as source,
                self._directory(target, lease, kind, deadline, create=True) as directory,
            ):
                if directory.entry_identity(query.filename, deadline) is not None:
                    raise ProtocolError("目标资源已存在，安装未覆盖", "PLUGIN_FILE_ALREADY_EXISTS")
                context.report_effect("staged")
                staged = directory.file(".plugin-install-" + uuid4().hex, create=True)
                try:
                    size = source.information().size
                    offset = 0
                    while offset < size:
                        context.check_cancelled()
                        if monotonic() >= deadline:
                            raise ProtocolError("资源安装已超时", "PLUGIN_DEADLINE_EXCEEDED")
                        block = source.read(offset, min(65536, size - offset))
                        if not block:
                            raise ProtocolError("私有源文件读取不完整", "PLUGIN_FILE_CHANGED")
                        staged.write(offset, block)
                        offset += len(block)
                        context.progress(offset * 90 / max(size, 1), "正在安装游戏资源", stage="copying")
                    staged.flush()
                    if source.digest(deadline, max_bytes=64 * 1024 * 1024) != staged.digest(
                        deadline, max_bytes=64 * 1024 * 1024
                    ):
                        raise ProtocolError("资源复制校验失败", "PLUGIN_FILE_CHANGED")
                    self._verify(target, lease, kind, directory_path, deadline)
                    context.finalize(lambda: staged.publish(query.filename))
                    return {"filename": query.filename, "installed": True}
                finally:
                    if staged.is_published:
                        staged.close()
                    else:
                        staged.discard()

        reference = self._operations.submit(
            "plugin_resource_install",
            worker,
            lease,
            result_lease=lease,
            project_result=lambda value: value,
            deadline=deadline,
            submission_deadline=submission_deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    @staticmethod
    def _delete(stream: FileHandle) -> None:
        """
        在提交闸门内标记原对象删除；关闭失败时尽可能撤回同一对象的标记。
        """
        try:
            stream.mark_deletion(True)
            if not stream.deletion_pending():
                raise FileBoundaryError("资源删除未确认", "PLUGIN_FILE_IO_FAILED")
            stream.close()
        except FileBoundaryError as error:
            if not stream.is_closed:
                stream.mark_deletion(False)
                if not stream.deletion_pending():
                    raise OperationCommitRejectedError("删除未执行，原资源已保留", error.error_code) from error
            raise

    def _mutate(
        self, kind: str, action: Literal["delete", "set_enabled"], query: _Reference, submission_deadline: float
    ) -> dict[str, JsonValue]:
        with self._lock:
            item = self._references.get(query.resource_ref)
        if item is None or item.kind != kind:
            raise ProtocolError("资源引用不属于当前运行或种类", "PLUGIN_RESOURCE_NOT_FOUND")
        target, lease = self._resources.game_target(item.instance_ref, "instances.manage", submission_deadline)
        directory_path = self._game.plugin_resource_path(target, kind)
        deadline = monotonic() + 60

        def worker(context: OperationContext) -> dict[str, JsonValue]:
            with self._directory(target, lease, kind, deadline) as directory:
                streams: list[FileHandle] = []
                try:
                    stream = directory.delete_target(item.name, streams.append)
                    etag = stream.information().etag(stream.digest(deadline, max_bytes=64 * 1024 * 1024))
                    if etag != item.etag:
                        raise ProtocolError("资源已变化，请重新获取列表", "PLUGIN_FILE_CHANGED")
                    context.check_cancelled()
                    self._verify(target, lease, kind, directory_path, deadline)
                    if action == "delete":
                        context.finalize(lambda: self._delete(stream))
                        return {"deleted": True}
                    assert isinstance(query, _Enabled)
                    name = (
                        item.name.removesuffix(".disabled")
                        if query.enabled
                        else item.name.removesuffix(".disabled") + ".disabled"
                    )
                    if name != item.name:
                        context.finalize(lambda: stream.move(name))
                    return {"filename": name, "enabled": query.enabled}
                finally:
                    for stream in streams:
                        stream.close()

        reference = self._operations.submit(
            "plugin_resource_delete" if action == "delete" else "plugin_mod_enabled",
            worker,
            lease,
            result_lease=lease,
            project_result=lambda value: value,
            deadline=deadline,
            submission_deadline=submission_deadline,
        )
        return {"operation_ref": reference, "state": "queued"}

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        严格分派三类资源的方法，不开放任意服务调用。

        :param method: 固定的资源列表、安装、删除或模组启停方法
        :param params: 仅含当前运行引用与明确参数的不可信请求
        :param deadline: 当前 RPC 截止
        :return: 安全列表或本运行任务回执
        :raises ProtocolError: 方法、引用、字段或准入不满足时抛出
        """
        kind, _, action = method.partition(".")
        if kind not in {"mods", "resourcepacks", "shaders"}:
            raise ProtocolError("游戏资源种类不支持", "PLUGIN_API_METHOD_NOT_FOUND")
        if action == "list":
            return self._list(kind, _List.model_validate(params), deadline)
        if action == "install":
            return self._install(kind, _Install.model_validate(params), deadline)
        if action == "delete":
            return self._mutate(kind, "delete", _Reference.model_validate(params), deadline)
        if kind == "mods" and action == "set_enabled":
            return self._mutate(kind, "set_enabled", _Enabled.model_validate(params), deadline)
        raise ProtocolError("游戏资源方法不支持", "PLUGIN_API_METHOD_NOT_FOUND")
