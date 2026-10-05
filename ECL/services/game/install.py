# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：安装协调器：版本安装/卸载流程编排。
#
# 公开接口：
#   - class InstallCoordinator
#       - install_version(body, game_path, source, java_path) -> dict[str, str] — 开始安装版本，返回任务 ID 和最终保存的版本名称。
#       - install_blocking(task_id, ..., report, cancel_event) -> None — 在无事件循环的工作线程中同步执行完整版本安装，供安装任务与整合包导入复用。
#       - uninstall_version(version_id, game_path) -> None — 从指定 Minecraft 目录卸载版本。
# ============================================================

from __future__ import annotations

import asyncio
import functools
import json
import shutil
from collections.abc import Callable, Mapping
from contextvars import copy_context
from pathlib import Path
from threading import Event, Thread
from typing import Any, TypedDict
from uuid import uuid4

import httpx
from anyio import to_thread

from .base import Downloader, GameServiceError, _GameState

# install_blocking 的进度上报回调：签名与 _emit_install_progress 去掉 task_id 后一致。
InstallProgressReporter = Callable[..., None]


class _InstallDownloadState(TypedDict):
    downloader: Downloader | None
    speed_bytes_per_second: int
    base_completed: int | None


class InstallCoordinator(_GameState):
    fabric_api_project = "fabric-api"
    fabric_api_timeout_seconds = 10

    def _retry_install_downloads_blocking(
        self,
        task_id: str,
        game_path: Path,
        save_name: str,
        source: str,
        failed_entries: set[tuple[str, str]],
        primary_downloader: Downloader,
        progress_state: _InstallDownloadState,
        report: InstallProgressReporter,
        emit_progress: Callable[[int | None], None],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> set[tuple[str, str]]:
        """
        只将首选源失败的文件交给备用源，保留已下载文件和任务标识。

        备用源没有不同地址时返回原始失败集合，由安装流程统一报告。
        必须在无事件循环的工作线程中调用（下载协程经 ``_run_downloader_blocking`` 驱动）。
        """
        if not failed_entries:
            return failed_entries
        fallback_entries = self._fallback_download_entries(
            game_path,
            save_name,
            source,
            failed_entries,
            getattr(primary_downloader, "local_failed_paths", set()),
        )
        if not fallback_entries:
            return failed_entries
        progress_state["base_completed"] = len(primary_downloader.completed_entries)
        report("download", "首选下载源失败，正在尝试备用源", subtask="download_files")
        fallback_downloader = self._downloader_factory(
            fallback_entries,
            progress_callback=lambda done, total: emit_progress(None),
            speed_callback=lambda speed_mib_per_second: emit_progress(int(speed_mib_per_second * 1024 * 1024)),
        )
        progress_state["downloader"] = fallback_downloader
        with self._lock:
            self._active_downloads[task_id] = fallback_downloader
        self._run_downloader_blocking(fallback_downloader, loop)
        fallback_failed_paths = {file_path for _, file_path in fallback_downloader.failed_entries}
        recovered_paths = {file_path for _, file_path in fallback_entries} - fallback_failed_paths
        return {(url, file_path) for url, file_path in failed_entries if file_path not in recovered_paths}

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
    ) -> None:
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

        :param body: 经过边界校验的 IPC 请求数据
        :param game_path: Minecraft 游戏根目录
        :param source: 下载源名称，如 ``official`` 或 ``bmclapi``
        :param java_path: Java 可执行文件路径
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
            value = body.get(field_name)
            if not isinstance(value, str) or not value.strip():
                if loader == "fabric":
                    # Fabric 未指定加载器版本时由 Core 解析最新版本
                    loader_version = None
                else:
                    raise GameServiceError("未选择加载器版本", "LOADER_VERSION_REQUIRED")
            else:
                loader_version = value.strip()
        if loader in {"forge", "neoforge"}:
            java_path = self._resolve_java_path(java_path)

        path.mkdir(parents=True, exist_ok=True)
        (path / "versions").mkdir(parents=True, exist_ok=True)
        requested_task_id = body.get("task_id")
        task_id = requested_task_id.strip() if isinstance(requested_task_id, str) else ""
        if not task_id:
            task_id = f"install-{uuid4().hex}"

        loop = asyncio.get_running_loop()
        with self._lock:
            existing = self._install_tasks.get(task_id)
            if existing is not None and not existing.done():
                raise GameServiceError("相同的安装任务已在运行", "INSTALL_ALREADY_RUNNING")
            task = loop.create_task(
                self._run_install(
                    task_id,
                    version_id,
                    save_name,
                    loader,
                    loader_version,
                    body.get("fabric_api_version"),
                    path,
                    normalized_source,
                    java_path,
                ),
                name=f"ECLInstall-{task_id}",
            )
            self._install_tasks[task_id] = task
        self.logger.info("开始安装 %s，保存为 %s，加载器: %s", version_id, save_name, loader)
        return {"taskId": task_id, "versionId": version_id, "versionName": save_name}

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
    ) -> None:
        self._emit_install_progress(task_id, "install", "正在读取实例信息", done=0, total=1)
        report = functools.partial(self._emit_install_progress, task_id)
        # 任务取消无法直接中断工作线程，用取消事件让 install_blocking 的
        # 守护线程在取消后立刻停止下载器。
        cancel_event = Event()
        try:
            await to_thread.run_sync(
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
                abandon_on_cancel=True,
            )
        except asyncio.CancelledError:
            self.logger.info("已请求取消安装，后台下载线程仍等待清理；实例名称：%s", save_name)
            cancel_event.set()
            with self._lock:
                downloader = self._active_downloads.get(task_id)
            if downloader is not None:
                try:
                    downloader.stop()
                except Exception:
                    self.logger.exception("停止已取消安装的下载器失败")
            self._emit_install_progress(task_id, "error", "安装已取消", error_code="INSTALL_CANCELLED")
        except GameServiceError as exc:
            self.logger.error("版本安装失败 [%s]: %s", exc.error_code, exc)
            self._emit_install_progress(
                task_id,
                "error",
                str(exc),
                done=0,
                total=1,
                error_code=exc.error_code,
            )
        except Exception as exc:
            self.logger.exception("Core 安装版本失败: %s", save_name)
            self._emit_install_progress(
                task_id,
                "error",
                f"安装 {save_name} 失败: {exc}",
                done=0,
                total=1,
                error_code="VERSION_INSTALL_FAILED",
            )
        finally:
            with self._lock:
                self._active_downloads.pop(task_id, None)
                self._install_tasks.pop(task_id, None)

    @staticmethod
    def _ensure_install_not_cancelled(cancel_event: Event | None) -> None:
        # 无任务取消机制的工作线程在阶段边界检查取消请求。
        if cancel_event is not None and cancel_event.is_set():
            raise GameServiceError("安装已取消", "INSTALL_CANCELLED")

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
    ) -> None:
        """
        同步执行一次完整的版本安装：构建下载清单、并发下载、备用源补全。

        必须在没有运行中事件循环的线程中调用。下载协程默认经 ``_run_downloader_blocking``
        调度：传入 ``loop`` 时在既有事件循环上执行（行为与异步任务路径一致），
        未传入时在工作线程内用独立事件循环执行。外部取消通过 ``cancel_event``
        在阶段边界与下载过程中生效。

        :param task_id: 安装任务标识，用于登记活跃下载器
        :param version_id: Minecraft 版本 ID
        :param save_name: 实际保存的版本目录名
        :param loader: 加载器类型（vanilla/fabric/forge/neoforge/quilt）
        :param loader_version: 加载器版本；fabric 传 None 时由 Core 解析最新版
        :param fabric_api_version: 可选的 Fabric API 版本号
        :param game_path: Minecraft 游戏根目录
        :param source: 下载源名称（official/bmclapi）
        :param java_path: Java 可执行文件路径（forge/neoforge 安装所需）
        :param report: 进度上报回调，签名与 ``_emit_install_progress`` 去掉 task_id 一致
        :param cancel_event: 取消信号；为 None 时不检查取消
        :param loop: 可选的既有事件循环，下载协程将在其上执行
        :param include_fabric_api: fabric 安装是否附带 Fabric API；整合包导入的
            基础安装应传 False，避免与包内自带的 API 模组重复
        :raises GameServiceError: 下载失败（GAME_DOWNLOAD_FAILED）或取消（INSTALL_CANCELLED）时抛出
        """
        games = self._context(game_path, source).games
        if loader == "vanilla":
            download_list = games.build_minecraft_download_list(version_id, save_name)
        elif loader == "fabric":
            fabric_api = (
                self._resolve_fabric_api(
                    version_id, fabric_api_version if isinstance(fabric_api_version, str) else None
                )
                if include_fabric_api
                else None
            )
            download_list = games.build_fabric_download_list(version_id, loader_version, save_name, fabric_api)
        elif loader == "quilt":
            download_list = games.build_quilt_download_list(version_id, loader_version, save_name)
        elif loader == "forge":
            download_list = games.build_forge_download_list(version_id, loader_version, java_path, save_name)
        else:
            download_list = games.build_neoforged_download_list(version_id, loader_version, java_path, save_name)

        if not download_list:
            report("done", f"{save_name} 已安装完成", done=1, total=1)
            return

        self._ensure_install_not_cancelled(cancel_event)

        # 进度事件闭包：同时上报字节/文件进度、文件计数与实时速度。
        # 通过可变容器持有 downloader 引用，避免闭包在赋值前被调用。
        progress_state: _InstallDownloadState = {
            "downloader": None,
            "speed_bytes_per_second": 0,
            "base_completed": None,
        }

        def _emit_download_progress(speed_bytes_per_second: int | None = None) -> None:
            if speed_bytes_per_second is not None:
                progress_state["speed_bytes_per_second"] = speed_bytes_per_second
            downloader = progress_state["downloader"]
            assert downloader is not None, "下载器开始执行后才允许上报进度"
            base_completed = progress_state["base_completed"]
            is_fallback = base_completed is not None
            report(
                "download",
                f"正在下载 {save_name}",
                done=base_completed + len(downloader.completed_entries) if is_fallback else downloader.downloaded_bytes,
                total=len(download_list) if is_fallback else downloader.total_bytes,
                progress_type="files" if is_fallback or not downloader.use_byte_progress else "bytes",
                total_files=len(download_list),
                downloaded_files=(base_completed or 0) + len(downloader.completed_entries),
                speed=progress_state["speed_bytes_per_second"],
                subtask="download_files",
            )

        downloader = self._downloader_factory(
            download_list,
            progress_callback=lambda done, total: _emit_download_progress(),
            speed_callback=lambda speed_mib_per_second: _emit_download_progress(
                int(speed_mib_per_second * 1024 * 1024)
            ),
        )
        progress_state["downloader"] = downloader
        with self._lock:
            self._active_downloads[task_id] = downloader

        report(
            "download",
            f"准备下载 {len(download_list)} 个文件",
            done=0,
            total=len(download_list),
            progress_type="files",
            total_files=len(download_list),
            downloaded_files=0,
            subtask="download_files",
        )
        finished = Event()
        watcher: Thread | None = None
        if cancel_event is not None:
            watcher = Thread(
                target=copy_context().run,
                args=(self._watch_downloader_cancel, cancel_event, finished, downloader),
                name=f"ECLInstallCancel-{task_id}",
                daemon=True,
            )
            watcher.start()
        try:
            # 下载协程的执行循环由 _run_downloader_blocking 决定；
            # 取消请求由守护线程调用 Downloader.stop() 间接生效。
            self._run_downloader_blocking(downloader, loop)
        finally:
            finished.set()
            with self._lock:
                if self._active_downloads.get(task_id) is downloader:
                    self._active_downloads.pop(task_id, None)
            if watcher is not None:
                watcher.join(timeout=2)
        self._ensure_install_not_cancelled(cancel_event)
        failed_entries = set(downloader.failed_entries)
        failed_entries = self._retry_install_downloads_blocking(
            task_id,
            game_path,
            save_name,
            source,
            failed_entries,
            downloader,
            progress_state,
            report,
            _emit_download_progress,
            loop,
        )
        if failed_entries:
            failed_url, failed_path = next(iter(failed_entries))
            raise GameServiceError(
                f"有 {len(failed_entries)} 个文件下载失败，例如 {failed_path}（{failed_url}）",
                "GAME_DOWNLOAD_FAILED",
            )
        self.logger.info("版本安装完成: %s", save_name)
        report("done", f"{save_name} 已安装完成", done=1, total=1)

    @classmethod
    def _resolve_fabric_api(cls, game_version: str, fabric_api_version: str | None) -> tuple[str, str]:
        # 解析指定 Minecraft 版本下 Fabric API 的下载地址与文件名。
        params = {
            "game_versions": json.dumps([game_version]),
            "loaders": json.dumps(["fabric"]),
        }
        response = httpx.get(
            f"https://api.modrinth.com/v2/project/{cls.fabric_api_project}/version",
            params=params,
            headers={"User-Agent": "EuoraCraft-Launcher/version-install"},
            timeout=cls.fabric_api_timeout_seconds,
        )
        response.raise_for_status()
        versions = response.json()
        if not isinstance(versions, list) or not versions:
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
        return primary["url"], primary["filename"]

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
