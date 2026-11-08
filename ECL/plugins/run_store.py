# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：管理插件运行会话、内容复核与代次失效，不包含权限概念。
#
# 公开接口：
#   - class PluginRunError — 运行会话、内容复核或代次失效错误。
#   - class PluginRunStore — 本安装的运行会话原子管理。
#   - class PluginRunSession — 单次 Worker 的时效准入与失效检查。
# ============================================================

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic, time
from typing import ClassVar
from uuid import uuid4

from ECL.plugins.run_models import PluginContentBinding, PluginLease, PluginRunView


class PluginRunError(ValueError):
    """
    返回稳定运行错误码，不把私有路径或请求体放进异常文本。
    """

    def __init__(self, message: str, error_code: str) -> None:
        """
        保存可供业务适配层转换的失败类型。

        :param message: 不含敏感输入的中文规则说明
        :param error_code: 稳定错误类别
        """
        super().__init__(message)
        self.error_code: str = error_code


@dataclass(slots=True)
class _SessionState:
    """
    在运行服务锁内维护本次运行的代次与内容复核窗口。
    """

    installation_id: str
    binding: PluginContentBinding
    run_id: str
    epoch: int = 1
    blocked_error: str | None = None
    is_closed: bool = False
    content_verified_at: float = 0.0


class PluginRunSession:
    """
    只访问宿主持有的当前运行；不接受 Worker 自报主体或代次。

    检查点和提交接受共同维护先后顺序，实际任务与 Worker 收尾由运行服务负责。
    """

    def __init__(self, store: PluginRunStore, state: _SessionState) -> None:
        """
        接管宿主建立的运行身份；业务层应通过 store.open_session 创建。

        :param store: 唯一运行服务
        :param state: 宿主生成且登记的运行状态
        """
        self._store = store
        self._state = state

    def view(self) -> PluginRunView:
        """
        查询当前运行身份，复核外部代次与内容状态。

        :return: 本次运行的只读视图，损坏或关闭时携带失效码
        """
        return self._store._view(self)

    @property
    def run_id(self) -> str:
        """
        返回宿主生成的运行标识，不能由 Worker 或管理请求体改写。

        :return: 本运行会话的只读运行身份
        """
        return self._state.run_id

    @property
    def binding(self) -> PluginContentBinding:
        """
        返回不可变的已捕获内容，用于加载前再次复核。

        :return: 本运行绑定的代码和依赖身份
        """
        return self._state.binding

    def lease(self) -> PluginLease:
        """
        为本次业务操作取得宿主时效准入快照，不预先允许后续提交。

        :return: 当前运行代次的准入记录
        :raises PluginRunError: 运行已失效或内容不满足时抛出
        """
        view = self.view()
        with self._store._lock:
            state = self._store._state(self)
            if view.run_epoch != state.epoch:
                raise PluginRunError("插件运行准入记录已经失效", "PLUGIN_RUN_EXPIRED")
            return self._lease_locked()

    def _lease_locked(self) -> PluginLease:
        """
        只核验锁保护的内存状态，不执行磁盘或内容哈希。
        """
        state = self._store._state(self)
        view = self._store._view_locked(state)
        if view.error_code:
            raise PluginRunError("本次插件运行不具备准入", view.error_code)
        return PluginLease(state.installation_id, state.binding.content_digest, state.run_id, view.run_epoch)

    def check(self, lease: PluginLease) -> None:
        """
        在锁外强制复核内容和磁盘，再于短运行锁中拒绝失效准入。

        副作用提交前必须看到最新字节，因此不复用内容复核窗口。

        :param lease: 入队时的可信准入
        :raises PluginRunError: 内容或代次失效时抛出
        """
        self._store._view(self, force_recheck=True)
        with self._store._lock:
            self._check_locked(lease)

    def check_all(self, leases: tuple[PluginLease, ...]) -> None:
        """
        在一次内容刷新后同时核验同一运行的全部准入，不执行部分动作。

        :param leases: 受信调用方捕获的非空准入集合
        :raises PluginRunError: 任一准入失效或来源、代次不同则抛出
        """
        self._store._view(self, force_recheck=True)
        with self._store._lock:
            self._check_all_locked(leases)

    def _check_all_locked(self, leases: tuple[PluginLease, ...]) -> None:
        if not leases or len({lease.run_epoch for lease in leases}) != 1:
            raise PluginRunError("插件组合准入已经失效", "PLUGIN_RUN_EXPIRED")
        for lease in leases:
            self._check_locked(lease)

    def accept_all(self, leases: tuple[PluginLease, ...], register: Callable[[], None]) -> None:
        """
        在同一短运行锁内核验全部准入并接受一个纯内存登记。

        锁外刷新内容及磁盘，回调禁止 I/O、文件锁或重新查询运行状态。

        :param leases: 同一运行和代次的全部执行准入
        :param register: 不包含 I/O 的有限登记
        :raises PluginRunError: 任一准入失效时不执行登记
        """
        self.check_all(leases)
        with self._store._lock:
            self._check_all_locked(leases)
            register()

    def _check_locked(self, lease: PluginLease) -> None:
        state = self._store._state(self)
        if state.blocked_error == "PLUGIN_CONTENT_CHANGED":
            raise PluginRunError("插件内容与运行记录不一致", state.blocked_error)
        if (lease.installation_id, lease.content_digest, lease.run_id, lease.run_epoch) != (
            state.installation_id,
            state.binding.content_digest,
            state.run_id,
            state.epoch,
        ):
            raise PluginRunError("插件运行准入记录已经失效", "PLUGIN_RUN_EXPIRED")

    def accept(self, lease: PluginLease, register: Callable[[], None]) -> None:
        """
        将运行仍有效与一次确定动作的接受登记置于同一临界区。

        register 仅更新受信内存状态；业务动作、文件锁和落盘必须在返回后执行。

        :param lease: 本次动作原始准入
        :param register: 不含 I/O 的短时登记
        :raises PluginRunError: 关闭或失效先于接受时抛出
        """
        self.check(lease)
        with self._store._lock:
            self._check_locked(lease)
            register()

    def close(self) -> None:
        """
        幂等失效本次运行的旧准入记录。
        """
        with self._store._lock:
            state = self._store._state(self)
            if not state.is_closed:
                state.is_closed = True
                state.epoch += 1
                state.blocked_error = "PLUGIN_SESSION_EXPIRED"
                self._store._states_by_run.pop(state.run_id, None)


class PluginRunStore:
    """
    管理插件运行会话与内容身份复核，默认信任启用的插件内容。

    线程用同一服务锁保护；内容字节复核按窗口复用，会话关闭与内容变化立即失效。
    """

    token_pattern: ClassVar[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
    # 一次宿主 API 调用会多次取视图，内容字节复核按此窗口复用；
    # 副作用提交前的 check 不复用窗口，内容变化仍然立即生效。
    content_recheck_interval_seconds: ClassVar[float] = 0.5

    def __init__(self, *, clock: Callable[[], float] = time) -> None:
        """
        建立宿主运行服务，不读取或保留任何授权记录。

        :param clock: 宿主提供的单调复核时钟
        """
        self._clock = clock
        self._lock = RLock()
        self._states_by_run: dict[str, _SessionState] = {}
        self._used_run_ids: set[str] = set()

    @staticmethod
    def new_installation_id() -> str:
        """
        为一次新安装生成身份，卸载重装不得按插件名称复用旧值。

        :return: 宿主生成的随机安装身份
        """
        return uuid4().hex

    def open_session(
        self,
        installation_id: str,
        binding: PluginContentBinding,
        run_id: str | None = None,
    ) -> PluginRunSession:
        """
        复核内容并建立新运行代次。

        :param installation_id: 安装服务生成的身份
        :param binding: 宿主已捕获的内容身份
        :param run_id: 已创建 Worker 的宿主运行标识；None 生成新值
        :return: 就绪的运行会话
        :raises PluginRunError: 身份重复或内容已改变时抛出
        """
        run_id = run_id or uuid4().hex
        self._validate_token(installation_id)
        self._validate_token(run_id)
        state = _SessionState(installation_id, binding, run_id)
        self._verify_content(state)
        with self._lock:
            if run_id in self._used_run_ids:
                raise PluginRunError("插件运行标识已使用", "PLUGIN_SESSION_EXPIRED")
            self._used_run_ids.add(run_id)
            self._states_by_run[run_id] = state
        return PluginRunSession(self, state)

    def _state(self, session: PluginRunSession) -> _SessionState:
        """
        确认会话仍由本运行服务持有，关闭会话仅可查询拒绝状态。
        """
        if session._store is not self or (
            not session._state.is_closed and self._states_by_run.get(session._state.run_id) is not session._state
        ):
            raise PluginRunError("插件运行会话不属于当前宿主", "PLUGIN_SESSION_EXPIRED")
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
        在同一运行锁内失效本内容的所有运行。
        """
        for active in self._matching_states(state) or (state,):
            active.epoch += 1
            active.blocked_error = code

    def _verify_content(self, state: _SessionState) -> None:
        """
        拒绝启用后变化的字节，不以新摘要自动更新身份。
        """
        try:
            state.binding.verify()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._block(state, "PLUGIN_CONTENT_CHANGED")
            raise PluginRunError("插件内容与运行记录不一致", "PLUGIN_CONTENT_CHANGED") from exc

    @classmethod
    def _validate_token(cls, token: str) -> None:
        """
        标识只接受宿主随机十六进制值，避免路径或显示名称成为身份。
        """
        if not isinstance(token, str) or cls.token_pattern.fullmatch(token) is None:
            raise PluginRunError("插件安装或运行标识无效", "PLUGIN_SESSION_EXPIRED")

    def _view(self, session: PluginRunSession, *, force_recheck: bool = False) -> PluginRunView:
        """
        复核内容字节后发布当前代次，失效状态保持可见。

        内容字节复核按 `content_recheck_interval_seconds` 复用：一次宿主 API 调用
        会多次取视图，重复哈希整棵代码树会让插件延迟随体积线性增长。

        提交前的 `check` 传 `force_recheck`，保证副作用落地前一定看到最新字节。

        :param session: 当前运行会话
        :param force_recheck: 忽略复用窗口，强制重新校验内容字节
        :return: 当前运行视图
        """
        state = self._state(session)
        with self._lock:
            is_open = not state.is_closed and state.blocked_error is None
            should_verify_content = is_open and (
                force_recheck or monotonic() - state.content_verified_at >= self.content_recheck_interval_seconds
            )
        if should_verify_content:
            try:
                self._verify_content(state)
            except PluginRunError as exc:
                with self._lock:
                    self._block(state, exc.error_code)
            else:
                with self._lock:
                    state.content_verified_at = monotonic()
        with self._lock:
            return self._view_locked(state)

    def _view_locked(self, state: _SessionState) -> PluginRunView:
        """
        只从内存生成当前运行视图；调用方持有运行锁。
        """
        return PluginRunView(
            state.installation_id,
            state.binding.content_digest,
            state.epoch,
            state.blocked_error,
        )
