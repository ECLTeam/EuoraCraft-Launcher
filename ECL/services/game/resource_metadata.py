# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按来源和配置隔离在线项目详情缓存，合并在途读取并限制存储容量。
#
# 公开接口：
#   - class ProjectMetadata — 已校验的公开项目详情。
#   - class ResourceMetadataError — 缓存关闭、繁忙或等待超时的领域失败。
#   - class ResourceMetadataCache — 应用生命周期内的有界详情缓存。
# ============================================================

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProjectMetadata(BaseModel):
    """
    表示可展示的资源项目详情，不保存平台凭据或任意第三方响应字段。
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)
    id: str = Field(min_length=1)
    slug: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    body: str = ""
    icon_url: str | None = Field(default=None, alias="iconUrl")
    code: str = ""
    source: Literal["modrinth", "curseforge", "ftb"]
    resource_type: str | None = Field(default=None, alias="resourceType")
    loaders: tuple[str, ...] = ()
    game_versions: tuple[str, ...] = Field(default=(), alias="gameVersions")
    project_url: str = Field(default="", alias="projectUrl")


class ResourceMetadataError(ValueError):
    """
    提供不包含请求内容的稳定元数据失败及错误码。
    """

    def __init__(self, message: str, error_code: str) -> None:
        """
        保存可公开的失败摘要和稳定错误码，不携带外部响应内容。

        :param message: 面向调用方的失败说明
        :param error_code: 稳定的领域错误码
        """
        super().__init__(message)
        self.error_code: str = error_code


@dataclass(frozen=True, slots=True)
class _MetadataEntry:
    expires_at: float
    snapshot: ProjectMetadata


class ResourceMetadataCache:
    """
    在应用生命周期内拥有详情有效期与在途合并，不执行额外预热。

    加载函数在请求所属线程执行。关闭只停止接收及缓存回填，不宣称已发出的
    网络请求已经停止；已有等待方会获得关闭错误，线程由加载方的超时管理。
    """

    maximum_pending: ClassVar[int] = 64
    maximum_entry_bytes: ClassVar[int] = 2 * 1024 * 1024

    def __init__(self, *, ttl_seconds: float = 600, capacity: int = 256, wait_seconds: float = 60) -> None:
        """
        创建独立的有界缓存，不创建线程或发起网络请求。

        :param ttl_seconds: 成功详情的有效时间
        :param capacity: 最大成功快照数量
        :param wait_seconds: 同键等待方的最大等待时间
        """
        self._ttl_seconds = max(0, ttl_seconds)
        self._capacity = max(1, capacity)
        self._wait_seconds = max(0.1, wait_seconds)
        self._entries: OrderedDict[tuple[str, ...], _MetadataEntry] = OrderedDict()
        self._pending: dict[tuple[str, ...], Future[ProjectMetadata]] = {}
        self._lock = RLock()
        self._is_closed = False

    def get(self, key: tuple[str, ...], load: Callable[[], object]) -> ProjectMetadata:
        """
        读取校验后的公开详情，同键等待方复用当前请求，失败结果不缓存。

        :param key: 来源、项目、资源类型及影响响应的配置组成的身份
        :param load: 负责网络超时并返回外部响应的加载函数
        :return: 不可变且已校验的公开详情
        :raises ResourceMetadataError: 缓存关闭、容量繁忙、等待超时或外部结构无效
        """
        with self._lock:
            if self._is_closed:
                raise ResourceMetadataError("资源详情服务已关闭", "RESOURCE_METADATA_CLOSED")
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > monotonic():
                self._entries.move_to_end(key)
                return entry.snapshot
            self._entries.pop(key, None)
            pending = self._pending.get(key)
            is_owner = pending is None
            if pending is None:
                if len(self._pending) >= self.maximum_pending:
                    raise ResourceMetadataError("资源详情请求繁忙", "RESOURCE_METADATA_BUSY")
                pending = Future()
                self._pending[key] = pending
        if not is_owner:
            try:
                return pending.result(timeout=self._wait_seconds)
            except TimeoutError as exc:
                raise ResourceMetadataError("等待资源详情超时", "RESOURCE_METADATA_TIMEOUT") from exc
        try:
            try:
                snapshot = ProjectMetadata.model_validate(load())
            except ValidationError as exc:
                raise ResourceMetadataError("资源详情格式无效", "RESOURCE_METADATA_INVALID") from exc
            self._publish(key, snapshot, pending)
            return snapshot
        except BaseException as exc:
            with self._lock:
                if not pending.done():
                    pending.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._pending.get(key) is pending:
                    del self._pending[key]

    def _publish(self, key: tuple[str, ...], snapshot: ProjectMetadata, pending: Future[ProjectMetadata]) -> None:
        """
        在关闭检查与容量淘汰的同一锁内发布结果，不让迟到网络响应恢复已关闭缓存。
        """
        with self._lock:
            if self._is_closed:
                raise ResourceMetadataError("资源详情服务已关闭", "RESOURCE_METADATA_CLOSED")
            if len(snapshot.model_dump_json().encode("utf-8")) <= self.maximum_entry_bytes:
                self._entries[key] = _MetadataEntry(monotonic() + self._ttl_seconds, snapshot)
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
            if not pending.done():
                pending.set_result(snapshot)

    def close(self) -> None:
        """
        幂等释放快照并结束等待方，不把缓存关闭当作网络线程已经停止。
        """
        with self._lock:
            self._is_closed = True
            self._entries.clear()
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(ResourceMetadataError("资源详情服务已关闭", "RESOURCE_METADATA_CLOSED"))
            self._pending.clear()
