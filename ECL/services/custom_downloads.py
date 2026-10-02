# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过公开 Core 下载器执行任意 HTTP(S) 文件下载及原子落盘。
#
# 公开接口：
#   - class CustomDownloadRequest — 校验下载地址、目录、命名方式、请求头与覆盖意图。
#   - class CustomDownloadService — 向应用任务注册表提交可取消、可重试下载。
#   - class DownloadPolicy — 统一跨平台文件名约束和响应名称解析。
# ============================================================

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from os import getenv
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ECL.game import Downloader
from ECL.services.operations import OperationContext, OperationManager
from ECL.utils.errors import GameServiceError


class CustomDownloadRequest(BaseModel):
    """
    保存经过边界校验的下载意图，默认禁止覆盖已有文件。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=8192)
    target_path: Path | None = None
    download_directory: Path | None = None
    naming_mode: Literal["original", "custom"] | None = None
    custom_name: str | None = Field(default=None, max_length=255)
    user_agent: str = Field(default="", max_length=4096, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
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
    def validate_target(cls, target_path: Path | None) -> Path | None:
        """
        拒绝相对路径及目录；完整目标位置由用户选择或输入。

        :param target_path: 文件目标路径
        :return: 规范化后的绝对路径
        :raises ValueError: 目标不是有效文件位置
        """
        if target_path is None:
            return None
        if not target_path.is_absolute() or not target_path.name or target_path.is_dir():
            raise ValueError("请选择完整的下载文件保存路径")
        return target_path.resolve(strict=False)

    @field_validator("download_directory")
    @classmethod
    def validate_directory(cls, directory_path: Path | None) -> Path | None:
        """
        校验用户指定的绝对下载目录，不在校验阶段创建文件夹。

        :param directory_path: 用户指定的目录或默认目录占位
        :return: 规范化目录
        :raises ValueError: 路径是相对路径、文件或含空字符
        """
        if directory_path is None:
            return None
        if "\0" in str(directory_path) or not directory_path.is_absolute() or directory_path.is_file():
            raise ValueError("请选择有效的绝对下载文件夹")
        return directory_path.resolve(strict=False)

    @model_validator(mode="after")
    def validate_options(self) -> CustomDownloadRequest:
        """
        拒绝目标歧义和不合法请求头，兼容原来的完整路径请求。

        :return: 已校验的请求
        :raises ValueError: 保存方式冲突、名称不合法或请求头格式错误
        """
        if self.target_path is not None and (
            self.download_directory is not None or self.naming_mode is not None or self.custom_name is not None
        ):
            raise ValueError("完整保存路径不能与下载文件夹及命名方式混用")
        if self.naming_mode == "custom":
            if not self.custom_name or not DownloadPolicy.is_valid_name(self.custom_name):
                raise ValueError("请输入有效的单个文件名，包含所需扩展名")
        elif self.custom_name is not None:
            raise ValueError("自定义文件名需要选择自定义命名方式")
        if any(ord(char) < 32 or ord(char) > 126 for char in self.user_agent):
            raise ValueError("UA 不能包含控制字符或无法发送的字符")
        if len(self.headers) > 64 or sum(len(key) + len(value) for key, value in self.headers.items()) > 32768:
            raise ValueError("请求头不能超过 64 项或 32 KiB")
        seen: set[str] = set()
        for key, value in self.headers.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                raise ValueError("请求头名称格式无效")
            if key.lower() == "user-agent":
                raise ValueError("请在 UA 字段中设置 User-Agent")
            if key.lower() in seen:
                raise ValueError("请求头名称不能重复（不区分大小写）")
            if any(ord(char) < 32 or ord(char) > 126 for char in value):
                raise ValueError("请求头值不能包含控制字符或无法发送的字符")
            seen.add(key.lower())
        return self


class DownloadPolicy:
    """
    统一下载文件名与默认请求设置，避免服务器名称越过用户选择的目录。
    """

    default_user_agent: str = "EuoraCraft-Launcher"
    invalid_name_characters: frozenset[str] = frozenset('<>:"/\\|?*')
    reserved_names: frozenset[str] = frozenset(
        {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        | {f"{prefix}{index}" for prefix in ("COM", "LPT") for index in (*range(1, 10), "¹", "²", "³")}
    )

    @classmethod
    def is_valid_name(cls, name: str) -> bool:
        """
        在 Windows 和 POSIX 上采用一致的单文件名约束。

        :param name: 要直接保存的文件名
        :return: 是否可跨平台作为单个文件名
        """
        return bool(
            name
            and len(name.encode("utf-8")) <= 255
            and name not in {".", ".."}
            and name == name.rstrip(" .")
            and name.split(".")[0].rstrip(" ").upper() not in cls.reserved_names
            and not any(char in cls.invalid_name_characters or ord(char) < 32 or ord(char) == 127 for char in name)
        )

    @classmethod
    def safe_server_name(cls, name: str) -> str:
        """
        将不可信服务器名字收敛为单个文件名，空名回退并处理设备保留名。

        :param name: 响应头或 URL 提供的名字
        :return: 适合当前选中目录的安全文件名
        """
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(
            "_" if char in cls.invalid_name_characters or ord(char) < 32 or ord(char) == 127 else char for char in name
        ).rstrip(" .")
        if not name:
            return "download.bin"
        if name.split(".")[0].rstrip(" ").upper() in cls.reserved_names:
            name = f"_{name}"
        while len(name.encode("utf-8")) > 255:
            name = name[:-1]
        return name.rstrip(" .") or "download.bin"

    @classmethod
    def response_name(cls, disposition: str, url: str) -> str:
        """
        优先 RFC 6266 扩展名称，再使用普通名称与重定向 URL 回退。

        :param disposition: Content-Disposition 响应头
        :param url: 最终响应 URL
        :return: 经过单文件名处理的名称
        """
        extended = re.search(r"(?:^|;)\s*filename\*\s*=\s*([^;]+)", disposition, re.IGNORECASE)
        if extended:
            try:
                charset, _language, encoded = extended.group(1).strip().strip('"').split("'", 2)
                if charset.lower() in {"utf-8", "iso-8859-1"}:
                    name = unquote(encoded, encoding=charset, errors="strict")
                    if name:
                        return cls.safe_server_name(name)
            except (ValueError, UnicodeError):
                # 错误的扩展编码不应挡住兼容的普通文件名。
                pass
        message = Message()
        message["Content-Disposition"] = disposition
        for key, value in message.get_params(header="content-disposition", failobj=[])[1:]:
            if key.lower() == "filename" and isinstance(value, str) and value:
                return cls.safe_server_name(value)
        return cls.safe_server_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))


@dataclass(frozen=True, slots=True)
class _DownloadIntent:
    request: CustomDownloadRequest
    headers: tuple[tuple[str, str], ...]
    target_path: Path | None


class CustomDownloadService:
    """
    复用应用任务注册表及 Core 下载器，临时目录保证失败不破坏已有文件。

    每个任务拥有独立异步循环；取消后停止下载器并关闭该循环的全部协程。
    """

    def __init__(
        self,
        operations: OperationManager,
        downloader_factory: Callable[..., Downloader] = Downloader,
        *,
        data_path: Path | None = None,
    ) -> None:
        """
        注入应用任务管理器与可替换的下载器边界。

        :param operations: 应用拥有的共享任务注册表
        :param downloader_factory: Core 公开下载器工厂
        :param data_path: 实际应用数据目录；旧完整路径调用可不提供
        """
        self._operations = operations
        self._downloader_factory = downloader_factory
        self._data_path = data_path
        self._requests: dict[str, _DownloadIntent] = {}
        self._resolved_paths: dict[str, Path] = {}
        self._active_paths: set[Path] = set()
        self._operation_by_path: dict[Path, str] = {}
        self._lock = RLock()

    def defaults(self) -> dict[str, str]:
        """
        返回实际数据目录下的初始下载文件夹，不创建文件系统对象。

        :return: 默认下载目录和默认 UA
        :raises GameServiceError: 未注入应用数据目录
        """
        if self._data_path is None:
            raise GameServiceError("下载数据目录未初始化", "DOWNLOAD_UNAVAILABLE")
        return {
            "downloadDirectory": str((self._data_path / "downloads").resolve(strict=False)),
            "userAgent": DownloadPolicy.default_user_agent,
        }

    def submit(self, request: CustomDownloadRequest) -> dict[str, str]:
        """
        注册下载任务，拒绝同时写入同一目标及未确认的覆盖。

        :param request: 经过校验的下载意图
        :return: 新任务标识和初始状态
        :raises GameServiceError: 文件已存在或目标正在被下载
        """
        request = request.model_copy(deep=True)
        if request.target_path is None and request.download_directory is None:
            request = request.model_copy(update={"download_directory": Path(self.defaults()["downloadDirectory"])})
        target_path = request.target_path
        if target_path is None and request.naming_mode == "custom":
            assert request.download_directory is not None and request.custom_name is not None
            target_path = request.download_directory / request.custom_name
        intent = _DownloadIntent(
            request,
            tuple({**request.headers, "User-Agent": request.user_agent or DownloadPolicy.default_user_agent}.items()),
            target_path,
        )
        with self._lock:
            if target_path is not None:
                self._reserve_path(target_path, request.overwrite)
            try:
                operation = self._operations.submit("custom_download", lambda context: self._download(context, intent))
            except Exception:
                if target_path is not None:
                    self._active_paths.discard(target_path)
                raise
            self._requests[operation["operationId"]] = intent
            if target_path is not None:
                self._operation_by_path[target_path] = operation["operationId"]
            return operation

    def retry(self, operation_id: str) -> dict[str, str]:
        """
        为本会话失败或取消的下载创建新任务，不复用原任务标识。

        :param operation_id: 原下载任务标识
        :return: 重试任务的标识
        :raises GameServiceError: 原任务不可重试
        """
        with self._lock:
            intent = self._requests.get(operation_id)
            status = self._operations.get(operation_id)["status"]
            if intent is None or status not in {"failed", "cancelled"}:
                raise GameServiceError("该任务不可重试", "DOWNLOAD_RETRY_UNAVAILABLE")
            request = intent.request
            target_path = self._resolved_paths.get(operation_id)
            if target_path is not None:
                request = request.model_copy(
                    update={
                        "target_path": target_path,
                        "download_directory": None,
                        "naming_mode": None,
                        "custom_name": None,
                    }
                )
            return self.submit(request)

    def _reserve_path(self, target_path: Path, should_overwrite: bool) -> None:
        """
        在服务锁内占用最终路径，防止自动命名与手动命名任务竞争。
        """
        if target_path in self._active_paths:
            operation_id = self._operation_by_path.get(target_path)
            if operation_id is None or self._operations.get(operation_id)["status"] in {"pending", "running"}:
                raise GameServiceError("该位置已有正在执行的下载", "DOWNLOAD_TARGET_BUSY")
            # 排队任务可能在进入工作函数前取消，此时由下一次占用回收其路径。
            self._active_paths.discard(target_path)
            self._operation_by_path.pop(target_path, None)
        if target_path.exists() and not should_overwrite:
            raise GameServiceError("文件已存在，请确认覆盖或选择其他位置", "DOWNLOAD_TARGET_EXISTS")
        self._active_paths.add(target_path)

    def _download(self, context: OperationContext, intent: _DownloadIntent) -> dict[str, str]:
        """
        在任务线程内下载至同盘临时目录，取消及失败均保留原目标。
        """
        target_path = intent.target_path
        has_reserved_path = target_path is not None
        request = intent.request
        try:
            context.check_cancelled()
            if target_path is None:
                context.progress(0, "正在确定文件名")
                name = asyncio.run(self._resolve_name(context, request.url, intent.headers))
                assert request.download_directory is not None
                target_path = (request.download_directory / name).resolve(strict=False)
                if target_path.parent != request.download_directory:
                    raise GameServiceError("下载目标超出所选文件夹", "DOWNLOAD_TARGET_INVALID")
                with self._lock:
                    self._resolved_paths[context.operation_id] = target_path
                    self._reserve_path(target_path, request.overwrite)
                    self._operation_by_path[target_path] = context.operation_id
                    has_reserved_path = True
            with self._lock:
                self._resolved_paths[context.operation_id] = target_path
            context.check_cancelled()
            context.progress(0, "正在下载文件", name=target_path.name, path=str(target_path))
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".ecl-download-", dir=target_path.parent) as staging_directory:
                staged_file = Path(staging_directory) / target_path.name
                asyncio.run(self._run_downloader(context, request.url, staged_file, intent.headers))
                context.check_cancelled()
                if request.overwrite:
                    context.finalize(lambda: staged_file.replace(target_path))
                elif sys.platform == "win32":
                    context.finalize(lambda: staged_file.rename(target_path))
                else:
                    # 硬链接提供原子“仅不存在时创建”，避免检查后覆盖其他程序新建的文件。
                    context.finalize(lambda: target_path.hardlink_to(staged_file))
                return {"path": str(target_path), "name": target_path.name}
        except OSError as exc:
            raise GameServiceError(
                "无法保存下载文件，请检查文件夹权限或文件是否被占用", "DOWNLOAD_WRITE_FAILED"
            ) from exc
        finally:
            if has_reserved_path and target_path is not None:
                with self._lock:
                    self._active_paths.discard(target_path)
                    self._operation_by_path.pop(target_path, None)

    async def _resolve_name(self, context: OperationContext, url: str, headers: tuple[tuple[str, str], ...]) -> str:
        """
        有限探测响应头，支持取消；HEAD 不支持时仅读取流式 GET 头部。
        """

        async def probe() -> str:
            async with httpx.AsyncClient(
                headers=dict(headers),
                timeout=10,
                follow_redirects=True,
                max_redirects=5,
                proxy=getenv("ECL_DOWNLOAD_PROXY") or None,
                trust_env=False,
            ) as client:
                response = await client.head(url)
                if response.status_code in {405, 501}:
                    async with client.stream("GET", url) as streamed:
                        streamed.raise_for_status()
                        return DownloadPolicy.response_name(
                            streamed.headers.get("Content-Disposition", ""), str(streamed.url)
                        )
                response.raise_for_status()
                return DownloadPolicy.response_name(response.headers.get("Content-Disposition", ""), str(response.url))

        task = asyncio.create_task(probe())
        try:
            async with asyncio.timeout(15):
                while not task.done():
                    context.check_cancelled()
                    await asyncio.wait({task}, timeout=0.1)
                return await task
        except (httpx.HTTPError, TimeoutError):
            # 名称元数据不可用时让正式下载报告网络错误，不泄露请求头内容。
            return DownloadPolicy.response_name("", url)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run_downloader(
        self, context: OperationContext, url: str, staged_file: Path, headers: tuple[tuple[str, str], ...]
    ) -> None:
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
        downloader.headers = dict(headers)
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
