# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：下载并校验 Java 到专用临时目录，原子安装登记，并持久核对运行时占用。
#
# 公开接口：
#   - class JavaInstaller — 在后台任务中安装 Java，不修改调用方传入的启动设置。
#   - class JavaLifecycle — 核对、取得及释放持久使用证据。
#   - class RuntimeLease — 关联启动阶段和实际游戏进程的运行时占用。
# ============================================================
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import psutil

from ECL.game import JavaRuntime, JavaScanner
from ECL.utils import atomic_write_text
from ECL.utils.network import download_proxy_url

from .models import CleanupRecord, JavaError, JavaInstallPlan, JavaPolicy, JavaRegistry, RuntimeRecord, RuntimeUsage
from .packages import JavaArchive

if TYPE_CHECKING:
    from ECL.services.operations import OperationContext


class JavaInstaller:
    """
    下载、解压和探测使用专用临时目录，最终提交与取消互斥。

    每次提交只安装一个确定的包；安装失败保留旧版本和全部启动配置。
    """

    def __init__(self, registry: JavaRegistry, ensure_unused: Callable[[RuntimeRecord], None]) -> None:
        """
        注入登记根目录，所有磁盘变更限制在该根目录内。

        :param registry: 唯一的运行时登记清单
        :param ensure_unused: 移动已有安装前重新核对进程占用的回调
        """
        self.registry = registry
        self._ensure_unused = ensure_unused
        self._logger = logging.getLogger(__name__)

    def install(self, plan: JavaInstallPlan, context: OperationContext) -> RuntimeRecord:
        """
        执行一个已验证计划，持久化失败时回滚本次目录。

        :param plan: 后端保存的不可变安装计划
        :param context: 用于取消任务、报告进度和保存最终结果的任务对象
        :return: 已提交的运行时
        :raises JavaError: 下载、校验、探测或提交失败时抛出
        """
        if JavaPolicy.host() != (plan.package.platform, plan.package.architecture):
            raise JavaError("Java 下载包与当前平台不一致", "JAVA_PLATFORM_MISMATCH")
        with self.registry.locked():
            state = self.registry.read()
            existing = next(
                (
                    item
                    for item in state.runtimes
                    if item.install_path == plan.install_path and item.package_checksum == plan.package.checksum
                ),
                None,
            )
            if existing and plan.install_path.exists():
                self.registry.validate_owned_directory(existing)
                try:
                    self._probe(plan.install_path, plan, context)
                except JavaError:
                    self._ensure_unused(existing)
                else:
                    return existing.model_copy(update={"validation_status": "valid"})
        staging_root = self.registry.root_path / ".staging"
        staging_root.mkdir(exist_ok=True)
        if staging_root.is_symlink():
            raise JavaError("Java 临时目录已被替换", "JAVA_DIRECTORY_CHANGED")
        stage = Path(mkdtemp(dir=staging_root))
        archive = stage / plan.package.filename
        unpacked = stage / "unpacked"
        try:
            context.check_cancelled()
            if shutil.disk_usage(self.registry.root_path).free < plan.estimated_free_bytes:
                raise JavaError("磁盘可用空间不足以安装 Java", "JAVA_DISK_SPACE_LOW")
            self._download(plan, archive, context)
            context.progress(76, "正在解压 Java", stage="extracting")
            installed_files = JavaArchive.extract(archive, unpacked, context.check_cancelled)
            context.progress(92, "正在验证 Java 运行时", stage="probing")
            runtime = self._probe(unpacked, plan, context)
            relative_executable = runtime.path.relative_to(unpacked)
            final_runtime = JavaRuntime(
                plan.install_path / relative_executable,
                runtime.version,
                runtime.vendor,
                runtime.architecture,
                runtime.is_jdk,
            )
            record = JavaPolicy.record(final_runtime, "managed").model_copy(
                update={
                    "install_path": plan.install_path,
                    "installed_files": installed_files,
                    "distribution_source": "temurin",
                    "release_name": plan.package.release_name,
                    "package_checksum": plan.package.checksum,
                    "package_platform": plan.package.platform,
                    "registered_at": datetime.now(UTC).isoformat(),
                }
            )
            context.progress(97, "正在登记 Java", stage="registering")
            result = context.finalize(lambda: self._commit(plan, unpacked, record))
            return RuntimeRecord.model_validate(result)
        finally:
            try:
                shutil.rmtree(stage)
            except OSError:
                self._logger.warning("Java 临时安装目录清理未完成，保留待清理记录")
                with self.registry.locked():
                    state = self.registry.read()
                    relative = stage.relative_to(self.registry.root_path).as_posix()
                    cleanup = CleanupRecord(
                        relative_path=relative,
                        owned_files=tuple(path.relative_to(stage).as_posix() for path in stage.rglob("*")),
                    )
                    self.registry.write(state.model_copy(update={"cleanup_paths": (*state.cleanup_paths, cleanup)}))

    def _commit(self, plan: JavaInstallPlan, unpacked: Path, record: RuntimeRecord) -> RuntimeRecord:
        """
        在短时文件互斥内移动并登记，只有本次移动成功的目录可被回滚。
        """
        with self.registry.locked():
            state = self.registry.read()
            old_backup: Path | None = None
            if plan.install_path.exists():
                existing = next(
                    (
                        item
                        for item in state.runtimes
                        if item.install_path == plan.install_path and item.package_checksum == plan.package.checksum
                    ),
                    None,
                )
                if existing:
                    self.registry.validate_owned_directory(existing)
                    self._ensure_unused(existing)
                    old_backup = self.registry.root_path / ".trash" / uuid4().hex
                    old_backup.parent.mkdir(exist_ok=True)
                    if old_backup.parent.is_symlink():
                        raise JavaError("Java 清理目录已被替换", "JAVA_DIRECTORY_CHANGED")
                    plan.install_path.rename(old_backup)
                else:
                    raise JavaError("Java 目标目录已有内容，安装未覆盖", "JAVA_INSTALL_TARGET_EXISTS")
            plan.install_path.parent.mkdir(exist_ok=True)
            if (
                plan.install_path.parent.is_symlink()
                or plan.install_path.parent.resolve().parent != self.registry.root_path
            ):
                raise JavaError("Java 安装目录已被替换", "JAVA_DIRECTORY_CHANGED")
            try:
                unpacked.rename(plan.install_path)
                previous = next((item for item in state.runtimes if item.runtime_id == record.runtime_id), None)
                if previous:
                    record = record.model_copy(update={"is_enabled": previous.is_enabled})
                if old_backup and existing:
                    cleanup = CleanupRecord(
                        relative_path=old_backup.relative_to(self.registry.root_path).as_posix(),
                        owned_files=existing.installed_files,
                    )
                    state = state.model_copy(update={"cleanup_paths": (*state.cleanup_paths, cleanup)})
                self.registry.replace(state, record)
            except Exception:
                if plan.install_path.exists():
                    plan.install_path.rename(unpacked)
                if old_backup:
                    old_backup.rename(plan.install_path)
                raise
            return record

    @staticmethod
    def _probe(root: Path, plan: JavaInstallPlan, context: OperationContext) -> JavaRuntime:
        """
        校验真实 Java，Java 8 JDK 的内嵌 JRE 不作为 JDK 主入口。
        """
        executable_name = "java.exe" if plan.package.platform == "windows" else "java"
        matches: list[JavaRuntime] = []
        for path in root.rglob(executable_name):
            context.check_cancelled()
            if not path.resolve().is_relative_to(root):
                raise JavaError("Java 可执行路径超出安装目录", "JAVA_ARCHIVE_UNSAFE")
            runtime = JavaScanner.probe(path)
            if runtime is None:
                continue
            major = JavaPolicy.version_key(runtime.version)[0]
            kind = "JDK" if runtime.is_jdk else "JRE"
            if (
                major == plan.package.major_version
                and JavaPolicy.architecture(runtime.architecture) == plan.package.architecture
                and kind == plan.package.runtime_kind
            ):
                matches.append(runtime)
        if len(matches) != 1:
            raise JavaError("安装包未包含唯一的适用 Java 入口", "JAVA_PACKAGE_PROBE_FAILED")
        return matches[0]

    @staticmethod
    def _download(plan: JavaInstallPlan, archive: Path, context: OperationContext) -> None:
        """
        分块下载并限量校验，超时或取消由实际工作线程结束后报告。
        """
        context.progress(1, "正在下载 Java", stage="downloading")
        deadline = monotonic() + 900
        for attempt in range(3):
            context.check_cancelled()
            try:
                with (
                    httpx.Client(
                        proxy=download_proxy_url(), trust_env=False, follow_redirects=True, max_redirects=5, timeout=15
                    ) as client,
                    client.stream("GET", plan.package.download_url) as response,
                ):
                    response.raise_for_status()
                    if response.url.scheme != "https":
                        raise JavaError("Java 下载发生不安全跳转", "JAVA_DOWNLOAD_FAILED")
                    digest = hashlib.sha256()
                    downloaded_bytes = 0
                    sampled_bytes = 0
                    sampled_at = monotonic()
                    with archive.open("wb") as output:
                        for chunk in response.iter_bytes():
                            context.check_cancelled()
                            if monotonic() >= deadline:
                                raise JavaError("Java 下载耗时超过上限，请调整下载代理后重试", "JAVA_DOWNLOAD_TIMEOUT")
                            downloaded_bytes += len(chunk)
                            if downloaded_bytes > plan.package.download_bytes:
                                raise JavaError("Java 安装包大小不符", "JAVA_HASH_MISMATCH")
                            digest.update(chunk)
                            output.write(chunk)
                            now = monotonic()
                            if now - sampled_at >= 0.25:
                                context.progress(
                                    min(72, downloaded_bytes * 72 / plan.package.download_bytes),
                                    "正在下载 Java",
                                    stage="downloading",
                                    done=downloaded_bytes,
                                    total=plan.package.download_bytes,
                                    speed=(downloaded_bytes - sampled_bytes) / (now - sampled_at),
                                )
                                sampled_at, sampled_bytes = now, downloaded_bytes
                    context.progress(74, "正在校验 Java 安装包", stage="verifying")
                    if downloaded_bytes != plan.package.download_bytes or digest.hexdigest() != plan.package.checksum:
                        raise JavaError("Java 安装包哈希或大小不符", "JAVA_HASH_MISMATCH")
                    return
            except httpx.HTTPError as exc:
                if attempt == 2 or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in {408, 429, 500, 502, 503, 504}
                ):
                    raise JavaError("Java 下载失败，请检查下载代理后重试", "JAVA_DOWNLOAD_FAILED") from exc


class JavaLifecycle:
    """
    将 PID 与进程启动时间一起核对，避免 PID 复用导致错误使用判定。

    无法读取的占用证据保持阻断，不据清单为空推断未被使用。
    """

    registry: JavaRegistry
    usage_path: Path

    def __init__(self, registry: JavaRegistry) -> None:
        """
        归属使用证据目录，不取得共享网络或游戏进程资源。

        :param registry: 唯一的运行时登记清单
        """
        self.registry = registry
        self.usage_path = registry.root_path / ".leases"
        if self.usage_path.is_symlink():
            raise JavaError("Java 使用记录目录已被替换", "JAVA_DIRECTORY_CHANGED")
        self.usage_path.mkdir(exist_ok=True)

    def path(self, lease_id: str) -> Path:
        """
        返回仅由后端生成的使用证据路径。

        :param lease_id: 后端生成的使用证据身份
        """
        if len(lease_id) != 32 or any(character not in "0123456789abcdef" for character in lease_id):
            raise JavaError("Java 使用记录身份无效", "JAVA_USAGE_UNKNOWN")
        return self.usage_path / f"{lease_id}.json"

    def write(self, usage: RuntimeUsage) -> None:
        """
        在 registry.locked 内原子保存使用证据，失败保留上一份证据。

        :param usage: 已确认的进程身份与运行时使用信息
        """
        atomic_write_text(self.path(usage.lease_id), usage.model_dump_json())

    def acquire(self, runtime_id: str) -> RuntimeLease:
        """
        在调用方持有登记互斥时为启动阶段取得持久占用。

        :param runtime_id: 已验证且启用的运行时
        :return: 由退出回调或失败清理释放的租约
        """
        usage = RuntimeUsage(
            lease_id=uuid4().hex,
            runtime_id=runtime_id,
            process_id=os.getpid(),
            process_started_at=psutil.Process().create_time(),
        )
        self.write(usage)
        return RuntimeLease(self, usage)

    def is_in_use(self, runtime_id: str) -> bool:
        """
        在登记互斥下核对当前证据，并清除确定已退出的租约。

        :raises JavaError: 证据或进程身份无法确认时抛出，阻止移除

        :param runtime_id: 已登记的运行时身份
        """
        for file_path in self.usage_path.glob("*.json"):
            try:
                if file_path.is_symlink() or file_path.stat().st_size > 4096:
                    raise ValueError("invalid usage")
                usage = RuntimeUsage.model_validate_json(file_path.read_bytes())
                process = psutil.Process(usage.process_id)
                is_live = process.is_running() and abs(process.create_time() - usage.process_started_at) < 0.01
            except psutil.NoSuchProcess:
                is_live = False
            except (OSError, ValueError, psutil.AccessDenied) as exc:
                raise JavaError("Java 的进程占用状态暂无法确认", "JAVA_USAGE_UNKNOWN") from exc
            if not is_live:
                file_path.unlink(missing_ok=True)
            elif usage.runtime_id == runtime_id:
                return True
        return False


@dataclass(slots=True)
class RuntimeLease:
    """
    保存一次启动的持久占用，实际进程接管后不因启动器关闭而释放。
    """

    lifecycle: JavaLifecycle
    usage: RuntimeUsage
    is_game: bool = False
    is_released: bool = False

    def attach_process(self, process_id: int) -> None:
        """
        用真实进程身份替换启动阶段租约；已退出的进程幂等释放。

        :param process_id: InstancesManager 返回的实际进程编号
        """
        try:
            started_at = psutil.Process(process_id).create_time()
        except psutil.NoSuchProcess:
            self.release()
            return
        with self.lifecycle.registry.locked():
            if self.is_released:
                return
            updated = self.usage.model_copy(
                update={"process_id": process_id, "process_started_at": started_at, "is_game": True}
            )
            self.lifecycle.write(updated)
            self.usage = updated
            self.is_game = True

    def release(self) -> None:
        """
        在实际退出或启动失败时幂等删除占用证据。
        """
        with self.lifecycle.registry.locked():
            if self.is_released:
                return
            self.lifecycle.path(self.usage.lease_id).unlink(missing_ok=True)
            self.is_released = True
