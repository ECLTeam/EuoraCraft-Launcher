# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：协调版本安装与卸载，在工作线程统一选择加载器版本并报告安装进度。
#
# 公开接口：
#   - class InstallCoordinator
#       - install_version(body, game_path, source, java_path) -> dict[str, str] — 开始安装版本，返回任务 ID 和最终保存的版本名称。
#       - install_blocking(task_id, ..., report, cancel_event) -> None — 在无事件循环的工作线程中同步执行完整版本安装，供安装任务与整合包导入复用。
#       - uninstall_version(version_id, game_path) -> None — 从指定 Minecraft 目录卸载版本。
#       - reserve_install_target / release_install_target — 共享目标占位及实际目录句柄收尾。
# ============================================================

from __future__ import annotations

import asyncio
import functools
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from contextvars import copy_context
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
from anyio import to_thread
from packaging.version import InvalidVersion, Version

from ECL.game import GetGames, InstallEvent, InstallObserver
from ECL.utils.instance_reading import InstanceReadBoundary, directory_identity
from ECL.utils.windows_file_handles import FileBoundaryError

from .base import Downloader, GameServiceError, _GameState
from .install_progress import InstallationProgress, InstallationSnapshot
from .install_targets import InstallReservation
from .mod_sources import mod_api_base, mod_user_agent, rewrite_mod_file_url
from .workspace import resolve_relative_id

# install_blocking 的进度上报回调：签名与 _emit_install_progress 去掉 task_id 后一致。
InstallProgressReporter = Callable[..., None]


class InstallCoordinator(_GameState):
    """
    维护版本安装任务，在工作线程中解析加载器并执行清单生成与下载。

    普通安装和整合包基础安装共用版本选择及取消检查，界面受理阶段不查询网络。
    """

    fabric_api_project: str = "fabric-api"
    fabric_api_timeout_seconds: int = 10

    def reserve_install_target(self, game_path: Path, version_name: str) -> InstallReservation:
        """
        在准入时独占同一物理游戏根和实例名，所有安装入口共用同一张占位表。

        :param game_path: 已存在的游戏根目录
        :param version_name: 已校验实例名
        :return: 实际执行结束后必须释放的占位
        :raises GameServiceError: 相同目标仍被另一安装使用
        """
        root_path = self._normalize_game_path(game_path)
        root_identity = directory_identity(root_path)
        comparison_name = version_name.casefold() if sys.platform == "win32" else version_name
        reservation = InstallReservation(root_path, root_identity, version_name, comparison_name, uuid4().hex)
        key = (root_identity, comparison_name)
        with self._lock:
            if self._closing:
                raise GameServiceError("游戏服务正在关闭", "GAME_SERVICE_CLOSED")
            if key in self._install_reservations:
                raise GameServiceError("该实例正在安装，请等待安装结束", "INSTALL_ALREADY_RUNNING")
            self._install_reservations[key] = reservation
        return reservation

    def release_install_target(self, reservation: InstallReservation) -> bool:
        """
        仅释放自身占位，重复清理不会移除后来任务占用的相同目标。

        :param reservation: 原准入产生的占位
        :return: 该占位已不再持有目标
        """
        for handle in tuple(reversed(reservation.handles)):
            try:
                handle.close()
            except (OSError, FileBoundaryError):
                self.logger.exception("安装目录句柄尚未关闭，保留目标占位等待清理")
            else:
                reservation.handles.remove(handle)
        if reservation.handles:
            return False
        with self._lock:
            key = (reservation.root_identity, reservation.comparison_name)
            if self._install_reservations.get(key) == reservation:
                self._install_reservations.pop(key)
        return True

    def _check_install_target(self, reservation: InstallReservation, game_path: Path, version_name: str) -> None:
        """
        每次执行前检查目标、占位与物理根一致，拒绝把旧占位换绑到新目录。
        """
        with self._lock:
            current = self._install_reservations.get((reservation.root_identity, reservation.comparison_name))
        if current != reservation or reservation.root_path != game_path or reservation.version_name != version_name:
            raise GameServiceError("安装目标占位已经失效", "INSTALL_TARGET_INVALID")
        reader = InstanceReadBoundary(game_path, monotonic() + 5)
        if reader.identity != reservation.root_identity:
            raise GameServiceError("安装目标目录身份已经变化", "INSTALL_TARGET_INVALID")
        versions_path = game_path / "versions"
        if versions_path.exists() or versions_path.is_symlink():
            reader.directory(Path("versions"))
        instance_path = versions_path / version_name
        if instance_path.exists() or instance_path.is_symlink():
            reader.directory(Path("versions") / version_name)

    def _emit_install_progress(
        self,
        task_id: str,
        phase: str,
        message: str,
        *,
        done: int | None = None,
        total: int | None = None,
        progress_type: str | None = None,
        total_files: int | None = None,
        downloaded_files: int | None = None,
        speed: int | None = None,
        subtask: str | None = None,
        error_code: str | None = None,
        installation: InstallationSnapshot | None = None,
    ) -> None:
        if installation is not None:
            self.events.emit(
                "game:install_progress", {"kind": "installation", "task_id": task_id, "installation": installation}
            )
            return
        payload: dict[str, Any] = {
            "phase": phase,
            "task_id": task_id,
            "message": message,
        }
        if done is not None:
            payload["done"] = done
        if total is not None:
            payload["total"] = total
        if progress_type:
            payload["progress_type"] = progress_type
        if total_files is not None:
            payload["total_files"] = total_files
        if downloaded_files is not None:
            payload["downloaded_files"] = downloaded_files
        if speed is not None:
            payload["speed"] = speed
        if subtask:
            payload["subtask"] = subtask
        if error_code:
            payload["errorCode"] = error_code
        self.events.emit("game:install_progress", payload)

    def install_version(
        self,
        body: Mapping[str, object],
        *,
        game_path: Any,
        source: Any = "official",
        java_path: str | None = None,
    ) -> dict[str, str]:
        """
        开始安装版本，返回任务 ID 和最终保存的版本名称。

        :param body: 已经 API 校验的请求参数；加载器版本缺省或空白时自动选择最新版
        :param game_path: Minecraft 游戏根目录
        :param source: 下载源名称，如 ``official`` 或 ``bmclapi``
        :param java_path: Java 可执行文件路径
        :return: 已提交的任务标识、游戏版本与最终实例名称
        :raises GameServiceError: 请求参数、Java 配置无效或同一任务仍在运行时抛出
        """
        path = self._normalize_game_path(game_path)
        normalized_source = self._normalize_source(source)
        version_id = self._normalize_version_name(body.get("version_id"), "Minecraft 版本")
        save_name = self._normalize_version_name(body.get("version_name") or version_id)
        loader = str(body.get("loader_type") or "vanilla").strip().casefold()
        if loader in {"", "none"}:
            loader = "vanilla"
        if loader not in {"vanilla", "fabric", "forge", "neoforge", "quilt"}:
            raise GameServiceError(f"暂不支持安装加载器: {body.get('loader_type')}", "UNSUPPORTED_LOADER")

        self.logger.debug(
            "开始安装版本：版本：%s；加载器：%s；目录：%s；来源：%s；保存名称：%s",
            version_id,
            loader,
            path,
            normalized_source,
            save_name,
        )

        loader_version = None
        if loader != "vanilla":
            field_name = f"{loader}_version"
            loader_version = self._normalize_loader_version(body.get(field_name))
        if loader in {"forge", "neoforge"}:
            java_path = self._resolve_java_path(java_path)

        path.mkdir(parents=True, exist_ok=True)
        (path / "versions").mkdir(parents=True, exist_ok=True)
        requested_task_id = body.get("task_id")
        task_id = requested_task_id.strip() if isinstance(requested_task_id, str) else ""
        if not task_id:
            task_id = f"install-{uuid4().hex}"

        loop = asyncio.get_running_loop()
        reservation = self.reserve_install_target(path, save_name)
        with self._lock:
            existing = self._install_tasks.get(task_id)
            if existing is not None and not existing.done():
                self.release_install_target(reservation)
                raise GameServiceError("相同的安装任务已在运行", "INSTALL_ALREADY_RUNNING")
            coroutine = self._run_install(
                task_id,
                version_id,
                save_name,
                loader,
                loader_version,
                body.get("fabric_api_version"),
                path,
                normalized_source,
                java_path,
                reservation,
            )
            try:
                task = loop.create_task(coroutine, name=f"ECLInstall-{task_id}")
            except BaseException:
                coroutine.close()
                self.release_install_target(reservation)
                raise
            self._install_tasks[task_id] = task
            # 在协程首次运行前被取消时，协程内部的finally不会执行。
            task.add_done_callback(lambda finished: self._finish_install_task(task_id, reservation, finished))
        self.logger.info("开始安装 %s，保存为 %s，加载器: %s", version_id, save_name, loader)
        return {"taskId": task_id, "versionId": version_id, "versionName": save_name}

    def _finish_install_task(self, task_id: str, reservation: InstallReservation, task: asyncio.Task[None]) -> None:
        """
        任务真正结束后释放占位，包含首次运行前取消和正常工作线程退出。
        """
        with self._lock:
            if self._install_tasks.get(task_id) is task:
                self._install_tasks.pop(task_id)
        self.release_install_target(reservation)

    async def _run_install(
        self,
        task_id: str,
        version_id: str,
        save_name: str,
        loader: str,
        loader_version: str | None,
        fabric_api_version: Any,
        game_path: Path,
        source: str,
        java_path: str | None,
        reservation: InstallReservation,
    ) -> None:
        report = functools.partial(self._emit_install_progress, task_id)
        cancel_event = Event()
        progress_record = InstallationProgress(loader, True, report)
        worker = asyncio.create_task(
            to_thread.run_sync(
                functools.partial(
                    self.install_blocking,
                    task_id,
                    version_id,
                    save_name,
                    loader,
                    loader_version,
                    fabric_api_version,
                    game_path,
                    source,
                    java_path,
                    report,
                    cancel_event,
                    asyncio.get_running_loop(),
                    progress_record=progress_record,
                    reservation=reservation,
                )
            )
        )
        try:
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel_event.set()
                progress_record.request_cancel()
                with self._lock:
                    downloader = self._active_downloads.get(task_id)
                if downloader is not None:
                    downloader.stop()
                # 取消等待方不能提前结束安装；等待工作线程实际退出。
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                worker.result()
        except GameServiceError as exc:
            self.logger.info("版本安装结束；错误码：%s", exc.error_code)
        except Exception:
            self.logger.exception("版本安装执行失败")
        finally:
            with self._lock:
                self._active_downloads.pop(task_id, None)
                self._install_tasks.pop(task_id, None)
            self.release_install_target(reservation)

    @staticmethod
    def _normalize_loader_version(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise GameServiceError("加载器版本必须是字符串", "INVALID_LOADER_VERSION")
        return value.strip() or None

    def _resolve_install_loader_version(
        self,
        loader: str,
        version_id: str,
        loader_version: str | None,
        source: str,
        cancel_event: Event | None,
    ) -> str | None:
        """
        在安装工作线程中为缺省版本选择当前游戏兼容的最高加载器版本。

        手选版本直接保留，不依赖远端列表的顺序或稳定版分类。查询沿用扫描协调器的
        来源与网络策略；空列表、查询失败或无法比较的版本数据均明确失败。取消后不
        继续生成清单，避免远端查询返回时又开始写入实例文件。
        """
        self._ensure_install_not_cancelled(cancel_event)
        if loader == "vanilla":
            return None
        selected_version = self._normalize_loader_version(loader_version)
        if selected_version is not None:
            return selected_version
        try:
            versions = self.loader_versions(loader, version_id, source)
        except Exception as exc:
            self._ensure_install_not_cancelled(cancel_event)
            self.logger.exception("查询安装所需的加载器版本失败；加载器：%s；游戏版本：%s", loader, version_id)
            raise GameServiceError("查询加载器版本失败，请重试", "LOADER_VERSION_QUERY_FAILED") from exc
        self._ensure_install_not_cancelled(cancel_event)
        if not versions:
            raise GameServiceError(f"未找到 {loader} 在 {version_id} 下的可用版本", "LOADER_VERSION_NOT_FOUND")

        try:
            selected_version = max(
                versions, key=lambda candidate: self._install_loader_version_key(loader, version_id, candidate)
            )
        except InvalidVersion as exc:
            raise GameServiceError("加载器版本数据无法比较，请重试", "INVALID_LOADER_VERSION_DATA") from exc
        self.logger.info(
            "已选择加载器最新版；加载器：%s；游戏版本：%s；加载器版本：%s", loader, version_id, selected_version
        )
        return selected_version

    @staticmethod
    def _install_loader_version_key(loader: str, game_version: str, candidate: str) -> Version:
        """
        统一目录最新版本标记与安装默认选择，保留实际安装所需的原始版本串。
        """
        if loader == "neoforge" and game_version == "1.20.1":
            candidate = candidate.removeprefix("1.20.1-")
        return Version(candidate)

    @staticmethod
    def _ensure_install_not_cancelled(cancel_event: Event | None) -> None:
        # 工作线程不能随时中止，因此在每个安装阶段开始前检查取消请求。
        if cancel_event is not None and cancel_event.is_set():
            raise GameServiceError("安装已取消", "INSTALL_CANCELLED")

    def _build_fabric_install_download_list(
        self,
        games: GetGames,
        game_path: Path,
        version_id: str,
        save_name: str,
        loader_version: str | None,
        fabric_api_version: object,
        include_fabric_api: bool,
        cancel_event: Event | None,
        observer: InstallObserver,
    ) -> tuple[list[tuple[str, str]], frozenset[Path]]:
        """
        生成 Fabric 版本清单，并按实例有效隔离设置追加可选的 Fabric API。

        Core 先写入加载器元数据，随后才能判断新实例是否属于模组实例。
        整合包禁用附带 API 时不查询模组来源，也不添加额外文件。
        """
        download_list = games.build_fabric_download_list(version_id, loader_version, save_name, observer=observer)
        if not include_fabric_api:
            return download_list, frozenset()
        self._ensure_install_not_cancelled(cancel_event)
        observer.emit("resolve_fabric_api")
        api_url, filename = self._resolve_fabric_api(
            version_id, fabric_api_version if isinstance(fabric_api_version, str) else None
        )
        isolated = self.resolve_version_isolation(game_path, save_name)
        target = self.resolve_instance(game_path, save_name, isolated)
        api_path = resolve_relative_id(target.game_data_path / "mods", filename, must_exist=False)
        return [*download_list, (api_url, str(api_path))], frozenset({api_path.resolve(strict=False)})

    def install_blocking(
        self,
        task_id: str,
        version_id: str,
        save_name: str,
        loader: str,
        loader_version: str | None,
        fabric_api_version: Any,
        game_path: Path,
        source: str,
        java_path: str | None,
        report: InstallProgressReporter,
        cancel_event: Event | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        include_fabric_api: bool = True,
        *,
        progress_record: InstallationProgress | None = None,
        reservation: InstallReservation | None = None,
        check_callback: Callable[[], None] | None = None,
    ) -> None:
        """
        同步执行一次完整的版本安装：构建下载清单、并发下载、备用源补全。

        必须在没有运行中事件循环的线程中调用。下载协程默认经 ``_run_downloader_blocking``
        传入 ``loop`` 时，在已有事件循环中运行（与异步任务的执行方式相同），
        未传入时在工作线程内用独立事件循环执行。外部取消通过 ``cancel_event``
        在各安装阶段开始前和下载过程中生效。

        :param task_id: 安装任务标识，用于登记活跃下载器
        :param version_id: Minecraft 版本 ID
        :param save_name: 实际保存的版本目录名
        :param loader: 加载器类型（vanilla/fabric/forge/neoforge/quilt）
        :param loader_version: 加载器版本；缺省或空白时按版本号选择兼容的最新版
        :param fabric_api_version: 可选的 Fabric API 版本号
        :param game_path: Minecraft 游戏根目录
        :param source: 下载源名称（official/bmclapi）
        :param java_path: Java 可执行文件路径（forge/neoforge 安装所需）
        :param report: 进度上报回调，签名与 ``_emit_install_progress`` 去掉 task_id 一致
        :param cancel_event: 取消信号；为 None 时不检查取消
        :param loop: 可选的既有事件循环，下载协程将在其上执行
        :param progress_record: 异步入口持有的本次安装记录，用于在线程退出前报告取消请求
        :param include_fabric_api: fabric 安装是否附带 Fabric API；整合包导入的
            基础安装应传 False，避免与包内自带的 API 模组重复
        :param reservation: 已在准入时持有的目标占位；未提供时在本次执行范围内占位
        :param check_callback: 插件原始准入与物理身份检查，不用于构造新许可
        :raises GameServiceError: 加载器版本查询或数据错误、下载失败或安装取消时抛出
        """
        owns_reservation = reservation is None
        reservation = reservation or self.reserve_install_target(game_path, save_name)
        record = progress_record or InstallationProgress(loader, include_fabric_api, report)

        def check() -> None:
            self._ensure_install_not_cancelled(cancel_event)
            self._check_install_target(reservation, game_path, save_name)
            if check_callback is not None:
                check_callback()

        observer = InstallObserver(record.accept, check)
        try:
            observer.emit("resolve_metadata")
            loader_version = self._resolve_install_loader_version(
                loader, version_id, loader_version, source, cancel_event
            )
            observer.check_cancelled()
            games = self._context(game_path, source).games
            additional_paths: frozenset[Path] = frozenset()
            if loader == "vanilla":
                download_list = games.build_minecraft_download_list(version_id, save_name, observer=observer)
            elif loader == "fabric":
                download_list, additional_paths = self._build_fabric_install_download_list(
                    games,
                    game_path,
                    version_id,
                    save_name,
                    loader_version,
                    fabric_api_version,
                    include_fabric_api,
                    cancel_event,
                    observer,
                )
            elif loader == "quilt":
                download_list = games.build_quilt_download_list(
                    version_id, loader_version, save_name, observer=observer
                )
            elif loader == "forge":
                download_list = games.build_forge_download_list(
                    version_id, loader_version, java_path, save_name, observer=observer
                )
            else:
                download_list = games.build_neoforged_download_list(
                    version_id, loader_version, java_path, save_name, observer=observer
                )
            observer.check_cancelled()
            if download_list:
                self._download_install_files(
                    task_id, game_path, save_name, source, download_list, observer, cancel_event, loop, additional_paths
                )
            else:
                observer.emit("download_game_files", status="skipped")
            observer.emit("finalize_install")
            observer.check_cancelled()
            record.finish("completed", f"{save_name} 已安装完成")
            self.logger.info("版本安装完成；实例名称：%s", save_name)
        except GameServiceError as exc:
            record.finish("canceled" if exc.error_code == "INSTALL_CANCELLED" else "error", str(exc), exc.error_code)
            raise
        except Exception:
            record.finish("error", "安装失败，请查看错误详情", "VERSION_INSTALL_FAILED")
            raise
        finally:
            if owns_reservation:
                self.release_install_target(reservation)

    def _download_install_files(
        self,
        task_id: str,
        game_path: Path,
        save_name: str,
        source: str,
        download_list: list[tuple[str, str]],
        observer: InstallObserver,
        cancel_event: Event | None,
        loop: asyncio.AbstractEventLoop | None,
        additional_paths: frozenset[Path],
    ) -> None:
        """
        执行资源下载及同一步骤内的备用源补全，完成数按唯一目标路径计算。

        下载器实际退出后才解除取消监听；备用源沿用同一安装记录和取消信号。
        """
        observer.emit("download_game_files", action="preflight")
        downloader = self._downloader_factory(download_list, observer=observer)
        self._run_install_downloader(task_id, downloader, cancel_event, loop)
        observer.check_cancelled()
        failed = set(downloader.failed_entries)
        if failed:
            failed = self._verify_failed_install_files(game_path, save_name, source, failed, additional_paths, observer)
        if failed:
            entries = self._fallback_download_entries(
                game_path,
                save_name,
                source,
                failed,
                downloader.local_failed_paths,
            )
            if entries:
                all_paths = {Path(file_path).resolve(strict=False) for _, file_path in download_list}
                failed_paths = {Path(file_path).resolve(strict=False) for _, file_path in failed}
                completed_paths = all_paths - failed_paths
                transferred = downloader.bytes_downloaded_for_speed
                total_files = len(all_paths)

                def report_fallback(event: InstallEvent) -> None:
                    observer.callback(
                        InstallEvent(
                            stage="download_game_files",
                            status=event.status,
                            action="fallback" if event.action == "downloading" else event.action,
                            unit="files",
                            completed=len(completed_paths) + event.completed_files,
                            total=total_files,
                            completed_files=len(completed_paths) + event.completed_files,
                            total_files=total_files,
                            transferred_bytes=transferred + event.transferred_bytes,
                            speed=event.speed,
                        )
                    )

                fallback_observer = InstallObserver(report_fallback, observer.check_cancelled)
                fallback_observer.emit("download_game_files", action="fallback")
                fallback = self._downloader_factory(list(entries), observer=fallback_observer)
                self._run_install_downloader(task_id, fallback, cancel_event, loop)
                observer.check_cancelled()
                recovered = {file_path for _, file_path in fallback.completed_entries}
                failed = {(url, file_path) for url, file_path in failed if file_path not in recovered}
        if failed:
            failed = self._verify_failed_install_files(game_path, save_name, source, failed, additional_paths, observer)
        if failed:
            raise GameServiceError(f"有 {len(failed)} 个游戏文件下载失败，请重试", "GAME_DOWNLOAD_FAILED")
        observer.emit("download_game_files", status="completed")

    def _verify_failed_install_files(
        self,
        game_path: Path,
        save_name: str,
        source: str,
        failed_entries: set[tuple[str, str]],
        additional_paths: frozenset[Path],
        observer: InstallObserver,
    ) -> set[tuple[str, str]]:
        """
        按当前元数据校验失败文件，识别已被并发安装补齐的共享缓存。

        Core 校验负责哈希与版本规则；额外 Fabric API 不属于游戏清单，仍保留
        下载器确认的失败，避免仅凭目标文件存在误报成功。
        """
        observer.emit("download_game_files", action="checking")
        checker = self._context(game_path, source).files_checker
        checking_observer = InstallObserver(check_cancelled=observer.check_cancelled)
        missing = checker.check_files(game_path, save_name, observer=checking_observer)
        missing_paths = {Path(file_path).resolve(strict=False) for _, file_path in missing}
        observer.check_cancelled()
        return {
            (url, file_path)
            for url, file_path in failed_entries
            if Path(file_path).resolve(strict=False) in missing_paths | additional_paths
        }

    def _run_install_downloader(
        self,
        task_id: str,
        downloader: Downloader,
        cancel_event: Event | None,
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """
        登记当前下载器并在其实际退出时释放取消监听，首选源与备用源共用。
        """
        finished = Event()
        watcher: Thread | None = None
        with self._lock:
            self._active_downloads[task_id] = downloader
        if cancel_event is not None:
            watcher = Thread(
                target=copy_context().run,
                args=(self._watch_downloader_cancel, cancel_event, finished, downloader),
                daemon=True,
            )
            watcher.start()
        try:
            self._run_downloader_blocking(downloader, loop)
        finally:
            finished.set()
            with self._lock:
                self._active_downloads.pop(task_id, None)
            if watcher is not None:
                watcher.join(timeout=2)

    def _resolve_fabric_api(self, game_version: str, fabric_api_version: str | None) -> tuple[str, str]:
        """
        解析指定 Minecraft 版本下 Fabric API 的下载地址与文件名。

        Fabric API 属于 Modrinth 模组资源，跟随模组源请求，并在远端失败时切换到
        另一模组源重试；下载地址按当前模组源重写后才交给下载器。

        :param game_version: 目标 Minecraft 游戏版本
        :param fabric_api_version: 可选的 Fabric API 版本号；空值取最新版本
        :return: (下载地址, 文件名)
        :raises GameServiceError: 未找到可用版本或缺少可下载文件时抛出
        """
        params = {
            "game_versions": json.dumps([game_version]),
            "loaders": json.dumps(["fabric"]),
        }

        def fetch_versions(mod_source: str) -> list[Any]:
            response = httpx.get(
                f"{mod_api_base(mod_source, 'modrinth')}/project/{self.fabric_api_project}/version",
                params=params,
                headers={"User-Agent": mod_user_agent()},
                timeout=self.fabric_api_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []

        mod_source = self.mod_source()
        versions = self.mod_request(
            "Fabric API 版本",
            fetch_versions,
            is_valid=lambda payload: isinstance(payload, list),
        )
        if not versions:
            raise GameServiceError("未找到可用的 Fabric API 版本", "FABRIC_API_NOT_FOUND")
        if fabric_api_version:
            selected = next(
                (
                    item
                    for item in versions
                    if isinstance(item, dict) and item.get("version_number") == fabric_api_version
                ),
                None,
            )
            if selected is None:
                raise GameServiceError(f"未找到 Fabric API 版本: {fabric_api_version}", "FABRIC_API_VERSION_NOT_FOUND")
        else:
            selected = versions[0]
        files = selected.get("files") or []
        primary = next(
            (item for item in files if isinstance(item, dict) and item.get("primary")),
            files[0] if files else None,
        )
        if not isinstance(primary, dict) or not primary.get("url") or not primary.get("filename"):
            raise GameServiceError("Fabric API 缺少可下载文件", "FABRIC_API_FILE_MISSING")
        return rewrite_mod_file_url(str(primary["url"]), mod_source), str(primary["filename"])

    def uninstall_version(self, version_id: Any, game_path: Any) -> None:
        """
        从指定 Minecraft 目录卸载版本。

        :param version_id: Minecraft 版本或实例标识
        :param game_path: Minecraft 游戏根目录
        """
        name = self._normalize_version_name(version_id)
        root = self._normalize_game_path(game_path) / "versions"
        target = (root / name).resolve(strict=False)
        self.logger.debug("卸载实例：版本：%s；目录：%s", name, target)
        if target.parent != root.resolve(strict=False):
            raise GameServiceError("实例目录超出允许范围", "INVALID_VERSION_PATH")
        if not target.exists():
            raise GameServiceError("要卸载的实例不存在", "VERSION_NOT_FOUND")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise GameServiceError(f"卸载实例失败: {exc}", "VERSION_UNINSTALL_FAILED") from exc
