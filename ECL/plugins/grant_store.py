# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：持久化用户 v2 授权并管理运行代次的拒绝屏障，不注册 IPC 或执行插件。
#
# 公开接口：
#   - class PluginGrantError — 授权、主体、持久化或内容复核失败。
#   - class PluginGrantStore — 本安装、本内容的原子授权存储。
#   - class PluginGrantSession — 单次 Worker 的授权视图、准入和失效检查。
# ============================================================

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import time
from typing import ClassVar
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.plugins.grant_models import (
    GrantLease,
    GrantView,
    PermissionDecision,
    PermissionManagementCaller,
    PermissionView,
    PluginContentBinding,
)
from ECL.utils import atomic_write_text


class PluginGrantError(ValueError):
    """
    返回稳定授权错误码，不把授权文件、私有路径或请求体放进异常文本。
    """

    def __init__(self, message: str, error_code: str) -> None:
        """
        保存可供业务适配层转换的失败类型。

        :param message: 不含敏感输入的中文规则说明
        :param error_code: 稳定错误类别
        """
        super().__init__(message)
        self.error_code: str = error_code


class _GrantRecord(BaseModel):
    """
    校验磁盘记录，拒绝未知版本和持久化会话许可。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: StrictInt = Field(default=2, ge=2, le=2)
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(default=0, ge=0, le=2**53 - 1)
    reviewed_at: StrictFloat | None = Field(default=None, gt=0, allow_inf_nan=False)
    grants: tuple[PermissionDecision, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def _validate_grants(self) -> _GrantRecord:
        if self.revision > 0 and self.reviewed_at is None:
            raise ValueError("已保存的授权缺少审阅时间")
        if len({item.capability for item in self.grants}) != len(self.grants):
            raise ValueError("磁盘授权能力重复")
        if any(item.lifetime != "permanent" for item in self.grants):
            raise ValueError("会话许可不能保存到授权文件")
        return self

    @field_validator("grants", mode="before")
    @classmethod
    def _grant_sequence(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, list | tuple):
            raise ValueError("授权清单必须是数组")
        return tuple(value)


@dataclass(slots=True)
class _SessionState:
    """
    在授权服务锁内维护本次运行的版本、会话许可和拒绝屏障。
    """

    installation_id: str
    binding: PluginContentBinding
    run_id: str
    revision: int
    permanent_grants: dict[str, PermissionDecision]
    epoch: int = 1
    session_grants: dict[str, PermissionDecision] = field(default_factory=dict)
    expired_capabilities: set[str] = field(default_factory=set)
    blocked_error: str | None = None
    is_closed: bool = False


class PluginGrantSession:
    """
    只访问宿主持有的当前授权；不接受 Worker 自报主体或 epoch 来赋权。

    检查点和提交接受共同维护授权先后顺序，实际任务与 Worker 收尾由运行服务负责。
    """

    def __init__(self, store: PluginGrantStore, state: _SessionState) -> None:
        """
        接管宿主建立的运行身份；业务层应通过 store.open_session 创建。

        :param store: 唯一授权服务
        :param state: 宿主生成且登记的运行状态
        """
        self._store = store
        self._state = state

    def view(self) -> GrantView:
        """
        查询有效许可，复核外部授权版本并使过期授权失效。

        :return: 本次运行的只读视图，损坏或关闭时保持 deny
        """
        return self._store._view(self)

    def selected_resources(self, capability: str) -> tuple[str, ...]:
        """
        向可信管理层返回原始选择，包括失效资源，供用户查看和撤销。

        :param capability: 需要查看的已声明能力
        :return: 当前决定记录中的持久资源 ID，不直接交给 Worker
        """
        self.view()
        with self._store._lock:
            state = self._store._state(self)
            grant = {**state.permanent_grants, **state.session_grants}.get(capability)
            return grant.resource_refs if grant else ()

    @property
    def run_id(self) -> str:
        """
        返回宿主生成的运行标识，不能由 Worker 或管理请求体改写。

        :return: 本授权会话的只读运行身份
        """
        return self._state.run_id

    @property
    def binding(self) -> PluginContentBinding:
        """
        返回不可变的已捕获内容，用于许可加载前再次复核。

        :return: 本运行绑定的代码、权限和依赖身份
        """
        return self._state.binding

    def authorize(self, capability: str, resource_refs: tuple[str, ...] = ()) -> GrantLease:
        """
        为本次业务操作取得宿主准入快照，不预先允许后续提交。

        :param capability: 已声明且已经实现的能力
        :param resource_refs: 此操作实际访问的精确宿主资源
        :return: 当前运行代次和授权版本的准入记录
        :raises PluginGrantError: 能力、资源、必要许可或内容不满足时抛出
        """
        view = self.view()
        with self._store._lock:
            state = self._store._state(self)
            if view.grant_epoch != state.epoch:
                raise PluginGrantError("插件授权准入记录已经失效", "PLUGIN_GRANT_EXPIRED")
            return self._authorize_locked(capability, resource_refs)

    def _authorize_locked(self, capability: str, resource_refs: tuple[str, ...]) -> GrantLease:
        """
        只核验锁保护的内存状态与到期时间，不执行磁盘或内容哈希。
        """
        state = self._store._state(self)
        view = self._store._view_locked(state)
        if view.error_code:
            raise PluginGrantError("本次插件运行不具备授权准入", view.error_code)
        if not view.required_satisfied:
            raise PluginGrantError("插件缺少必要授权", "PLUGIN_REQUIRED_PERMISSION_DENIED")
        permission = next((item for item in view.items if item.capability == capability), None)
        if permission is None or permission.decision != "allow":
            raise PluginGrantError("插件能力未获准", "PLUGIN_PERMISSION_DENIED")
        declared = next(item for item in state.binding.declarations if item.capability == capability)
        if (
            not isinstance(resource_refs, tuple)
            or not all(isinstance(item, str) for item in resource_refs)
            or len(set(resource_refs)) != len(resource_refs)
            or (declared.resource_refs and not resource_refs)
            or (capability == "instances.read" and not resource_refs)
            or not set(resource_refs).issubset(permission.resource_refs)
        ):
            raise PluginGrantError("插件资源范围未获准", "PLUGIN_RESOURCE_DENIED")
        return GrantLease(
            state.installation_id,
            state.binding.content_digest,
            state.run_id,
            view.grant_epoch,
            capability,
            tuple(sorted(resource_refs)),
        )

    def check(self, lease: GrantLease) -> None:
        """
        在锁外复核内容和磁盘，再于短授权锁中拒绝失效租约。

        :param lease: 入队时的可信准入
        :raises PluginGrantError: 内容、期限、范围或代次失效时抛出
        """
        self.view()
        with self._store._lock:
            self._check_locked(lease)

    def check_all(self, leases: tuple[GrantLease, ...]) -> None:
        """
        在一次内容刷新后同时核验同一运行的全部原始准入，不执行部分授权动作。

        :param leases: 受信调用方捕获的非空准入集合
        :raises PluginGrantError: 任一准入失效或来源、代次不同则抛出
        """
        self.view()
        with self._store._lock:
            self._check_all_locked(leases)

    def _check_all_locked(self, leases: tuple[GrantLease, ...]) -> None:
        if not leases or len({lease.grant_epoch for lease in leases}) != 1:
            raise PluginGrantError("插件组合准入已经失效", "PLUGIN_GRANT_EXPIRED")
        for lease in leases:
            self._check_locked(lease)

    def accept_all(self, leases: tuple[GrantLease, ...], register: Callable[[], None]) -> None:
        """
        在同一短授权锁内核验全部原始准入并接受一个纯内存登记。

        锁外刷新内容及磁盘，回调禁止 I/O、文件锁、重新查询权限或插件代码。

        :param leases: 同一运行和代次的全部执行准入
        :param register: 不包含 I/O 的有限登记
        :raises PluginGrantError: 任一准入失效时不执行登记
        """
        self.check_all(leases)
        with self._store._lock:
            self._check_all_locked(leases)
            register()

    def _check_locked(self, lease: GrantLease) -> None:
        state = self._store._state(self)
        if state.blocked_error == "PLUGIN_CONTENT_CHANGED":
            raise PluginGrantError("插件内容与审阅记录不一致", state.blocked_error)
        if (lease.installation_id, lease.content_digest, lease.run_id, lease.grant_epoch) != (
            state.installation_id,
            state.binding.content_digest,
            state.run_id,
            state.epoch,
        ) or self._authorize_locked(lease.capability, lease.resource_refs) != lease:
            raise PluginGrantError("插件授权准入记录已经失效", "PLUGIN_GRANT_EXPIRED")

    def accept(self, lease: GrantLease, register: Callable[[], None]) -> None:
        """
        将权限仍有效与一次确定动作的接受登记置于同一授权临界区。

        register 仅更新受信内存状态；业务动作、文件锁和落盘必须在返回后执行。
        锁序为授权锁再任务或注册锁，禁止回调重新查询授权或执行插件代码。

        :param lease: 本次动作原始准入
        :param register: 不含 I/O 的短时登记
        :raises PluginGrantError: 撤销、关闭或到期先于接受时抛出
        """
        self.check(lease)
        with self._store._lock:
            self._check_locked(lease)
            register()

    def close(self) -> None:
        """
        幂等失效本次运行的会话许可和旧准入记录，不删除永久授权。
        """
        with self._store._lock:
            state = self._store._state(self)
            if not state.is_closed:
                state.is_closed = True
                state.epoch += 1
                state.session_grants.clear()
                state.blocked_error = "PLUGIN_SESSION_EXPIRED"
                self._store._states_by_run.pop(state.run_id, None)


class PluginGrantStore:
    """
    保存独立于旧插件声明的用户授权，默认拒绝且不自动继承安装或内容身份。

    同步磁盘方法须在后台边界调用。线程用同一服务锁保护，跨进程更新用文件锁和
    revision 比较；文件只保留永久决定，会话允许只保留在当前运行内存中。
    """

    max_record_bytes: ClassVar[int] = 2 * 1024 * 1024
    token_pattern: ClassVar[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")

    def __init__(
        self,
        data_path: Path,
        *,
        implemented_capabilities: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time,
    ) -> None:
        """
        建立宿主授权服务，不读取或修改旧 PermissionManager。

        :param data_path: 受信可写数据根目录
        :param implemented_capabilities: 已通过功能验收的能力，默认没有开放能力
        :param clock: 宿主提供的授权到期时钟
        """
        self.data_path: Path = Path(data_path).resolve()
        self.implemented_capabilities: frozenset[str] = frozenset(implemented_capabilities)
        self._clock = clock
        self._lock = RLock()
        self._io_locks: dict[tuple[str, str], RLock] = {}
        self._states_by_run: dict[str, _SessionState] = {}
        self._used_run_ids: set[str] = set()
        self._available_instance_refs: frozenset[str] = frozenset()

    def update_resource_scope(self, available_refs: frozenset[str]) -> None:
        """
        接收宿主在授权锁外验证的资源快照，失效或恢复时使受影响租约失效。

        :param available_refs: 当前物理身份仍有效的宿主资源 ID
        """
        with self._lock:
            previous = self._available_instance_refs
            self._available_instance_refs = available_refs
            for state in self._states_by_run.values():
                grant = {**state.permanent_grants, **state.session_grants}.get("instances.read")
                if grant and (set(grant.resource_refs) & previous) != (set(grant.resource_refs) & available_refs):
                    state.epoch += 1

    @staticmethod
    def new_installation_id() -> str:
        """
        为一次新安装生成身份，卸载重装不得按插件名称复用旧值。

        :return: 宿主生成的随机安装身份
        """
        return uuid4().hex

    def open_session(
        self, installation_id: str, binding: PluginContentBinding, run_id: str | None = None
    ) -> PluginGrantSession:
        """
        复核已审阅内容并建立新运行代次，不承诺已经满足必要权限。

        :param installation_id: 安装服务生成的身份
        :param binding: 宿主已捕获的内容和声明
        :param run_id: 已创建 Worker 的宿主运行标识；None 生成新值
        :return: 尚须检查 required_satisfied 的授权会话
        :raises PluginGrantError: 身份重复或内容已改变时抛出
        """
        run_id = run_id or uuid4().hex
        self._validate_token(installation_id)
        self._validate_token(run_id)
        state = _SessionState(installation_id, binding, run_id, 0, {})
        with self._record_lock(state):
            self._verify_content(state)
            try:
                record = self._read(state)
                state.revision = record.revision
                state.permanent_grants = {item.capability: item for item in record.grants}
            except PluginGrantError as exc:
                state.blocked_error = exc.error_code
            with self._lock:
                if run_id in self._used_run_ids:
                    raise PluginGrantError("插件运行标识已使用", "PLUGIN_SESSION_EXPIRED")
                self._used_run_ids.add(run_id)
                self._states_by_run[run_id] = state
        return PluginGrantSession(self, state)

    def update(
        self,
        session: PluginGrantSession,
        decisions: tuple[PermissionDecision, ...],
        *,
        expected_revision: int,
        content_digest: str,
        caller: PermissionManagementCaller,
    ) -> GrantView:
        """
        在拒绝屏障建立后原子提交用户决定，失败时不恢复旧运行的允许状态。

        会话允许移除对应永久允许，拒绝始终持久化。任何成功增权或减权都使旧
        lease 失效；这里不把屏障建立描述为业务线程或远端请求已经停止。

        :param session: 管理入口从安装登记定位的运行会话
        :param decisions: 用户明确的决定，不从插件声明推断 allow
        :param expected_revision: 主界面审阅时的持久化版本
        :param content_digest: 主界面审阅的精确内容身份
        :param caller: 来自受信原生入口的分类，不来自 body
        :return: 保存成功后的有效视图
        :raises PluginGrantError: 主体、版本、内容、范围或写入失败时抛出
        """
        if caller is not PermissionManagementCaller.main_webview:
            raise PluginGrantError("此入口不能管理插件授权", "PLUGIN_MANAGEMENT_CALLER_DENIED")
        state = self._state(session)
        if content_digest != state.binding.content_digest:
            raise PluginGrantError("授权审阅内容不匹配", "PLUGIN_GRANT_CONTENT_MISMATCH")
        with self._record_lock(state):
            with self._lock:
                if state.is_closed:
                    raise PluginGrantError("插件运行已经结束", "PLUGIN_SESSION_EXPIRED")
                self._validate_decisions(state, decisions)
            self._verify_content(state)
            record_path = self._record_path(state)
            try:
                with _exclusive_lock(record_path.with_suffix(".lock"), wait_seconds=2):
                    previous = self._read(state)
                    with self._lock:
                        if state.is_closed:
                            raise PluginGrantError("插件运行已经结束", "PLUGIN_SESSION_EXPIRED")
                        self._validate_decisions(state, decisions)
                        if (
                            type(expected_revision) is not int
                            or previous.revision != expected_revision
                            or previous.revision < state.revision
                        ):
                            raise PluginGrantError("授权记录版本已变化", "PLUGIN_GRANT_REVISION_CONFLICT")
                    self._commit_decisions(record_path, state, previous, decisions)
            except PluginGrantError:
                raise
            except (OSError, PluginDependencyError, ValidationError) as exc:
                with self._lock:
                    self._block(state, "PLUGIN_GRANT_PERSIST_FAILED")
                raise PluginGrantError("插件授权保存失败", "PLUGIN_GRANT_PERSIST_FAILED") from exc
        return self._view(session)

    def _commit_decisions(
        self,
        record_path: Path,
        state: _SessionState,
        previous: _GrantRecord,
        decisions: tuple[PermissionDecision, ...],
    ) -> None:
        """
        发布内存拒绝屏障、锁外保存、再发布结果；调用方只持有每内容 I/O 锁。
        """
        with self._lock:
            if state.is_closed:
                raise PluginGrantError("插件运行已经结束", "PLUGIN_SESSION_EXPIRED")
            self._validate_decisions(state, decisions)
            grants = {item.capability: item for item in previous.grants}
            for item in decisions:
                if item.decision == "deny" or item.lifetime == "permanent":
                    grants[item.capability] = item.model_copy(update={"lifetime": "permanent"})
                else:
                    grants.pop(item.capability, None)
            candidate = _GrantRecord(
                installation_id=state.installation_id,
                content_digest=state.binding.content_digest,
                revision=previous.revision + 1,
                reviewed_at=self._clock(),
                grants=tuple(sorted(grants.values(), key=lambda item: item.capability)),
            )
            affected = self._matching_states(state)
            for active in affected:
                active.epoch += 1
                active.blocked_error = "PLUGIN_GRANT_PERSIST_FAILED"
        atomic_write_text(record_path, candidate.model_dump_json())
        with self._lock:
            for active in affected:
                if active.is_closed:
                    continue
                active.revision = candidate.revision
                active.permanent_grants = dict(grants)
                active.expired_capabilities.clear()
                active.blocked_error = None
                for item in decisions:
                    active.session_grants.pop(item.capability, None)
            if not state.is_closed:
                for item in decisions:
                    if item.decision == "allow" and item.lifetime == "session":
                        state.session_grants[item.capability] = item

    def _record_lock(self, state: _SessionState) -> RLock:
        """
        串行同一安装内容的磁盘准备和保存，等待时不持有全局授权锁。
        """
        with self._lock:
            return self._io_locks.setdefault((state.installation_id, state.binding.content_digest), RLock())

    def _state(self, session: PluginGrantSession) -> _SessionState:
        """
        确认会话仍由本授权服务持有，关闭会话仅可查询拒绝状态。
        """
        if session._store is not self or (
            not session._state.is_closed and self._states_by_run.get(session._state.run_id) is not session._state
        ):
            raise PluginGrantError("插件授权会话不属于当前宿主", "PLUGIN_SESSION_EXPIRED")
        return session._state

    def _matching_states(self, state: _SessionState) -> tuple[_SessionState, ...]:
        return tuple(
            item
            for item in self._states_by_run.values()
            if item.installation_id == state.installation_id
            and item.binding.content_digest == state.binding.content_digest
        )

    def _block(self, state: _SessionState, code: str) -> None:
        """
        在同一授权锁内失效本内容的所有运行，屏障不因旧磁盘记录仍存在而消失。
        """
        for active in self._matching_states(state) or (state,):
            active.epoch += 1
            active.session_grants.clear()
            active.blocked_error = code

    def _verify_content(self, state: _SessionState) -> None:
        """
        拒绝审阅后变化的字节，不能以新摘要自动更新许可。
        """
        try:
            state.binding.verify()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._block(state, "PLUGIN_CONTENT_CHANGED")
            raise PluginGrantError("插件内容与审阅记录不一致", "PLUGIN_CONTENT_CHANGED") from exc

    @classmethod
    def _validate_token(cls, token: str) -> None:
        """
        标识只接受宿主随机十六进制值，避免路径或显示名称成为身份。
        """
        if not isinstance(token, str) or cls.token_pattern.fullmatch(token) is None:
            raise PluginGrantError("插件安装或运行标识无效", "PLUGIN_SESSION_EXPIRED")

    def _record_path(self, state: _SessionState) -> Path:
        """
        路径完全由宿主安装标识和内容摘要决定，拒绝链接及越界祖先。
        """
        target_path = self.data_path / "plugin_grants" / state.installation_id / f"{state.binding.content_digest}.json"
        for entry in (target_path.parent.parent, target_path.parent, target_path):
            if entry.is_symlink() or not entry.resolve().is_relative_to(self.data_path):
                raise PluginGrantError("授权存储路径无效", "PLUGIN_GRANT_STORE_CORRUPTED")
        if os.name == "nt":
            # 完整内容摘要加原子临时文件名会超过 MAX_PATH，保留完整身份而不截短摘要。
            path_text = str(target_path)
            if not path_text.startswith("\\\\?\\"):
                path_text = "\\\\?\\UNC\\" + path_text[2:] if path_text.startswith("\\\\") else "\\\\?\\" + path_text
            return Path(path_text)
        return target_path

    @staticmethod
    def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("授权文件包含重复字段")
            result[key] = value
        return result

    def _read(self, state: _SessionState) -> _GrantRecord:
        """
        读取有界的独立 v2 记录；损坏、错主体和旧声明文件均不得转换成 allow。
        """
        record_path = self._record_path(state)
        if not record_path.exists():
            return _GrantRecord(installation_id=state.installation_id, content_digest=state.binding.content_digest)
        try:
            if record_path.stat().st_size > self.max_record_bytes:
                raise ValueError("授权文件超过上限")
            payload: object = json.loads(record_path.read_bytes(), object_pairs_hook=self._unique_pairs)
            record = _GrantRecord.model_validate(payload)
            if record.installation_id != state.installation_id or record.content_digest != state.binding.content_digest:
                raise ValueError("授权文件主体不匹配")
            self._validate_decisions(state, record.grants, is_disk=True)
            return record
        except (OSError, ValueError, RecursionError) as exc:
            raise PluginGrantError("插件授权文件损坏或无法读取", "PLUGIN_GRANT_STORE_CORRUPTED") from exc

    def _validate_decisions(
        self, state: _SessionState, decisions: tuple[PermissionDecision, ...], *, is_disk: bool = False
    ) -> None:
        """
        校验每条决定只涉及已审阅声明和精确资源，不支持通配符或隐式新增能力。
        """
        if (
            not isinstance(decisions, tuple)
            or len(decisions) > 128
            or any(not isinstance(item, PermissionDecision) for item in decisions)
            or len({item.capability for item in decisions}) != len(decisions)
        ):
            raise PluginGrantError("授权决定格式或数量无效", "PLUGIN_GRANT_INVALID")
        declared_by_capability = {item.capability: item for item in state.binding.declarations}
        for item in decisions:
            declared = declared_by_capability.get(item.capability)
            if declared is None:
                raise PluginGrantError("授权决定超出已审阅声明", "PLUGIN_GRANT_INVALID")
            if (
                item.capability in {"filesystem.read", "filesystem.write", "filesystem.delete", "filesystem.replace"}
                and item.resource_refs
            ):
                raise PluginGrantError("文件许可只允许安装私有空间", "PLUGIN_GRANT_INVALID")
            if item.capability == "instances.read":
                if any(re.fullmatch(r"[0-9a-f]{32}", ref) is None for ref in item.resource_refs):
                    raise PluginGrantError("实例资源身份无效", "PLUGIN_GRANT_INVALID")
                if (
                    not is_disk
                    and item.decision == "allow"
                    and not set(item.resource_refs).issubset(self._available_instance_refs)
                ):
                    raise PluginGrantError("实例资源已不可用，请刷新后重试", "PLUGIN_RESOURCE_UNAVAILABLE")
            elif not set(item.resource_refs).issubset(declared.resource_refs):
                raise PluginGrantError("授权决定超出已审阅声明", "PLUGIN_GRANT_INVALID")
            if (
                not is_disk
                and item.decision == "allow"
                and item.expires_at is not None
                and item.expires_at <= self._clock()
            ):
                raise PluginGrantError("不能授予已经到期的许可", "PLUGIN_GRANT_INVALID")

    def _view(self, session: PluginGrantSession) -> GrantView:
        """
        在每内容 I/O 锁内复核磁盘和代码，再短时发布版本和到期状态。
        """
        state = self._state(session)
        with self._record_lock(state):
            with self._lock:
                should_refresh = not state.is_closed and state.blocked_error is None
            if should_refresh:
                try:
                    self._verify_content(state)
                    record = self._read(state)
                    with self._lock:
                        if not state.is_closed and state.blocked_error is None:
                            if record.revision < state.revision or (
                                record.revision == state.revision
                                and {item.capability: item for item in record.grants} != state.permanent_grants
                            ):
                                raise PluginGrantError("授权记录版本或内容发生回退", "PLUGIN_GRANT_STORE_CORRUPTED")
                            if record.revision != state.revision:
                                for active in self._matching_states(state):
                                    active.epoch += 1
                                    active.revision = record.revision
                                    active.permanent_grants = {item.capability: item for item in record.grants}
                                    active.session_grants.clear()
                                    active.expired_capabilities.clear()
                except PluginGrantError as exc:
                    with self._lock:
                        self._block(state, exc.error_code)
            with self._lock:
                return self._view_locked(state)

    def _view_locked(self, state: _SessionState) -> GrantView:
        """
        只从内存生成当前许可，同时发布到期屏障；调用方持有授权锁。
        """
        grants = {**state.permanent_grants, **state.session_grants}
        expired = {
            item.capability
            for item in grants.values()
            if item.decision == "allow" and item.expires_at is not None and item.expires_at <= self._clock()
        }
        if expired - state.expired_capabilities:
            state.epoch += 1
            state.expired_capabilities.update(expired)
        items: list[PermissionView] = []
        required_satisfied = state.blocked_error is None
        for declared in state.binding.declarations:
            grant = grants.get(declared.capability)
            is_allowed = (
                state.blocked_error is None
                and grant is not None
                and grant.decision == "allow"
                and declared.capability in self.implemented_capabilities
                and declared.capability not in state.expired_capabilities
            )
            resources = grant.resource_refs if is_allowed and grant else ()
            if declared.capability == "instances.read":
                resources = tuple(ref for ref in resources if ref in self._available_instance_refs)
                is_allowed = is_allowed and bool(resources)
                is_required_missing = not is_allowed
            else:
                is_required_missing = not is_allowed or not set(declared.resource_refs).issubset(resources)
            if declared.required and is_required_missing:
                required_satisfied = False
            items.append(
                PermissionView(
                    declared.capability,
                    declared.required,
                    "allow" if is_allowed else "deny",
                    resources,
                    grant.expires_at if grant else None,
                    state.epoch,
                )
            )
        return GrantView(
            state.installation_id,
            state.binding.content_digest,
            state.revision,
            state.epoch,
            tuple(items),
            required_satisfied,
            state.blocked_error,
        )
