# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：下载并校验 Java 到专用临时目录，探测成功后原子安装和登记。
#
# 公开接口：
#   - class JavaInstaller — 在应用任务执行边界完成安装，不改调用方启动配置。
# ============================================================
from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from uuid import uuid4

import httpx

from ECL.game import JavaRuntime, JavaScanner
from ECL.services.operations import OperationContext
from ECL.utils.network import download_proxy_url

from .archives import JavaArchive
from .models import CleanupRecord, JavaError, JavaInstallPlan, JavaPolicy, RuntimeRecord
from .registry import JavaRegistry


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
        :param context: 统一任务的取消、进度和最终提交边界
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
