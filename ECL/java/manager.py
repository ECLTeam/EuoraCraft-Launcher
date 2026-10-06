# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：协调 Java 清单、来源查询、安装任务、启动使用保护与托管移除。
#
# 公开接口：
#   - class JavaManager — 应用唯一 Java 管理服务，不替用户改写启动配置。
# ============================================================
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from threading import RLock
from time import monotonic, time
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from pydantic import JsonValue

from ECL.events import EventBus
from ECL.game import JavaRuntime, JavaScanner
from ECL.utils.logging import get_logger

from .installer import JavaInstaller, JavaLifecycle, RuntimeLease
from .models import (
    CleanupRecord,
    JavaError,
    JavaInstallPlan,
    JavaInventory,
    JavaPolicy,
    JavaReferenceConfig,
    JavaRegistry,
    RuntimeRecord,
    RuntimeView,
)
from .packages import JavaCatalog

if TYPE_CHECKING:
    from ECL.services.operations import OperationContext, OperationManager


class JavaManager:
    """
    统一拥有 Java 状态与后台任务，系统扫描不会删除失效的登记记录。

    使用应用共享任务和元数据客户端；关闭本服务只禁止新任务，不把正在
    运行的游戏误判为停止。长任务由共享执行器取消并清理。
    """

    registry: JavaRegistry
    lifecycle: JavaLifecycle
    catalog: JavaCatalog

    def __init__(
        self,
        data_path: Path,
        client: httpx.Client,
        operations: OperationManager,
        events: EventBus,
        config_provider: Callable[[], JavaReferenceConfig] | None = None,
        reference_provider: Callable[[Path, tuple[Path, ...]], tuple[str, ...]] | None = None,
    ) -> None:
        """
        归属登记及安装根目录，并注入当前配置与实例引用的只读提供者。

        :param data_path: 当前启动器数据目录
        :param client: 应用拥有的元数据 HTTP 客户端
        :param operations: 应用唯一任务管理器
        :param events: 应用事件总线
        :param config_provider: 读取全局及实例引用所需的配置快照
        :param reference_provider: 扫描实例独立设置，返回引用该 Java 的实例标签
        """
        self.registry = JavaRegistry(JavaPolicy.storage_root(data_path))
        self.lifecycle = JavaLifecycle(self.registry)
        self.catalog = JavaCatalog(client)
        self._installer = JavaInstaller(self.registry, self._ensure_unused)
        self._operations = operations
        self._events = events
        self._config_provider = config_provider or JavaReferenceConfig
        self._reference_provider = reference_provider
        self._inventory_lock = RLock()
        self._task_lock = RLock()
        self._probe_lock = RLock()
        self._plans: dict[str, JavaInstallPlan] = {}
        self._receipts_by_action: dict[tuple[str, str], dict[str, str]] = {}
        self._scanned: tuple[JavaRuntime, ...] | None = None
        self._probe_cache: dict[Path, tuple[tuple[int, ...], float, JavaRuntime | None]] = {}
        self._is_closed = False
        self._logger = get_logger("JavaManager")

    def _ensure_open(self) -> None:
        if self._is_closed:
            raise JavaError("Java 管理服务已关闭", "JAVA_MANAGER_CLOSED")

    def _probe(self, path: Path, *, force: bool = False) -> JavaRuntime | None:
        """
        按文件特征缓存短时探测，执行文件和 release 变化都使缓存失效。
        """
        try:
            stat = path.stat()
            release = path.parent.parent / "release"
            release_stat = release.stat() if release.is_file() else None
            signature = (
                stat.st_mtime_ns,
                stat.st_size,
                release_stat.st_mtime_ns if release_stat else 0,
                int((path.parent / "javac").is_file() or (path.parent / "javac.exe").is_file()),
            )
        except OSError:
            return None
        with self._probe_lock:
            cached = self._probe_cache.get(path)
            if cached and not force and cached[0] == signature and monotonic() - cached[1] < 15:
                return cached[2]
        runtime = JavaScanner.probe(path)
        with self._probe_lock:
            if len(self._probe_cache) >= 256:
                self._probe_cache.pop(next(iter(self._probe_cache)))
            self._probe_cache[path] = (signature, monotonic(), runtime)
        return runtime

    def _refresh_record(self, record: RuntimeRecord) -> RuntimeRecord:
        """
        更新可运行信息，保留启用状态、安装拥有权与失效条目身份。
        """
        runtime = self._probe(record.executable_path)
        if runtime is None:
            return record.model_copy(
                update={"validation_status": "invalid" if record.executable_path.is_file() else "missing"}
            )
        refreshed = JavaPolicy.record(runtime)
        return record.model_copy(
            update={
                "vendor": refreshed.vendor,
                "full_version": refreshed.full_version,
                "major_version": refreshed.major_version,
                "runtime_kind": refreshed.runtime_kind,
                "architecture": refreshed.architecture,
                "validation_status": "valid",
            }
        )

    def inventory(self, force: bool = False, extra_paths: tuple[Path, ...] = ()) -> JavaInventory:
        """
        合并扫描、长期登记和托管安装，返回可显示的全部状态。

        :param force: 是否重新扫描系统及探测登记条目
        :param extra_paths: 旧扫描接口明确传入的补充可执行路径
        :return: 单一运行时清单及真实占用和引用快照
        """
        self._ensure_open()
        with self._inventory_lock:
            with self.registry.locked():
                registered = self.registry.read()
            if force:
                with self._probe_lock:
                    self._probe_cache.clear()
            if force or self._scanned is None:
                configured = self._config_provider().java_path
                candidates = [str(item.executable_path) for item in registered.runtimes]
                candidates.extend(str(path) for path in extra_paths)
                if configured:
                    candidates.append(configured)
                scanner = JavaScanner(
                    cache_file=self.registry.root_path / "probe-cache.json", user_java_paths=candidates
                )
                self._scanned = tuple(scanner.scan())
            discovered = tuple(JavaPolicy.record(runtime) for runtime in self._scanned)
            with self.registry.locked():
                state = self.registry.read()
                records = {item.runtime_id: item for item in state.runtimes}
                for discovered_record in discovered:
                    # 扫描与移除可并发；登记锁内再次核对，避免把刚移除的路径重新登记。
                    if discovered_record.executable_path.is_file():
                        records.setdefault(discovered_record.runtime_id, discovered_record)
                refreshed = tuple(self._refresh_record(record) for record in records.values())
                changed = state.model_copy(update={"runtimes": refreshed})
                if changed != state:
                    self.registry.write(changed)
                views = tuple(self._view(record) for record in refreshed)
            system, architecture = JavaPolicy.host()
            return JavaInventory(
                runtimes=tuple(
                    sorted(views, key=lambda item: (-item.major_version, item.vendor, str(item.executable_path)))
                ),
                platform=system,
                architecture=architecture,
                managed_root_path=self.registry.root_path,
                cleanup_pending_count=len(state.cleanup_paths),
            )

    def _view(self, record: RuntimeRecord) -> RuntimeView:
        """
        为界面读取只读使用信息，无法确认时保守阻止移除。
        """
        try:
            references = self._references(record)
            in_use = self.lifecycle.is_in_use(record.runtime_id)
            is_usage_unknown = False
        except JavaError:
            references, in_use, is_usage_unknown = (), True, True
        return RuntimeView(
            **record.model_dump(), references=references, is_in_use=in_use, usage_unknown=is_usage_unknown
        )

    def _references(self, record: RuntimeRecord) -> tuple[str, ...]:
        """
        核对当前全局配置和已配置实例的手动引用，坏文件保留未知状态。
        """
        config = self._config_provider()
        references: list[str] = []
        if not config.java_auto and config.java_path and self._same_home(Path(config.java_path), record):
            references.append("全局设置")
        if self._reference_provider is not None:
            try:
                references.extend(self._reference_provider(record.java_home_path, config.minecraft_paths))
            except ValueError as exc:
                raise JavaError(str(exc), "JAVA_REFERENCES_UNKNOWN") from exc
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _same_home(path: Path, record: RuntimeRecord) -> bool:
        return path.expanduser().resolve(strict=False).parent.parent == record.java_home_path

    def register(self, path: Path) -> RuntimeRecord:
        """
        登记现有 Java，保存成功后通知唯一清单刷新。

        :param path: 明确选择或已解析的可执行路径
        """
        self._ensure_open()
        record = self.registry.register(path)
        self._changed()
        return record

    def set_enabled(self, runtime_id: str, is_enabled: bool) -> RuntimeRecord:
        """
        修改后续可用状态，不杀死已有游戏进程。

        :param runtime_id: 已登记的运行时身份
        :param is_enabled: 是否允许后续选择和启动
        """
        self._ensure_open()
        result = self.registry.set_enabled(runtime_id, is_enabled)
        self._changed()
        return result

    def validate_selection(self, runtime_id: str, required_major: int | None = None) -> RuntimeRecord:
        """
        选用前重新探测并校验启用与版本要求，失败保持原设置。

        :param runtime_id: 已登记的运行时身份
        :param required_major: 当前已知的最低 Java 主版本，未知时为空
        """
        with self.registry.locked():
            record = self.registry.find(self.registry.read(), runtime_id)
            if not record.is_enabled:
                raise JavaError("所选 Java 已停用，请先启用", "JAVA_DISABLED")
            runtime = self._probe(record.executable_path, force=True)
            if runtime is None:
                raise JavaError("所选 Java 已失效", "JAVA_INVALID_RUNTIME")
            if required_major and JavaPolicy.version_key(runtime.version)[0] < required_major:
                raise JavaError(f"该实例需要 Java {required_major} 或更高版本", "JAVA_VERSION_NOT_FOUND")
            return self._refresh_record(record)

    def select(self, required_major: int | None = None) -> Path:
        """
        保留最近适用主版本策略，同主版本按已探测的数字补丁倒序选择。

        :param required_major: 当前已知的最低 Java 主版本，未知时为空
        """
        records = [
            item
            for item in self.inventory().runtimes
            if item.is_enabled
            and item.validation_status == "valid"
            and (not required_major or item.major_version >= required_major)
        ]
        if not records:
            message = f"未找到 Java {required_major} 或更高版本" if required_major else "未找到可用 Java"
            raise JavaError(message, "JAVA_VERSION_NOT_FOUND" if required_major else "JAVA_NOT_FOUND")
        major = (
            min(item.major_version for item in records)
            if required_major
            else max(item.major_version for item in records)
        )
        host_architecture = JavaPolicy.host()[1]
        record = max(
            (item for item in records if item.major_version == major),
            key=lambda item: (
                item.architecture == host_architecture,
                JavaPolicy.version_key(item.full_version),
                str(item.executable_path),
            ),
        )
        return record.executable_path

    def acquire(self, path: Path) -> RuntimeLease:
        """
        在文件互斥下重新验证并取得启动租约，避免选择与删除竞态。

        :param path: 明确选择或已解析的可执行路径
        """
        with self.registry.locked():
            runtime = self._probe(path, force=True)
            if runtime is None:
                raise JavaError("Java 可执行文件不存在或无法运行", "JAVA_NOT_FOUND")
            candidate = JavaPolicy.record(runtime)
            state = self.registry.read()
            record = next((item for item in state.runtimes if item.runtime_id == candidate.runtime_id), candidate)
            if not record.is_enabled:
                raise JavaError("所选 Java 已停用", "JAVA_DISABLED")
            return self.lifecycle.acquire(record.runtime_id)

    def install_plan(self, package_id: str) -> JavaInstallPlan:
        """
        生成十五分钟有效的后端安装计划，目标目录不可由前端指定。

        :param package_id: 已校验的具体发布包身份
        """
        self._ensure_open()
        package = self.catalog.package(package_id)
        target = self.registry.root_path / "installed" / f"temurin-{package.major_version}-{package.checksum[:16]}"
        plan = JavaInstallPlan(
            plan_id=uuid4().hex,
            package=package,
            install_path=target,
            expires_at=time() + 900,
            estimated_free_bytes=package.download_bytes * 6 + 64 * 1024 * 1024,
        )
        with self._task_lock:
            self._plans = {key: value for key, value in self._plans.items() if value.expires_at > time()}
            if len(self._plans) >= 64:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan.plan_id] = plan
        return plan

    def install(self, plan_id: str) -> dict[str, str]:
        """
        提交或复用同一目标安装任务，网络工作由应用执行器拥有。

        :param plan_id: 已确认的安装计划身份
        :return: 新任务或同一在途任务的回执
        """
        self._ensure_open()
        with self._task_lock:
            plan = self._plans.get(plan_id)
            if plan is None or plan.expires_at <= time():
                raise JavaError("Java 安装计划已过期，请重新确认", "JAVA_PLAN_EXPIRED")

        def worker(context: OperationContext) -> dict[str, object]:
            record = self._installer.install(plan, context)
            self._changed()
            return {
                "runtimeId": record.runtime_id,
                "name": f"Java {record.major_version}",
                "runtime": record.model_dump(mode="json", by_alias=True, exclude={"installed_files"}),
            }

        return self._submit_once(("install", str(plan.install_path)), "java_install", worker)

    def _submit_once(
        self, key: tuple[str, str], kind: str, worker: Callable[[OperationContext], dict[str, object]]
    ) -> dict[str, str]:
        """
        在应用会话内按动作和目标合并在途请求，跨页回执不会创建重复工作。
        """
        with self._task_lock:
            receipt = self._receipts_by_action.get(key)
            if receipt and self._operations.get(receipt["operationId"]).get("status") in {"pending", "running"}:
                return dict(receipt)

            def execute(context: OperationContext) -> dict[str, object]:
                try:
                    return worker(context)
                finally:
                    with self._task_lock:
                        self._receipts_by_action.pop(key, None)

            receipt = self._operations.submit(kind, execute)
            self._receipts_by_action[key] = receipt
            return receipt

    def check_updates(self) -> tuple[dict[str, JsonValue], ...]:
        """
        检查同主版本同类型托管更新，外部运行时和跨主版本不自动更新。
        """
        inventory = self.inventory()
        updates: list[dict[str, JsonValue]] = []
        for record in inventory.runtimes:
            if record.origin != "managed" or record.distribution_source != "temurin":
                continue
            packages = self.catalog.packages(record.major_version, record.runtime_kind, force=True)
            latest = next(
                (
                    package
                    for package in packages
                    if package.architecture == record.architecture and package.platform == record.package_platform
                ),
                None,
            )
            if (
                latest
                and record.release_name
                and latest.checksum != record.package_checksum
                and JavaPolicy.version_key(latest.release_name) > JavaPolicy.version_key(record.release_name)
            ):
                updates.append(
                    {"runtimeId": record.runtime_id, "package": latest.model_dump(mode="json", by_alias=True)}
                )
        return tuple(updates)

    def forget(self, runtime_id: str) -> None:
        """
        移除手动登记而保留外部文件，系统条目和托管条目使用各自操作。

        :param runtime_id: 已登记的运行时身份
        """
        with self.registry.locked():
            state = self.registry.read()
            record = self.registry.find(state, runtime_id)
            if record.origin != "manual":
                raise JavaError("系统 Java 请停用，托管 Java 请使用移除安装", "JAVA_FORGET_UNSUPPORTED")
            self._check_removable(record)
            self.registry.write(
                state.model_copy(
                    update={"runtimes": tuple(item for item in state.runtimes if item.runtime_id != runtime_id)}
                )
            )
        self._changed()

    def _check_removable(self, record: RuntimeRecord) -> None:
        """
        在移除互斥内重新检查实际使用与配置引用，不相信界面旧快照。
        """
        self._ensure_unused(record)
        if self._references(record):
            raise JavaError("Java 仍被全局或实例设置使用，请先切换", "JAVA_REFERENCED")

    def _ensure_unused(self, record: RuntimeRecord) -> None:
        """
        真实进程占用禁止移动、重装或移除当前运行时。
        """
        if self.lifecycle.is_in_use(record.runtime_id):
            raise JavaError("Java 正被启动任务或游戏使用，请等待进程退出", "JAVA_IN_USE")

    def remove(self, runtime_id: str) -> dict[str, str]:
        """
        后台移除可信托管目录，登记失败恢复目录，清理失败留下重试记录。

        :param runtime_id: 已登记的运行时身份
        """
        self._ensure_open()

        def worker(context: OperationContext) -> dict[str, object]:
            context.progress(5, "正在核对 Java 使用状态", stage="checking")
            retired = self.registry.root_path / ".trash" / uuid4().hex

            def commit() -> RuntimeRecord:
                with self.registry.locked():
                    state = self.registry.read()
                    record = self.registry.find(state, runtime_id)
                    self._check_removable(record)
                    if record.origin != "managed":
                        raise JavaError("该 Java 不属于启动器安装", "JAVA_NOT_MANAGED")
                    if (
                        record.install_path is not None
                        and not record.install_path.exists()
                        and not record.install_path.is_symlink()
                    ):
                        self.registry.write(
                            state.model_copy(
                                update={
                                    "runtimes": tuple(item for item in state.runtimes if item.runtime_id != runtime_id)
                                }
                            )
                        )
                        return record
                    owned = self.registry.validate_owned_directory(record)
                    retired.parent.mkdir(exist_ok=True)
                    if retired.parent.is_symlink():
                        raise JavaError("Java 清理目录已被替换", "JAVA_DIRECTORY_CHANGED")
                    owned.rename(retired)
                    changed = state.model_copy(
                        update={
                            "runtimes": tuple(item for item in state.runtimes if item.runtime_id != runtime_id),
                            "cleanup_paths": (
                                *state.cleanup_paths,
                                CleanupRecord(
                                    relative_path=retired.relative_to(self.registry.root_path).as_posix(),
                                    owned_files=record.installed_files,
                                ),
                            ),
                        }
                    )
                    try:
                        self.registry.write(changed)
                    except Exception:
                        retired.rename(owned)
                        raise
                    return record

            context.finalize(commit)
            if not retired.exists():
                self._changed()
                return {"runtimeId": runtime_id, "cleanupPending": False}
            context.progress(75, "正在清理 Java 安装目录", stage="cleaning")
            try:
                shutil.rmtree(retired)
            except OSError:
                self._logger.warning("Java 已从清单移除，部分安装文件待清理")
                self._changed()
                return {"runtimeId": runtime_id, "cleanupPending": True}
            with self.registry.locked():
                state = self.registry.read()
                relative = retired.relative_to(self.registry.root_path).as_posix()
                self.registry.write(
                    state.model_copy(
                        update={
                            "cleanup_paths": tuple(
                                path for path in state.cleanup_paths if path.relative_path != relative
                            )
                        }
                    )
                )
            self._changed()
            return {"runtimeId": runtime_id, "cleanupPending": False}

        return self._submit_once(("remove", runtime_id), "java_remove", worker)

    def _changed(self) -> None:
        with self._inventory_lock:
            self._scanned = None
        self._events.emit("java:runtimes_changed", {})

    def cleanup(self) -> dict[str, str]:
        """
        重试已登记的遗留清理，路径及文件集合在实际执行时再次验证。

        :return: 应用任务回执；不能清理的条目继续保留
        """
        self._ensure_open()

        def worker(context: OperationContext) -> dict[str, object]:
            with self.registry.locked():
                pending = self.registry.read().cleanup_paths
            context.finalize(lambda: None)
            completed: set[str] = set()
            for index, entry in enumerate(pending):
                context.check_cancelled()
                path = self._cleanup_path(entry)
                context.progress(index * 100 / max(1, len(pending)), "正在清理 Java 遗留文件", stage="cleaning")
                if not path.exists():
                    completed.add(entry.relative_path)
                    continue
                try:
                    shutil.rmtree(path)
                    completed.add(entry.relative_path)
                except OSError:
                    self._logger.warning("Java 遗留文件暂无法清理，保留待清理记录")
            with self.registry.locked():
                state = self.registry.read()
                self.registry.write(
                    state.model_copy(
                        update={
                            "cleanup_paths": tuple(
                                entry for entry in state.cleanup_paths if entry.relative_path not in completed
                            )
                        }
                    )
                )
            self._changed()
            return {"cleanedCount": len(completed), "remainingCount": len(pending) - len(completed)}

        return self._submit_once(("cleanup", "all"), "java_cleanup", worker)

    def _cleanup_path(self, entry: CleanupRecord) -> Path:
        """
        只清理专用临时/回收目录，链接替换或未知文件均拒绝。
        """
        pure = PurePosixPath(entry.relative_path)
        if len(pure.parts) != 2 or pure.parts[0] not in {".staging", ".trash"} or pure.parts[1] in {".", ".."}:
            raise JavaError("Java 清理目录记录无效", "JAVA_DIRECTORY_CHANGED")
        path = self.registry.root_path.joinpath(*pure.parts)
        if path.is_symlink() or path.parent.is_symlink() or path.resolve() != path.absolute():
            raise JavaError("Java 清理目录已被替换", "JAVA_DIRECTORY_CHANGED")
        for child in path.rglob("*"):
            if not child.resolve().is_relative_to(path) or child.relative_to(path).as_posix() not in entry.owned_files:
                raise JavaError("Java 清理目录含未知内容，保留原文件", "JAVA_DIRECTORY_CHANGED")
        return path

    def close(self) -> None:
        """
        禁止新操作，不关闭共享客户端，也不释放仍在运行游戏的租约。
        """
        self._is_closed = True
