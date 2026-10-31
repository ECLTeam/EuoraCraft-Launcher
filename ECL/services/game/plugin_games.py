# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为受信插件适配器提供固定来源的游戏查询与受控安装、进程操作，不暴露账户或启动命令。
#
# 公开接口：
#   - class GameCatalogEntry — 不含网络地址的版本目录项。
#   - class ResourceDownload — 已有在线资源服务选出的公开文件信息。
#   - class GameResourceTarget / PluginInstallTarget / InstalledGame — 物理目标与内部安装结果。
#   - class PluginGameRun / PluginLaunchControl / PluginLaunchReservation — 进程事实、提交控制与句柄归属。
#   - class PluginGameCoordinator — 复用游戏服务状态的插件游戏业务入口。
# ============================================================

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal

from packaging.version import InvalidVersion

from ECL.services.game.base import GameServiceError, _GameState
from ECL.services.game.install_progress import InstallationSnapshot
from ECL.services.game.install_targets import InstallReservation
from ECL.services.game.workspace import resolve_instance_target
from ECL.services.operations import OperationCommitRejectedError, OperationContext, OperationOrigin
from ECL.utils.instance_reading import DirectoryIdentity, directory_identity
from ECL.utils.windows_file_handles import FileBoundaryError, FileDirectory


@dataclass(frozen=True, slots=True)
class ResourceDownload:
    """
    仅保存公开文件名、下载地址、大小与校验摘要，不提供宿主目标路径。
    """

    filename: str
    url: str
    sha512: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class GameResourceTarget:
    """
    保存准入时确定的根及实例物理身份，不接受插件直接提供路径。
    """

    root_path: Path
    root_identity: DirectoryIdentity
    version_id: str | None = None
    instance_identity: DirectoryIdentity | None = None


@dataclass(frozen=True, slots=True)
class PluginInstallTarget:
    """
    记录本运行创建的未完成目录身份，重试不能换绑到其他实例或安装内容。
    """

    origin: OperationOrigin
    identity: DirectoryIdentity
    version_id: str
    loader: str
    loader_version: str | None


@dataclass(frozen=True, slots=True)
class InstalledGame:
    """
    保存内部安装结果，实际交付由当前运行的读取许可与显式投影决定。
    """

    root_path: Path
    version_name: str
    identity: DirectoryIdentity


@dataclass(frozen=True, slots=True)
class PluginGameRun:
    """
    保存一次已提交进程的公开事实，token仅供受信适配器映射运行引用。
    """

    token: str
    version_id: str
    pid: int
    running: bool
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class PluginLaunchControl:
    """
    将当前运行准入、取消和短进程创建提交接到既有异步启动流程。
    """

    origin: OperationOrigin
    context: OperationContext
    cancel_event: Event
    verify: Callable[[], None]


@dataclass(frozen=True, slots=True)
class PluginLaunchReservation:
    """
    拥有启动占位及固定目录句柄，实际收尾前不释放给后来的启动流程。
    """

    cancel_event: Event
    handles: list[FileDirectory] = field(default_factory=list, compare=False, hash=False, repr=False)


@dataclass(frozen=True, slots=True)
class GameCatalogEntry:
    """
    保存允许交付给插件的版本字段，不携带原始元数据地址。
    """

    version_id: str
    version_type: Literal["release", "snapshot"]
    release_time: str


class PluginGameCoordinator(_GameState):
    """
    使用宿主有效下载源及已有游戏服务，不接受插件选择网络地址或凭据。
    """

    def plugin_resource_download(self, source: str, project_id: str, version_id: str) -> ResourceDownload:
        """
        复用现有平台文件选择和用户下载源，仅读取公开下载信息。

        :param source: 已校验的平台名称
        :param project_id: 已校验的项目标识
        :param version_id: 已校验的平台版本标识
        :return: 不含提供方密钥的公开文件信息
        :raises GameServiceError: 平台数据结构无效或文件不可下载时抛出
        """
        selected = self._select_online_file(source, project_id, version_id)
        filename, url, size = selected.get("filename"), selected.get("url"), selected.get("size")
        hashes = selected.get("hashes")
        sha512 = hashes.get("sha512") if isinstance(hashes, dict) else None
        if not isinstance(filename, str) or not isinstance(url, str):
            raise GameServiceError("在线文件信息无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
        if sha512 is not None and not isinstance(sha512, str):
            raise GameServiceError("在线文件校验信息无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
        if size is not None and (type(size) is not int or size < 0):
            raise GameServiceError("在线文件大小无效", "PLUGIN_RESOURCE_CATALOG_INVALID")
        return ResourceDownload(filename, url, sha512, size)

    def plugin_resource_path(self, target: GameResourceTarget, kind: str) -> Path:
        """
        按现有隔离策略解析游戏资源位置，不由插件决定实例数据目录。

        :param target: 宿主已经验证的实例物理目标
        :param kind: 模组、资源包或光影的固定业务类型
        :return: 当前实例真实使用的资源目录
        :raises GameServiceError: 类型或实例不满足时抛出
        """
        if target.version_id is None:
            raise GameServiceError("资源管理需要具体实例", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        if kind == "mods":
            directory = "mods"
        elif kind == "resourcepacks":
            directory = "resourcepacks"
        elif kind == "shaders":
            directory = "shaderpacks"
        else:
            raise GameServiceError("游戏资源类型不支持", "PLUGIN_SCHEMA_INVALID")
        isolated = self.resolve_version_isolation(target.root_path, target.version_id)
        resolved = resolve_instance_target(target.root_path, target.version_id, isolated)
        return resolved.game_data_path / directory

    def plugin_catalog(
        self, version_type: Literal["release", "snapshot"] | None, deadline: float
    ) -> tuple[GameCatalogEntry, ...]:
        """
        读取正式版和快照目录，并在阻塞查询返回后复核截止时间。

        :param version_type: 可选的版本类型
        :param deadline: 本次查询的单调时钟截止
        :return: 仅含公开字段的有界版本目录
        :raises GameServiceError: 来源返回损坏目录或查询超时
        """
        if monotonic() >= deadline:
            raise GameServiceError("游戏目录查询超时", "PLUGIN_DEADLINE_EXCEEDED")
        catalog = self.minecraft_versions_classified(self._game_download_source_provider())
        entries = []
        for category in ("release", "snapshot"):
            if version_type is not None and version_type != category:
                continue
            for item in catalog.get(category, []):
                identifier, released = item.get("id"), item.get("releaseTime")
                if not isinstance(identifier, str) or not 1 <= len(identifier) <= 64 or not isinstance(released, str):
                    raise GameServiceError("游戏版本目录字段无效", "PLUGIN_GAME_CATALOG_FAILED")
                entries.append(GameCatalogEntry(identifier, category, released[:64]))
                if len(entries) > 10000:
                    raise GameServiceError("游戏版本目录超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        if monotonic() >= deadline:
            raise GameServiceError("游戏目录查询超时", "PLUGIN_DEADLINE_EXCEEDED")
        return tuple(entries)

    def plugin_loaders(self, game_version: str, loader: str, deadline: float) -> tuple[str, ...]:
        """
        仅对宿主目录中的版本查询受支持加载器，按与安装一致的规则降序返回。

        :param game_version: 正式版或快照的实际版本标识
        :param loader: 已校验的加载器类型
        :param deadline: 本次查询截止
        :return: 版本号元组，不含下载地址
        :raises GameServiceError: 游戏版本不在固定目录或结果无效
        """
        if not any(item.version_id == game_version for item in self.plugin_catalog(None, deadline)):
            raise GameServiceError("游戏版本不在宿主目录中", "PLUGIN_GAME_VERSION_INVALID")
        versions = self.loader_versions(loader, game_version, self._game_download_source_provider())
        if monotonic() >= deadline:
            raise GameServiceError("加载器目录查询超时", "PLUGIN_DEADLINE_EXCEEDED")
        if len(versions) > 10000 or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in versions):
            raise GameServiceError("加载器版本目录字段无效", "PLUGIN_GAME_CATALOG_FAILED")
        try:
            return tuple(
                sorted(
                    versions,
                    key=lambda value: self._install_loader_version_key(loader, game_version, value),
                    reverse=True,
                )
            )
        except InvalidVersion as exc:
            raise GameServiceError("加载器版本数据无法比较", "INVALID_LOADER_VERSION_DATA") from exc

    def plugin_install(
        self,
        context: OperationContext,
        origin: OperationOrigin,
        target: GameResourceTarget,
        reservation: InstallReservation,
        version_id: str,
        loader: str,
        loader_version: str | None,
        verify: Callable[[], None],
    ) -> InstalledGame:
        """
        在独占目标及固定物理目录内复用现有安装器，失败残留仅允许原运行继续。

        :param context: 唯一任务服务提供的进度、取消及提交控制
        :param origin: 宿主确认的安装和运行来源
        :param target: 已批准游戏根及原始身份
        :param reservation: 提交前登记的安装目标占位
        :param version_id: 宿主目录中的游戏版本
        :param loader: 已校验加载器类型
        :param loader_version: 明确版本或由宿主选择最新版
        :param verify: 复核原始安装许可及目录身份
        :return: 不直接交付给插件的内部完成结果
        :raises GameServiceError: 版本、目标、安装或取消不满足
        """
        context.check_cancelled()
        if not any(entry.version_id == version_id for entry in self.plugin_catalog(None, monotonic() + 30)):
            raise GameServiceError("游戏版本不在宿主目录中", "PLUGIN_GAME_VERSION_INVALID")
        if (
            loader != "vanilla"
            and loader_version is not None
            and loader_version not in self.plugin_loaders(version_id, loader, monotonic() + 30)
        ):
            raise GameServiceError("加载器版本不在宿主目录中", "PLUGIN_LOADER_VERSION_INVALID")
        verify()
        version_name = reservation.version_name
        identity_key = (reservation.root_identity, reservation.comparison_name)
        instance_path = target.root_path / "versions" / version_name
        source = self._game_download_source_provider()
        java_path = self._resolve_java_path(None) if loader in {"forge", "neoforge"} else None
        root = FileDirectory.open(target.root_path)
        reservation.handles.append(root)
        verify()
        is_existing = instance_path.exists() or instance_path.is_symlink()
        if is_existing:
            with self._lock:
                previous = self._plugin_install_targets.get(identity_key)
            current_identity = directory_identity(instance_path)
            if previous != PluginInstallTarget(origin, current_identity, version_id, loader, loader_version):
                raise GameServiceError("目标实例已经存在，不能由插件覆盖", "PLUGIN_FILE_EXISTS")
        context.report_effect("partial")
        versions = root.child("versions", create=not (target.root_path / "versions").exists())
        reservation.handles.append(versions)
        instance = versions.child(version_name, create=not is_existing)
        reservation.handles.append(instance)
        created_identity = directory_identity(instance_path)
        with self._lock:
            self._plugin_install_targets[identity_key] = PluginInstallTarget(
                origin, created_identity, version_id, loader, loader_version
            )

        def check() -> None:
            context.check_cancelled()
            verify()
            if directory_identity(instance_path) != created_identity:
                raise GameServiceError("安装目标身份发生变化", "PLUGIN_RESOURCE_UNAVAILABLE")

        def report(phase: str, message: str, *, installation: InstallationSnapshot, **_details: object) -> None:
            stage = installation["currentStageId"] or "resolve_metadata"
            current = next((entry for entry in installation["stages"] if entry["id"] == stage), None)
            progress = current["progress"] if current is not None else None
            total = progress["total"] if progress is not None else None
            percent = min(99, int(100 * progress["completed"] / total)) if total and progress else 0
            context.progress(percent, "插件正在安装游戏", stage=stage, installation=installation)

        try:
            self.install_blocking(
                context.operation_id,
                version_id,
                version_name,
                loader,
                loader_version,
                None,
                target.root_path,
                source,
                java_path,
                report,
                context.cancel_event,
                reservation=reservation,
                check_callback=check,
            )
        except GameServiceError as exc:
            if exc.error_code == "INSTALL_CANCELLED":
                raise GameServiceError("操作已取消", "OPERATION_CANCELLED") from exc
            raise
        check()
        result = InstalledGame(target.root_path, version_name, created_identity)
        context.finalize(lambda: result)
        with self._lock:
            self._plugin_install_targets.pop(identity_key, None)
        return result

    def release_plugin_install_records(self, origin: OperationOrigin) -> None:
        """
        运行结束后释放该运行的重试身份，不删除任何实例或残留文件。

        :param origin: 已结束的受信运行来源
        """
        with self._lock:
            self._plugin_install_targets = {
                key: record for key, record in self._plugin_install_targets.items() if record.origin != origin
            }

    async def bind_plugin_launch_loop(self) -> None:
        """
        绑定宿主现有异步循环，使插件启动与账户HTTP客户端使用同一生命周期。

        此方法由Tauri宿主在其portal线程调用，不创建额外事件循环。
        """
        with self._lock:
            self._plugin_launch_loop = asyncio.get_running_loop()

    def reserve_plugin_launch(self, origin: OperationOrigin) -> PluginLaunchReservation:
        """
        在任务入队前独占全局启动流程，不与用户启动并发准备同一执行器。

        :param origin: 宿主确认的当前插件来源
        :return: 只属于这次启动的取消及占位身份
        :raises GameServiceError: 宿主未就绪、已关闭、忙或引用达到上限
        """
        with self._lock:
            if self._closing or self._plugin_launch_loop is None or not self._plugin_launch_loop.is_running():
                raise GameServiceError("游戏启动宿主尚未就绪", "PLUGIN_HOST_NOT_READY")
            if self._launch_cancel_event is not None:
                raise GameServiceError("已有游戏启动流程正在执行", "PLUGIN_LAUNCH_BUSY")
            if sum(run.origin == origin for run in self._plugin_game_runs.values()) >= 128:
                raise GameServiceError("本运行游戏记录已达上限", "PLUGIN_RATE_LIMITED")
            reservation = PluginLaunchReservation(Event())
            self._launch_cancel_event = reservation.cancel_event
            self._launch_origin = origin
            return reservation

    def release_plugin_launch(self, reservation: PluginLaunchReservation) -> bool:
        """
        幂等释放自身启动占位，不会清除后来用户或插件创建的启动流程。

        :param reservation: 当前任务原始占位身份
        :return: 原占位已不再持有流程
        """
        for handle in tuple(reversed(reservation.handles)):
            try:
                handle.close()
            except (OSError, FileBoundaryError):
                self.logger.exception("游戏启动目录句柄尚未关闭，保留启动占位等待清理")
            else:
                reservation.handles.remove(handle)
        if reservation.handles:
            return False
        with self._lock:
            if self._launch_cancel_event is reservation.cancel_event:
                self._launch_cancel_event = None
                self._launch_origin = None
        return True

    def plugin_launch(
        self,
        context: OperationContext,
        origin: OperationOrigin,
        target: GameResourceTarget,
        reservation: PluginLaunchReservation,
        verify: Callable[[], None],
        memory_mb: int,
        width: int,
        height: int,
        fullscreen: bool,
        *,
        java_path: Path | None = None,
    ) -> PluginGameRun:
        """
        在宿主原循环执行既有准备流程，工作线程等待实际执行结束而不提前报告取消。

        :param context: 唯一任务上下文
        :param origin: 可信插件来源
        :param target: 已批准的具体实例
        :param reservation: 准入时的全局启动占位
        :param verify: 进程创建前的原始授权及身份检查
        :param memory_mb: 有限内存选项
        :param width: 窗口宽度
        :param height: 窗口高度
        :param fullscreen: 是否全屏
        :param java_path: 宿主从本运行已获准引用解析的 Java，不从插件请求直接读取
        :return: 不含账户、路径或启动参数的实际进程结果
        :raises GameServiceError: 宿主、启动或执行取消失败
        """
        context.check_cancelled()
        verify()
        with self._lock:
            loop = self._plugin_launch_loop
        if loop is None or not loop.is_running():
            raise GameServiceError("游戏启动宿主尚未就绪", "PLUGIN_HOST_NOT_READY")
        assert target.version_id is not None
        root = FileDirectory.open(target.root_path)
        reservation.handles.append(root)
        versions = root.child("versions")
        reservation.handles.append(versions)
        instance = versions.child(target.version_id)
        reservation.handles.append(instance)
        verify()
        control = PluginLaunchControl(origin, context, reservation.cancel_event, verify)
        context.report_effect("partial")
        coroutine = self.launch_instance(
            {"version_id": target.version_id},
            game_path=target.root_path,
            source=self._game_download_source_provider(),
            memory=memory_mb,
            width=width,
            height=height,
            fullscreen=fullscreen,
            java_path=java_path,
            launcher_locale=self._game_locale_provider(),
            plugin_control=control,
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        return self._wait_plugin_launch(future, context, origin, reservation)

    def _wait_plugin_launch(
        self,
        future: Future[dict[str, str] | PluginGameRun],
        context: OperationContext,
        origin: OperationOrigin,
        reservation: PluginLaunchReservation,
    ) -> PluginGameRun:
        """
        将取消与超时传给实际执行方；绝不取消future后把等待结束当作线程已退出。
        """
        cancellation_error: Exception | None = None
        while True:
            try:
                result = future.result(timeout=0.2)
                if not isinstance(result, PluginGameRun):
                    raise GameServiceError("游戏启动结果无效", "PLUGIN_LAUNCH_FAILED")
                return result
            except FutureTimeoutError as exc:
                if future.done() and isinstance(future.exception(), TimeoutError):
                    raise GameServiceError("游戏启动执行超时", "PLUGIN_DEADLINE_EXCEEDED") from exc
                if cancellation_error is None:
                    try:
                        context.check_cancelled()
                    except Exception as exc:
                        # 许可和任务截止在此边界转换为取消；仍等待原协程及其工作线程真正结束。
                        cancellation_error = exc
                        reservation.cancel_event.set()
                if reservation.cancel_event.is_set():
                    with self._lock:
                        downloader = self._active_downloads.get("__launch__") if self._launch_origin == origin else None
                    if downloader is not None:
                        downloader.stop()
            except GameServiceError as exc:
                if cancellation_error is not None:
                    raise cancellation_error from exc
                if exc.error_code == "LAUNCH_CANCELLED":
                    raise GameServiceError("操作已取消", "OPERATION_CANCELLED") from exc
                raise

    def plugin_game_run(self, origin: OperationOrigin, token: str) -> PluginGameRun:
        """
        从游戏服务唯一运行记录读取本运行进程，包括已经观察到的退出状态。

        :param origin: 原始插件运行来源
        :param token: 宿主持有的确切游戏运行身份
        :return: 有限公开进程事实
        :raises GameServiceError: 不是该运行创建的进程
        """
        with self._lock:
            run = self._plugin_game_runs.get(token)
            if run is None or run.origin != origin or run.process is None:
                raise GameServiceError("游戏进程不属于当前插件运行", "PLUGIN_OWNER_MISMATCH")
            exit_code = run.process.poll()
            if exit_code is not None:
                run.exited = True
                run.exit_code = exit_code
            return PluginGameRun(token, run.version_id, run.process.pid, not run.exited, run.exit_code)

    def plugin_running(self, origin: OperationOrigin, target: GameResourceTarget) -> tuple[PluginGameRun, ...]:
        """
        只查询指定实例中本运行创建的进程，不暴露用户或其他插件的进程。

        :param origin: 受信运行来源
        :param target: 独立读取许可覆盖的具体实例
        :return: 当前及已结束的有限进程记录
        """
        with self._lock:
            tokens = tuple(
                token
                for token, run in self._plugin_game_runs.items()
                if run.origin == origin and run.game_path == target.root_path and run.version_id == target.version_id
            )
        return tuple(self.plugin_game_run(origin, token) for token in tokens)

    def plugin_stop(self, context: OperationContext, origin: OperationOrigin, token: str) -> PluginGameRun:
        """
        对确切进程对象执行有界停止，进入提交后以实际退出结果确认，不按实例名误停。

        :param context: 停止任务的独立原始准入
        :param origin: 受信运行来源
        :param token: 当前运行取得的游戏身份
        :return: 实际停止后的进程事实
        :raises GameServiceError: 来源不符、进程对象变化或停止超时
        """
        before = self.plugin_game_run(origin, token)
        with self._lock:
            run = self._plugin_game_runs[token]
            process = run.process
        assert process is not None

        def stop() -> PluginGameRun:
            if before.running:
                if not any(
                    item.get("ID") == run.instance_id and item.get("Instance") is process
                    for item in self.instances.get_instances_info()
                ):
                    raise OperationCommitRejectedError("游戏进程对象已变化", "PLUGIN_OWNER_MISMATCH")
                with self._lock:
                    run.stopping = True
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                except subprocess.TimeoutExpired as exc:
                    raise GameServiceError("游戏停止尚未确认", "PLUGIN_GAME_STOP_TIMEOUT") from exc
            return self.plugin_game_run(origin, token)

        context.check_cancelled()
        context.progress(0, "插件正在停止游戏", stage="stopping")
        result = context.finalize(stop)
        assert isinstance(result, PluginGameRun)
        return result

    def release_plugin_game_runs(self, origin: OperationOrigin) -> None:
        """
        运行结束时释放查询引用，不因撤销或插件退出杀掉已创建的游戏。

        :param origin: 已关闭的插件运行来源
        """
        with self._lock:
            self._plugin_game_runs = {
                token: run for token, run in self._plugin_game_runs.items() if run.origin != origin
            }
