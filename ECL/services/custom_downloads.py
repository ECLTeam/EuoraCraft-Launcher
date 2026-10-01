# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过公开 Core 下载器执行任意 HTTP(S) 文件下载及原子落盘。
#
# 公开接口：
#   - class CustomDownloadRequest — 校验下载地址、完整目标路径和覆盖意图。
#   - class CustomDownloadService — 向应用任务注册表提交可取消、可重试下载。
# ============================================================

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ECL.game import Downloader
from ECL.services.operations import OperationContext, OperationManager
from ECL.utils.errors import GameServiceError


class CustomDownloadRequest(BaseModel):
    """
    保存经过边界校验的下载意图，默认禁止覆盖已有文件。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=8192)
    target_path: Path
    overwrite: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """
        仅接受无内嵌凭据的 HTTP(S) 地址，不猜测或修改下载文件名。

        :param value: 用户下载地址
        :return: 去除首尾空白的地址
        :raises ValueError: 地址格式不受支持
        """
        value = value.strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("请输入有效的 HTTP(S) 下载地址")
        _ = parsed.port
        return value

    @field_validator("target_path")
    @classmethod
    def validate_target(cls, target_path: Path) -> Path:
        """
        拒绝相对路径及目录；完整目标位置由用户选择或输入。

        :param target_path: 文件目标路径
        :return: 规范化后的绝对路径
        :raises ValueError: 目标不是有效文件位置
        """
        if not target_path.is_absolute() or not target_path.name or target_path.is_dir():
            raise ValueError("请选择完整的下载文件保存路径")
        return target_path.resolve(strict=False)


class CustomDownloadService:
    """
    复用应用任务注册表及 Core 下载器，临时目录保证失败不破坏已有文件。

    每个任务拥有独立异步循环；取消后停止下载器并关闭该循环的全部协程。
    """

    def __init__(
        self,
        operations: OperationManager,
        downloader_factory: Callable[..., Downloader] = Downloader,
    ) -> None:
        """
        注入应用任务管理器与可替换的下载器边界。

        :param operations: 应用拥有的共享任务注册表
        :param downloader_factory: Core 公开下载器工厂
        """
        self._operations = operations
        self._downloader_factory = downloader_factory
        self._requests: dict[str, CustomDownloadRequest] = {}
        self._active_paths: set[Path] = set()
        self._lock = RLock()

    def submit(self, request: CustomDownloadRequest) -> dict[str, str]:
        """
        注册下载任务，拒绝同时写入同一目标及未确认的覆盖。

        :param request: 经过校验的下载意图
        :return: 新任务标识和初始状态
        :raises GameServiceError: 文件已存在或目标正在被下载
        """
        target_path = request.target_path
        with self._lock:
            if target_path in self._active_paths:
                raise GameServiceError("该位置已有正在执行的下载", "DOWNLOAD_TARGET_BUSY")
            if target_path.exists() and not request.overwrite:
                raise GameServiceError("文件已存在，请确认覆盖或选择其他位置", "DOWNLOAD_TARGET_EXISTS")
            self._active_paths.add(target_path)
            try:
                operation = self._operations.submit("custom_download", lambda context: self._download(context, request))
            except Exception:
                self._active_paths.discard(target_path)
                raise
            self._requests[operation["operationId"]] = request
            return operation

    def retry(self, operation_id: str) -> dict[str, str]:
        """
        为本会话失败或取消的下载创建新任务，不复用原任务标识。

        :param operation_id: 原下载任务标识
        :return: 重试任务的标识
        :raises GameServiceError: 原任务不可重试
        """
        with self._lock:
            request = self._requests.get(operation_id)
            status = self._operations.get(operation_id)["status"]
            if request is None or status not in {"failed", "cancelled"}:
                raise GameServiceError("该任务不可重试", "DOWNLOAD_RETRY_UNAVAILABLE")
            return self.submit(request)

    def _download(self, context: OperationContext, request: CustomDownloadRequest) -> dict[str, str]:
        """
        在任务线程内下载至同盘临时目录，取消及失败均保留原目标。
        """
        try:
            context.check_cancelled()
            request.target_path.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".ecl-download-", dir=request.target_path.parent) as staging_directory:
                staged_file = Path(staging_directory) / request.target_path.name
                asyncio.run(self._run_downloader(context, request.url, staged_file))
                context.check_cancelled()
                if request.overwrite:
                    context.finalize(lambda: staged_file.replace(request.target_path))
                elif sys.platform == "win32":
                    context.finalize(lambda: staged_file.rename(request.target_path))
                else:
                    # 硬链接提供原子“仅不存在时创建”，避免检查后覆盖其他程序新建的文件。
                    context.finalize(lambda: request.target_path.hardlink_to(staged_file))
                return {"path": str(request.target_path), "name": request.target_path.name}
        finally:
            with self._lock:
                self._active_paths.discard(request.target_path)

    async def _run_downloader(self, context: OperationContext, url: str, staged_file: Path) -> None:
        """
        监督 Core 下载循环，将取消传至下载器并收回辅助协程和客户端。
        """
        speed = 0.0
        downloader: Downloader

        def on_speed(value: float) -> None:
            nonlocal speed
            speed = value * 1024 * 1024

        def on_progress(done: int, total: int) -> None:
            context.progress(
                min(99, done * 100 / total) if total > 0 else 0,
                "正在下载文件",
                done=done,
                total=total,
                speed=speed,
                progressType="bytes" if downloader.use_byte_progress else "files",
                name=staged_file.name,
            )

        downloader = self._downloader_factory(
            [(url, staged_file)], progress_callback=on_progress, speed_callback=on_speed, max_rounds=3
        )
        task = asyncio.create_task(downloader.run())
        try:
            while not task.done():
                context.check_cancelled()
                await asyncio.wait({task}, timeout=0.1)
            await task
            if downloader.failed_entries or not staged_file.is_file():
                raise GameServiceError("文件下载失败，请检查地址或网络后重试", "CUSTOM_DOWNLOAD_FAILED")
        finally:
            downloader.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # stop 的清理协程由 Core 调度到当前循环，给它一次执行机会再退出。
            await asyncio.sleep(0)
            if downloader.client is not None and not downloader.client.is_closed:
                await downloader.client.aclose()
