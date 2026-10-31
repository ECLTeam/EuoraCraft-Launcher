# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：维护已登记 Java 的授权对象身份，不保存第二份运行时业务状态。
#
# 公开接口：
#   - class JavaResource — 宿主持有的随机持久授权身份。
#   - class PluginJavaResourceCatalog — 发现、验证和退休 Java 授权候选。
# ============================================================
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ECL.java.models import JavaError, RuntimeRecord
from ECL.java.queries import JavaQueries
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.utils import atomic_write_text
from ECL.utils.instance_reading import directory_identity


class JavaResource(BaseModel):
    """
    保存随机身份与已审阅对象指纹，不复制版本或启用状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    resource_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    runtime_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_active: bool


class _JavaResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    revision: StrictInt = Field(default=0, ge=0, le=2**53 - 1)
    resources: tuple[JavaResource, ...] = Field(default=(), max_length=4096)


class PluginJavaResourceCatalog:
    """
    仅可信审阅发现新身份，普通刷新只退休已观察到的失效对象。
    """

    def __init__(self, data_path: Path, queries: JavaQueries) -> None:
        """
        绑定已有领域查询，不扫描系统或主动执行 Java。

        :param data_path: 宿主数据目录
        :param queries: 应用唯一 Java 查询服务
        """
        self._file = data_path / "plugin_java_resources.json"
        self._queries = queries
        self._lock = RLock()
        self._record = _JavaResources()
        self._is_loaded = False

    @staticmethod
    def fingerprint(record: RuntimeRecord) -> str:
        """
        校验规范目录链及实际探测文件，拒绝链接和重解析点。

        :param record: 唯一登记中的目标
        :return: 对象身份、路径及关键元数据的摘要
        :raises JavaError: 目标缺失、身份不可确认或被替换时抛出
        """
        try:
            home = record.java_home_path
            executable = record.executable_path
            if not home.is_absolute() or executable.parent != home / "bin":
                raise ValueError("home boundary")
            if executable.name.lower() not in {"java", "java.exe", "javaw.exe"}:
                raise ValueError("executable name")
            directories = [*reversed(home.parents), home, home / "bin"]
            identities = [directory_identity(path).model_dump() for path in directories]
            probe = executable.with_name("java.exe") if executable.name.lower() == "javaw.exe" else executable
            files = []
            for path in dict.fromkeys((executable, probe, home / "release")):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if path == home / "release":
                        files.append(None)
                        continue
                    raise
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("file type")
                if path.is_symlink() or not info.st_ino:
                    raise ValueError("file identity")
                files.append((info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size))
            payload = [str(home), str(executable), record.registered_at, identities, files]
            return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        except (OSError, ValueError) as exc:
            raise JavaError("Java 资源身份无法确认", "PLUGIN_RESOURCE_UNAVAILABLE") from exc

    def _read(self) -> _JavaResources:
        """
        读取有界身份索引，拒绝回退或改绑，保留损坏文件。
        """
        try:
            if not self._file.exists() and not self._file.is_symlink():
                if self._is_loaded and self._record.revision:
                    raise ValueError("missing index")
                return _JavaResources()
            if self._file.is_symlink() or self._file.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("index limit")
            record = _JavaResources.model_validate_json(self._file.read_bytes())
            if len({item.resource_id for item in record.resources}) != len(record.resources):
                raise ValueError("duplicate identity")
            if self._is_loaded:
                if record.revision < self._record.revision or (
                    record.revision == self._record.revision and record != self._record
                ):
                    raise ValueError("revision rollback")
                by_id = {item.resource_id: item for item in record.resources}
                for previous in self._record.resources:
                    current = by_id.get(previous.resource_id)
                    if (
                        current is None
                        or (previous.runtime_id, previous.fingerprint) != (current.runtime_id, current.fingerprint)
                        or (not previous.is_active and current.is_active)
                    ):
                        raise ValueError("identity changed")
            return record
        except (OSError, ValueError) as exc:
            raise JavaError("Java 授权资源索引无法读取", "PLUGIN_RESOURCE_CATALOG_INVALID") from exc

    def refresh(self, *, discover: bool = False) -> int:
        """
        校验登记对象并退休失效项，只有可信审阅可以登记新对象。

        :param discover: 是否接受可信界面发现新身份
        :return: Java 资源目录版本
        :raises JavaError: 索引损坏或并发目录修改时抛出
        """
        records = self._queries.records()
        fingerprints: dict[str, str] = {}
        for item in records:
            try:
                fingerprints[item.runtime_id] = self.fingerprint(item)
            except JavaError:
                # 不可验证的登记不是可授权资源；已有选择保留但标记不可用。
                continue
        with self._lock, _exclusive_lock(self._file.with_suffix(".lock")):
            previous = self._read()
            resources = [
                item
                if fingerprints.get(item.runtime_id) == item.fingerprint
                else item.model_copy(update={"is_active": False})
                for item in previous.resources
            ]
            if discover:
                active = {(item.runtime_id, item.fingerprint) for item in resources if item.is_active}
                resources.extend(
                    JavaResource(
                        resource_id=uuid4().hex, runtime_id=runtime_id, fingerprint=fingerprint, is_active=True
                    )
                    for runtime_id, fingerprint in fingerprints.items()
                    if (runtime_id, fingerprint) not in active
                )
            record = _JavaResources(revision=previous.revision, resources=tuple(resources))
            if record.resources != previous.resources:
                record = record.model_copy(update={"revision": previous.revision + 1})
                content = record.model_dump_json()
                if len(content.encode()) > 8 * 1024 * 1024:
                    raise JavaError("Java 授权资源索引超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
                atomic_write_text(self._file, content)
            self._record = record
            self._is_loaded = True
            return record.revision

    def resources(self) -> tuple[JavaResource, ...]:
        """
        返回身份快照，不携带业务状态。

        :return: 包含退休项的不可变身份列表
        """
        with self._lock:
            return self._record.resources

    def active_refs(self) -> frozenset[str]:
        """
        返回可供授权使用的当前身份集合。

        :return: 有效 Java 持久资源 ID
        """
        return frozenset(item.resource_id for item in self.resources() if item.is_active)

    def resolve(self, resource_id: str) -> RuntimeRecord:
        """
        从唯一登记解析目标，交付前复核对象指纹。

        :param resource_id: 宿主授权持久身份
        :return: 当前真实登记记录
        :raises JavaError: 身份退休、移除或对象变化时抛出
        """
        resource = next((item for item in self.resources() if item.resource_id == resource_id and item.is_active), None)
        if resource is not None:
            record = next((item for item in self._queries.records() if item.runtime_id == resource.runtime_id), None)
            if record is not None and self.fingerprint(record) == resource.fingerprint:
                return record
        raise JavaError("Java 资源已不可用", "PLUGIN_RESOURCE_UNAVAILABLE")
