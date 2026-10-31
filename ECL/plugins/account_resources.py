# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为最小账户摘要维护随机授权身份，不复制账户凭据或业务状态。
#
# 公开接口：
#   - class PluginAccountResourceCatalog — 可信审阅发现、退休及核验账户身份。
# ============================================================

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.utils import atomic_write_text
from ECL.utils.account_reading import AccountSummary


class _Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    resource_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_active: StrictBool


class _Index(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    revision: StrictInt = Field(default=0, ge=0, le=2**53 - 1)
    resources: tuple[_Identity, ...] = Field(default=(), max_length=4096)


class PluginAccountResourceCatalog:
    """
    只有可信主窗口可发现新身份；普通刷新仅核验原选择并退休失效账户。
    """

    def __init__(self, data_path: Path, snapshot: Callable[[], tuple[AccountSummary, ...]]) -> None:
        """
        绑定账户领域只读快照与宿主授权身份存储。

        :param data_path: 宿主数据目录
        :param snapshot: 不执行认证或返回凭据的领域快照
        """
        self._file = data_path / "plugin_account_resources.json"
        self._snapshot = snapshot
        self._lock = RLock()
        self._index = _Index()
        self._loaded = False

    @staticmethod
    def _fingerprint(account: AccountSummary) -> str:
        return hashlib.sha256(
            json.dumps([account.account_id, account.alias, account.uuid, account.kind], ensure_ascii=False).encode()
        ).hexdigest()

    def _read_index(self) -> _Index:
        """
        核验持久身份的唯一性、版本单调性和不可重新绑定约束。
        """
        if self._file.is_symlink():
            raise ValueError("linked index")
        if self._file.exists():
            if self._file.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("index limit")
            previous = _Index.model_validate_json(self._file.read_bytes())
        else:
            previous = _Index()
        if len({item.resource_id for item in previous.resources}) != len(previous.resources):
            raise ValueError("duplicate identity")
        if self._loaded:
            if previous.revision < self._index.revision or (
                previous.revision == self._index.revision and previous != self._index
            ):
                raise ValueError("index rollback")
            current = {item.resource_id: item for item in previous.resources}
            for item in self._index.resources:
                replacement = current.get(item.resource_id)
                if (
                    replacement is None
                    or replacement.fingerprint != item.fingerprint
                    or (not item.is_active and replacement.is_active)
                ):
                    raise ValueError("identity rebound")
        return previous

    def refresh(self, *, discover: bool = False) -> int:
        """
        原子维护已审阅身份，损坏和版本回退保留原文件并明确失败。

        :param discover: 仅可信审阅允许登记新的候选
        :return: 实际身份目录版本
        :raises ProtocolError: 快照、索引或版本不满足时抛出
        """
        accounts = self._snapshot()
        if len(accounts) > 4096 or not all(isinstance(item, AccountSummary) for item in accounts):
            raise ProtocolError("账户摘要格式无效", "PLUGIN_ACCOUNT_QUERY_FAILED")
        fingerprints = {self._fingerprint(account) for account in accounts}
        with self._lock, _exclusive_lock(self._file.with_suffix(".lock")):
            try:
                previous = self._read_index()
                resources = [
                    item if item.fingerprint in fingerprints else item.model_copy(update={"is_active": False})
                    for item in previous.resources
                ]
                if discover:
                    known = {item.fingerprint for item in resources if item.is_active}
                    resources.extend(
                        _Identity(resource_id=uuid4().hex, fingerprint=value, is_active=True)
                        for value in sorted(fingerprints - known)
                    )
                record = _Index(revision=previous.revision, resources=tuple(resources))
                if record.resources != previous.resources:
                    record = record.model_copy(update={"revision": previous.revision + 1})
                    atomic_write_text(self._file, record.model_dump_json())
                self._index = record
                self._loaded = True
                return record.revision
            except (OSError, ValueError) as error:
                raise ProtocolError("账户授权目录无法读取", "PLUGIN_RESOURCE_CATALOG_INVALID") from error

    def active_refs(self) -> frozenset[str]:
        """
        返回当前有效身份，不暴露业务账户标识。

        :return: 当前有效的宿主持久身份
        """
        with self._lock:
            return frozenset(item.resource_id for item in self._index.resources if item.is_active)

    def candidates(self) -> tuple[tuple[str, AccountSummary | None], ...]:
        """
        为可信主窗口提供当前或失效候选，失效对象不自动重新授予。

        :return: 宿主身份与当前最小摘要的对应快照
        """
        accounts = {self._fingerprint(item): item for item in self._snapshot()}
        with self._lock:
            return tuple(
                (item.resource_id, accounts.get(item.fingerprint) if item.is_active else None)
                for item in self._index.resources
            )

    def resolve(self, reference: str) -> AccountSummary:
        """
        重新读取权威账户快照并核验当前资源身份。

        :param reference: 宿主授权身份
        :return: 当前账户最小摘要
        :raises ProtocolError: 账户失效或已发生变化时抛出
        """
        for resource_id, account in self.candidates():
            if resource_id == reference and account is not None:
                return account
        raise ProtocolError("账户身份已经失效", "PLUGIN_RESOURCE_UNAVAILABLE")
