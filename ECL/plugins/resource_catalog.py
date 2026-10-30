# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原子维护用户配置目录与实例的物理身份，失效身份不会按同名路径恢复授权。
#
# 公开接口：
#   - class PluginResource — 宿主持有的持久资源身份。
#   - class PluginResourceCatalog — 发现、验证及退休插件可审阅资源。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from ECL.plugins.environment_pool import _exclusive_lock
from ECL.utils import atomic_write_text
from ECL.utils.instance_reading import DirectoryIdentity, InstanceReadBoundary, InstanceReadError, directory_identity


class PluginResource(BaseModel):
    """
    保存随机持久身份及物理对象，只由宿主构造，不直接交给 Worker。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    resource_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_path: str = Field(min_length=1, max_length=32768)
    root_identity: DirectoryIdentity
    version_id: str | None = Field(default=None, min_length=1, max_length=64)
    identity: DirectoryIdentity
    is_active: bool

    @property
    def label(self) -> str:
        """
        返回不含个人完整路径的资源名称。

        :return: 目录短名称或实例目录名称
        """
        return (self.version_id or Path(self.root_path).name or "Minecraft")[:120]


class _CatalogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: StrictInt = Field(ge=1, le=1)
    revision: StrictInt = Field(ge=0, le=2**53 - 1)
    resources: list[PluginResource] = Field(max_length=16384)


class PluginResourceCatalog:
    """
    仅管理现有配置中的资源；发现可登记身份，业务刷新只能退休失效资源。
    """

    def __init__(
        self,
        data_path: Path,
        roots: Callable[[], tuple[Path, ...]],
        names: Callable[[Path, float], tuple[str, ...]],
    ) -> None:
        """
        接收现有配置和游戏查询入口，不主动访问游戏目录。

        :param data_path: 宿主数据目录
        :param roots: 当前有效配置的目录快照
        :param names: 无写入的实例名称查询
        """
        self._file = data_path / "plugin_resources.json"
        self._roots = roots
        self._names = names
        self._lock = RLock()
        self._record = _CatalogRecord(schema_version=1, revision=0, resources=[])
        self._is_loaded = False

    def _read(self) -> _CatalogRecord:
        """
        严格读取目录索引，损坏时保留原文件并拒绝相关能力。
        """
        try:
            if not self._file.exists() and not self._file.is_symlink():
                if self._is_loaded and self._record.revision > 0:
                    raise ValueError("已存在的资源目录索引丢失")
                return _CatalogRecord(schema_version=1, revision=0, resources=[])
            if self._file.is_symlink() or self._file.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("资源索引文件无效")
            record = _CatalogRecord.model_validate_json(self._file.read_bytes())
            if len({item.resource_id for item in record.resources}) != len(record.resources):
                raise ValueError("资源标识重复")
            if record.revision < self._record.revision:
                raise ValueError("资源索引版本回退")
            self._validate_record_change(record)
            return record
        except (OSError, ValueError, ValidationError) as exc:
            raise InstanceReadError("插件资源目录无法读取", "PLUGIN_RESOURCE_CATALOG_INVALID") from exc

    def _validate_record_change(self, record: _CatalogRecord) -> None:
        """
        已观察到的身份不能被换绑或恢复，也不能用相同版本覆盖不同索引。
        """
        if not self._is_loaded:
            return
        if record.revision == self._record.revision and record != self._record:
            raise ValueError("资源索引内容变化但版本未变")
        current_by_id = {item.resource_id: item for item in record.resources}
        for previous in self._record.resources:
            current = current_by_id.get(previous.resource_id)
            if current is None or previous.model_dump(exclude={"is_active"}) != current.model_dump(
                exclude={"is_active"}
            ):
                raise ValueError("已有资源身份被删除或换绑")
            if not previous.is_active and current.is_active:
                raise ValueError("已退休的资源身份不能恢复")

    def _configured(self) -> dict[str, DirectoryIdentity]:
        configured: dict[str, DirectoryIdentity] = {}
        for configured_path in self._roots():
            try:
                root_path = configured_path.expanduser().resolve(strict=True)
                configured[str(root_path)] = directory_identity(root_path)
            except (OSError, InstanceReadError):
                continue
        if len(configured) > 128:
            raise InstanceReadError("游戏目录数量超过本轮支持范围", "PLUGIN_MESSAGE_TOO_LARGE")
        return configured

    @staticmethod
    def _valid(resource: PluginResource, configured: dict[str, DirectoryIdentity]) -> bool:
        if not resource.is_active or configured.get(resource.root_path) != resource.root_identity:
            return False
        try:
            root_path = Path(resource.root_path)
            if resource.version_id is None:
                return directory_identity(root_path) == resource.identity
            guard = InstanceReadBoundary(root_path, monotonic() + 5)
            return guard.directory(Path("versions") / resource.version_id) == resource.identity
        except (OSError, InstanceReadError):
            return False

    def refresh(self, *, discover: bool = False, parent_ref: str | None = None) -> int:
        """
        同步物理身份并退休失效项；只有可信审阅查询可以发现新身份。

        :param discover: 是否登记当前配置和所展开目录中的新资源
        :param parent_ref: 管理页请求展开的目录持久身份
        :return: 当前目录 revision
        :raises InstanceReadError: 索引、父目录或扫描失败时抛出
        """
        configured = self._configured()
        with self._lock:
            record = self._read()
        # 在目录锁和授权锁外读取元数据，提交时以 revision 拒绝覆盖并发发现。
        resources = [
            item if self._valid(item, configured) else item.model_copy(update={"is_active": False})
            for item in record.resources
        ]
        if discover:
            active_roots = {item.root_path for item in resources if item.is_active and item.version_id is None}
            resources.extend(
                PluginResource(
                    resource_id=uuid4().hex, root_path=root, root_identity=identity, identity=identity, is_active=True
                )
                for root, identity in configured.items()
                if root not in active_roots
            )
            if parent_ref is not None:
                self._discover_instances(resources, parent_ref)
        if len(resources) > 16384:
            raise InstanceReadError("资源目录数量超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        if configured != self._configured():
            raise InstanceReadError("资源配置已变化，请刷新", "PLUGIN_RESOURCE_REVISION_CONFLICT")
        with self._lock, _exclusive_lock(self._file.with_suffix(".lock")):
            latest = self._read()
            if latest.revision != record.revision:
                if resources != record.resources:
                    raise InstanceReadError("资源目录已变化，请刷新", "PLUGIN_RESOURCE_REVISION_CONFLICT")
                self._record = latest
                self._is_loaded = True
                return latest.revision
            if resources != record.resources:
                record = _CatalogRecord(schema_version=1, revision=record.revision + 1, resources=resources)
                content = record.model_dump_json()
                if len(content.encode("utf-8")) > 8 * 1024 * 1024:
                    raise InstanceReadError("资源索引超过保存上限", "PLUGIN_MESSAGE_TOO_LARGE")
                atomic_write_text(self._file, content)
            self._record = record
            self._is_loaded = True
            return record.revision

    def _discover_instances(self, resources: list[PluginResource], parent_ref: str) -> None:
        """
        为可信界面展开一个目录，不读取未展开目录中的实例资料。
        """
        parent = next((item for item in resources if item.resource_id == parent_ref), None)
        if parent is None or not parent.is_active or parent.version_id is not None:
            raise InstanceReadError("所选游戏目录已经失效")
        root_path = Path(parent.root_path)
        existing = {item.version_id for item in resources if item.is_active and item.root_path == parent.root_path}
        deadline = monotonic() + 5
        guard = InstanceReadBoundary(root_path, deadline)
        for name in self._names(root_path, deadline):
            if name in existing:
                continue
            identity = guard.directory(Path("versions") / name)
            resources.append(
                PluginResource(
                    resource_id=uuid4().hex,
                    root_path=parent.root_path,
                    root_identity=parent.identity,
                    version_id=name,
                    identity=identity,
                    is_active=True,
                )
            )

    def active_refs(self) -> frozenset[str]:
        """
        返回已刷新索引中的有效身份，不在授权锁内执行文件操作。

        :return: 当前有效的资源 ID
        """
        with self._lock:
            return frozenset(item.resource_id for item in self._record.resources if item.is_active)

    def active_instance_refs(self) -> frozenset[str]:
        """
        返回有效具体实例身份，供写授权校验，目录许可不能扩大为写范围。

        :return: 不包含目录的宿主资源ID集合
        """
        with self._lock:
            return frozenset(
                item.resource_id for item in self._record.resources if item.is_active and item.version_id is not None
            )

    def active_root_refs(self) -> frozenset[str]:
        """
        返回可独立批准安装的游戏根目录，不把具体实例当作安装根。

        :return: 仅含有效目录的宿主资源ID
        """
        with self._lock:
            return frozenset(
                item.resource_id for item in self._record.resources if item.is_active and item.version_id is None
            )

    def default_root_ref(self) -> str | None:
        """
        返回配置提供器按当前选择排序后的首个有效根，仅用于确认界面的默认选择。

        :return: 当前有效根的持久身份，没有有效配置时为空
        """
        configured = self._configured()
        first = next(iter(configured), None)
        with self._lock:
            return next(
                (
                    item.resource_id
                    for item in self._record.resources
                    if item.is_active and item.version_id is None and item.root_path == first
                ),
                None,
            )

    def resources(self) -> tuple[PluginResource, ...]:
        """
        返回不可变资源快照供可信审阅和运行映射使用。

        :return: 包括已退休条目的资源快照
        """
        with self._lock:
            return tuple(self._record.resources)

    def resolve(self, resource_id: str) -> PluginResource:
        """
        仅解析有效的持久身份；调用方仍必须另外校验自身授权。

        :param resource_id: 宿主生成的持久 ID
        :return: 当前有效资源
        :raises InstanceReadError: 身份不在当前有效目录中时抛出
        """
        with self._lock:
            resource = next((item for item in self._record.resources if item.resource_id == resource_id), None)
        if resource is None or not resource.is_active:
            raise InstanceReadError("插件资源已经不可用")
        return resource
