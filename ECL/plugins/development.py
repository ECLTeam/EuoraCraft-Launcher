# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在本次开发会话中检测目录插件变化，准备稳定副本并串行切换 Worker。
#
# 公开接口：
#   - class DevelopmentSource — 宿主确认的目录安装与运行摘要。
#   - class PluginDevelopmentService — 应用所属的监听、开发信任和代码副本生命周期。
# ============================================================

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from ECL.plugins.api._errors import PluginRuntimeError
from ECL.plugins.api.contracts import JsonValue, ProtocolError
from ECL.plugins.api.snapshot import CodeSnapshot
from ECL.plugins.installation_store import WorkerManifest
from ECL.utils import get_logger, remove_tree

if TYPE_CHECKING:
    from ECL.plugins.worker_service import PluginWorkerService


@dataclass(frozen=True, slots=True)
class DevelopmentSource:
    """
    保存实际安装目录和执行副本的区别，不允许客户端指定来源。
    """

    name: str
    installation_id: str
    source_path: Path
    execution_path: Path
    manifest: WorkerManifest
    state: str
    is_development: bool = False
    has_pending_resources: bool = False


@dataclass(slots=True)
class _DevelopmentPlugin:
    source: DevelopmentSource
    identity: tuple[int, int]
    token: str
    protected_digest: str = ""
    signature: tuple[tuple[str, int, int, int, int], ...] = ()
    changed_at: float | None = None
    digest: str = ""
    status: str = "preparing"
    error_code: str | None = None
    reload_count: int = 0
    supports_hot_reload: bool = True
    wants_enabled: bool = True
    needs_retry: bool = False


class PluginDevelopmentService:
    """
    开发信任只存在于当前进程；源文件变化不原地修改正在执行的副本。

    轮询只检测，实际换代复用 Worker 服务的锁、授权和真实清理；不接管用户文件。
    """

    ignored_directories: ClassVar[frozenset[str]] = frozenset(
        {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    )
    ignored_suffixes: ClassVar[tuple[str, ...]] = (".pyc", ".pyo", ".swp", ".swo", ".tmp", "~")

    def __init__(
        self,
        workers: PluginWorkerService,
        data_path: Path,
        *,
        changed: Callable[[], None] | None = None,
        interval: float = 0.75,
        debounce: float = 0.75,
    ) -> None:
        """
        绑定唯一 Worker 服务，创建时不监听或信任任何目录。

        :param workers: 应用拥有的 Worker 服务
        :param data_path: 宿主私有目录
        :param changed: 可信界面的状态失效通知
        :param interval: 轮询秒数
        :param debounce: 文件稳定等待秒数
        """
        self._workers = workers
        self._root_path = data_path.resolve() / "plugins" / "development_snapshots"
        self._changed = changed or (lambda: None)
        self._interval = interval
        self._debounce = debounce
        self._lock = RLock()
        self._transition_lock = RLock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._is_enabled = False
        self._plugins: dict[str, _DevelopmentPlugin] = {}
        self._copies: dict[Path, tuple[int, int]] = {}
        self._cleanup_notified: set[Path] = set()
        self._logger = get_logger("PluginDevelopment")

    def manages(self, name: str) -> bool:
        """
        查询此安装是否仍持有开发执行副本，供普通发现避免替换正在运行的内容。

        :param name: 安装名称
        :return: 安装由本会话接管且未要求重新审阅时为 True
        """
        with self._lock:
            plugin = self._plugins.get(name)
            return self._is_enabled and plugin is not None and plugin.status != "review_required"

    def intent(self, name: str, enabled: bool) -> None:
        """
        记录真实用户启停意图，源码迟到事件不得重新启用被禁用的插件。

        :param name: 安装名称
        :param enabled: 用户期望的启用状态
        """
        with self._lock:
            plugin = self._plugins.get(name)
            if plugin:
                plugin.wants_enabled = enabled
                plugin.status = "watching" if enabled else "disabled"
                if enabled:
                    plugin.needs_retry = True

    def wants_enabled(self, name: str) -> bool:
        """
        换代取得 Worker 锁后再检查实际用户意图，防止卸载或禁用与准备交错。

        :param name: 安装名称
        :return: 当前会话仍期望启动此安装时为 True
        """
        with self._lock:
            plugin = self._plugins.get(name)
            return self._is_enabled and plugin is not None and plugin.wants_enabled

    def prepare_enable(self, name: str, *, frontend_ready: bool) -> None:
        """
        在正常启用入口为已审阅目录准备首个稳定运行，避免先执行一次源目录。

        :param name: 正常生命周期已决定启用的安装名称
        :param frontend_ready: 可信主界面是否已经就绪
        :raises ProtocolError: 审阅或稳定副本准备未完成时抛出，不回退执行源目录
        """
        with self._transition_lock:
            if not self._is_enabled or name in self._plugins:
                return
            source = next((item for item in self._workers.directory_sources() if item.name == name), None)
            if source is None:
                return
            self._enroll(source, frontend_ready=frontend_ready)
            plugin = self._plugins[name]
            if plugin.error_code:
                raise ProtocolError("插件开发运行尚未就绪", plugin.error_code)

    def forget(self, name: str) -> None:
        """
        安装替换或卸载时终结该来源的开发信任，不删除用户目录。

        :param name: 安装名称
        """
        with self._lock:
            plugin = self._plugins.pop(name, None)
            if plugin is not None:
                plugin.wants_enabled = False
        if plugin is not None:
            self._changed()

    def view(self) -> dict[str, JsonValue]:
        """
        返回不含路径、凭据和原始异常的开发状态。

        :return: 开关及逐插件状态
        """
        with self._lock:
            return {
                "enabled": self._is_enabled,
                "items": [
                    {
                        "name": name,
                        "status": plugin.status,
                        "error_code": plugin.error_code,
                        "reload_count": plugin.reload_count,
                        "supports_hot_reload": plugin.supports_hot_reload,
                    }
                    for name, plugin in self._plugins.items()
                ],
            }

    def set_enabled(self, enabled: bool) -> dict[str, JsonValue]:
        """
        开启本次开发会话或撤销临时信任并停止开发运行，不持久化代码允许决定。

        :param enabled: 来自可信管理入口的严格布尔值
        :return: 当前实际状态
        :raises ProtocolError: 关闭时还有运行或副本未能清理时抛出
        """
        if type(enabled) is not bool:
            raise ProtocolError("开发模式状态无效", "PLUGIN_SCHEMA_INVALID")
        with self._transition_lock:
            with self._lock:
                self._is_enabled = enabled
            if enabled:
                self._stop.clear()
                self.poll()
                if self._thread is None or not self._thread.is_alive():
                    self._thread = Thread(target=self._loop, name="plugin_development", daemon=True)
                    self._thread.start()
            else:
                self._stop.set()
                for name, plugin in tuple(self._plugins.items()):
                    try:
                        self._workers.stop(name)
                        self._workers.restore_directory(name, plugin.source.source_path)
                    except (OSError, ValueError, PluginRuntimeError) as exc:
                        self._failure(plugin, exc, "cleanup_pending")
                        continue
                    with self._lock:
                        self._plugins.pop(name, None)
                self._cleanup_copies()
            self._changed()
            if not enabled and self._plugins:
                raise ProtocolError("开发运行仍在完成清理", "PLUGIN_HOST_CLEANUP_PENDING")
            return self.view()

    def _scan(self, root_path: Path) -> tuple[tuple[str, int, int, int, int], ...]:
        """
        用统一排除规则遍历本地源目录，拒绝链接、目录换绑与超限输入。
        """
        if root_path.is_symlink() or not root_path.is_dir() or root_path.resolve() != root_path:
            raise ProtocolError("开发目录无效", "PLUGIN_CONTENT_CHANGED")
        pending = [root_path]
        entries: list[tuple[str, int, int, int, int]] = []
        total_bytes = 0
        while pending:
            directory = pending.pop()
            for file_path in directory.iterdir():
                if file_path.name in self.ignored_directories or file_path.name.endswith(self.ignored_suffixes):
                    continue
                if file_path.is_symlink() or not file_path.resolve().is_relative_to(root_path):
                    raise ProtocolError("开发文件超出目录边界", "PLUGIN_CONTENT_CHANGED")
                if file_path.is_dir():
                    pending.append(file_path)
                    continue
                if not file_path.is_file():
                    raise ProtocolError("开发目录包含非普通文件", "PLUGIN_CONTENT_CHANGED")
                stat = file_path.stat()
                entries.append(
                    (
                        file_path.relative_to(root_path).as_posix(),
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_dev,
                        stat.st_ino,
                    )
                )
                total_bytes += stat.st_size
                if len(entries) > CodeSnapshot.max_files or total_bytes > CodeSnapshot.max_bytes:
                    raise ProtocolError("开发目录超过容量上限", "PLUGIN_CONTENT_CHANGED")
        return tuple(sorted(entries))

    def _prepare(self, plugin: _DevelopmentPlugin) -> tuple[Path, str, str, bool]:
        """
        复制完整源文件并二次比对字节，准备失败不停止有效旧运行。
        """
        source_path = plugin.source.source_path
        identity = source_path.stat()
        if (identity.st_dev, identity.st_ino) != plugin.identity:
            raise ProtocolError("开发目录身份已经变化", "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED")
        signature = self._scan(source_path)
        parent_path = self._root_path / plugin.source.installation_id
        if self._root_path.resolve() != self._root_path or parent_path.resolve() != parent_path:
            raise ProtocolError("开发副本目录无效", "PLUGIN_STORAGE_FAILED")
        if sum(path.parent == parent_path for path in self._copies) >= 2:
            raise ProtocolError("开发副本仍待清理", "PLUGIN_HOST_CLEANUP_PENDING")
        copy_path = parent_path / uuid4().hex
        copy_path.mkdir(parents=True, exist_ok=False)
        stat = copy_path.stat()
        self._copies[copy_path] = (stat.st_dev, stat.st_ino)
        for name, *_ in signature:
            source_file = source_path / name
            content = source_file.read_bytes()
            target_path = copy_path / name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content)
            if target_path.suffix == ".py":
                compile(content, name, "exec")
        if self._scan(source_path) != signature:
            raise ProtocolError("开发文件仍在保存中", "PLUGIN_DEVELOPMENT_FILES_UNSTABLE")
        final_identity = source_path.stat()
        if (final_identity.st_dev, final_identity.st_ino) != plugin.identity:
            raise ProtocolError("开发目录身份已经变化", "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED")
        snapshot = CodeSnapshot.capture(copy_path)
        for file in snapshot.files:
            if hashlib.sha256((source_path / file.name).read_bytes()).hexdigest() != file.sha256:
                raise ProtocolError("开发文件仍在保存中", "PLUGIN_DEVELOPMENT_FILES_UNSTABLE")
        manifest = WorkerManifest.model_validate_json((copy_path / "plugin.json").read_bytes())
        if manifest.name != plugin.source.name:
            raise ProtocolError("开发插件身份已经变化", "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED")
        protected: dict[str, JsonValue] = manifest.model_dump(mode="json", exclude={"version", "supports_hot_reload"})
        protected["dependency_files"] = {
            item.name: item.sha256 for item in snapshot.files if item.name.startswith(("locks/", "wheels/"))
        }
        protected_digest = hashlib.sha256(json.dumps(protected, sort_keys=True).encode()).hexdigest()
        return copy_path, snapshot.digest(), protected_digest, manifest.supports_hot_reload

    def _enroll(self, source: DevelopmentSource, *, frontend_ready: bool = True) -> None:
        """
        仅为已经审阅且正常流程决定启用的目录建立开发会话，显式退出者仍使用稳定副本。
        """
        token = self._workers.development_approval(source.name)
        stat = source.source_path.stat()
        plugin = _DevelopmentPlugin(source, (stat.st_dev, stat.st_ino), token)
        with self._lock:
            self._plugins[source.name] = plugin
        self._reload(plugin, initial=True, frontend_ready=frontend_ready)

    def _failure(self, plugin: _DevelopmentPlugin, error: BaseException, status: str = "failed") -> None:
        """
        只发布稳定错误，不把源码、个人路径和异常正文转发给界面。
        """
        with self._lock:
            was_changed = plugin.status != status or plugin.error_code != getattr(
                error, "error_code", "PLUGIN_WORKER_EXECUTION_FAILED"
            )
            plugin.status = status
            plugin.error_code = getattr(error, "error_code", "PLUGIN_WORKER_EXECUTION_FAILED")
        if was_changed:
            if isinstance(error, SyntaxError):
                self._logger.warning(
                    "插件源码语法无效：插件：%s；相对文件：%s；行：%s",
                    plugin.source.name,
                    error.filename,
                    error.lineno,
                )
            self._logger.warning("插件开发版本未完成：插件：%s；错误：%s", plugin.source.name, plugin.error_code)
            self._changed()

    def _reload(
        self,
        plugin: _DevelopmentPlugin,
        *,
        initial: bool = False,
        manual: bool = False,
        frontend_ready: bool = True,
    ) -> None:
        """
        准备后再停止旧版，换代前后都检查真实用户意图；失败不重放旧实现。
        """
        try:
            with self._lock:
                if not self._is_enabled or not plugin.wants_enabled:
                    return
                should_force = manual or plugin.status == "cleanup_pending"
                plugin.status = "preparing"
            copy_path, digest, protected, supports = self._prepare(plugin)
            initial = initial or not plugin.digest
            if initial:
                manifest = WorkerManifest.model_validate_json((copy_path / "plugin.json").read_bytes())
                protected_fields = {"version", "supports_hot_reload"}
                if manifest.model_dump(exclude=protected_fields) != plugin.source.manifest.model_dump(
                    exclude=protected_fields
                ):
                    raise ProtocolError("插件声明需要重新审阅", "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED")
            with self._lock:
                plugin.supports_hot_reload = supports
            if not initial and protected != plugin.protected_digest:
                self._workers.stop(plugin.source.name)
                with self._lock:
                    plugin.status = "review_required"
                    plugin.error_code = "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED"
                return
            running = any(
                item.name == plugin.source.name and item.state == "enabled"
                for item in self._workers.directory_sources()
            )
            if not initial and not should_force and (not supports or (digest == plugin.digest and running)):
                with self._lock:
                    plugin.status = "watching" if supports else "opted_out"
                return
            with self._lock:
                if not self._is_enabled or not plugin.wants_enabled:
                    return
                plugin.status = "reloading"
            self._workers.activate_development(
                plugin.source.name,
                copy_path,
                plugin.token,
                frontend_ready=frontend_ready,
            )
            with self._lock:
                plugin.digest = digest
                plugin.protected_digest = protected
                plugin.reload_count += 1
                plugin.error_code = None
                plugin.needs_retry = False
                plugin.status = "watching" if supports else "opted_out"
            self._logger.info("插件开发版本已加载：插件：%s；次数：%d", plugin.source.name, plugin.reload_count)
        except (OSError, ValueError, SyntaxError, PluginRuntimeError) as exc:
            current_source = next(
                (item for item in self._workers.directory_sources() if item.name == plugin.source.name), None
            )
            status = "cleanup_pending" if current_source and current_source.has_pending_resources else "failed"
            if getattr(exc, "error_code", None) in {"PLUGIN_HOST_CLEANUP_PENDING", "PLUGIN_JOB_STOP_PENDING"}:
                status = "cleanup_pending"
            if getattr(exc, "error_code", None) == "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED":
                status = "review_required"
            self._failure(plugin, exc, status)
        finally:
            self._cleanup_copies()
            self._changed()

    def _reload_changed_source(self, plugin: _DevelopmentPlugin, current: float) -> None:
        """
        核对单个目录身份并防抖换代，清理重试不受相同字节判断提前跳过。
        """
        identity = plugin.source.source_path.stat()
        if (identity.st_dev, identity.st_ino) != plugin.identity:
            raise ProtocolError("开发目录身份已经变化", "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED")
        signature = self._scan(plugin.source.source_path)
        if signature != plugin.signature:
            plugin.signature = signature
            plugin.changed_at = current
        if plugin.status == "cleanup_pending":
            plugin.needs_retry = True
        if plugin.needs_retry or (plugin.changed_at is not None and current - plugin.changed_at >= self._debounce):
            plugin.changed_at = None
            plugin.needs_retry = False
            self._reload(plugin)

    def poll(self, now: float | None = None) -> None:
        """
        检测一轮稳定变化，合并连续保存；实际换代不持有状态锁。

        :param now: 测试或宿主单调时钟，省略时读取当前时间
        """
        with self._transition_lock:
            if not self._is_enabled:
                return
            current = monotonic() if now is None else now
            sources = {item.name: item for item in self._workers.directory_sources()}
            for name, source in sources.items():
                previous = self._plugins.get(name)
                if source.state == "enabled" and (
                    previous is None or (previous.status == "review_required" and not source.is_development)
                ):
                    try:
                        self._enroll(source)
                    except (OSError, ValueError, PluginRuntimeError):
                        self._logger.warning("插件暂不能进入开发模式：插件：%s", name)
            for name, plugin in tuple(self._plugins.items()):
                if not plugin.wants_enabled or plugin.status == "review_required":
                    continue
                try:
                    self._reload_changed_source(plugin, current)
                except (OSError, ValueError, PluginRuntimeError) as exc:
                    if not plugin.source.source_path.is_dir():
                        self._workers.restore_directory(name, plugin.source.source_path)
                        self.forget(name)
                        continue
                    if getattr(exc, "error_code", None) == "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED":
                        self._workers.stop(name)
                        self._failure(plugin, exc, "review_required")
                    else:
                        self._failure(plugin, exc)
            self._cleanup_copies()

    def retry(self, name: str) -> None:
        """
        以现有生命周期锁同步重载一个开发插件，不改变用户启停选择。

        :param name: 已纳入开发会话的安装名称
        :raises ProtocolError: 安装尚未纳入本会话时抛出
        """
        with self._transition_lock:
            plugin = self._plugins.get(name)
            if plugin is None:
                raise ProtocolError("插件没有开发会话", "PLUGIN_RESOURCE_NOT_FOUND")
            self._reload(plugin, manual=True)
            if plugin.error_code:
                raise ProtocolError("插件开发重载未完成", plugin.error_code)

    def _cleanup_copies(self) -> None:
        """
        只清理已确认非活动且物理身份未变的本会话副本，不扫描用户历史目录。
        """
        active_paths = {item.execution_path for item in self._workers.directory_sources()}
        for copy_path, identity in tuple(self._copies.items()):
            if copy_path in active_paths:
                continue
            try:
                if not copy_path.exists():
                    self._copies.pop(copy_path, None)
                    continue
                stat = copy_path.stat()
                if (
                    copy_path.is_symlink()
                    or not copy_path.resolve().is_relative_to(self._root_path)
                    or (stat.st_dev, stat.st_ino) != identity
                ):
                    raise OSError("开发副本身份已经变化")
                remove_tree(copy_path)
                self._copies.pop(copy_path, None)
                self._cleanup_notified.discard(copy_path)
            except OSError:
                if copy_path not in self._cleanup_notified:
                    self._cleanup_notified.add(copy_path)
                    self._logger.warning("插件开发副本仍待清理")

    def _loop(self) -> None:
        """
        在应用线程中轮询，异常必须可观察；关闭由所属服务显式发出。
        """
        while not self._stop.wait(self._interval):
            try:
                self.poll()
            except Exception:
                self._logger.exception("插件开发目录检测失败")

    def close(self) -> None:
        """
        撤销本会话并有界等待检测线程结束，未完成清理明确失败。

        :raises ProtocolError: 线程或开发运行尚未停止时抛出
        """
        try:
            self.set_enabled(False)
        finally:
            self._stop.set()
            if self._thread is not None and current_thread() is not self._thread:
                self._thread.join(2)
                if self._thread.is_alive():
                    raise ProtocolError("开发检测线程尚未结束", "PLUGIN_HOST_CLEANUP_PENDING")
