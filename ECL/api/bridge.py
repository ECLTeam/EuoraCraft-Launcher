# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：前端桥接辅助：远程图片抓取、运行时选项校验等跨领域公共逻辑。
#
# 公开接口：
#   - guard_ipc_handler(state, operation, handler, timeout=…) -> Any — 为正式 IPC 命令补齐统一的异常边界与严重错误呈现元数据。
# ============================================================

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import os
import subprocess
import sys
from collections import deque
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
from anyio import to_thread
from pydantic import ValidationError
from pytauri import EventTarget
from pytauri.ffi import Emitter as _Emitter
from pytauri.ipc import WebviewWindow

from ECL.api.contracts import ApiResponse, failure
from ECL.api.models import (
    DebugProcessSpawnRequest,
    FrontendLogRequest,
    ProcessInputRequest,
    ProcessStopRequest,
)
from ECL.api.operation_logging import IpcLogPolicy, IpcLogRuntime, IpcLogSpec
from ECL.application import ApplicationContext
from ECL.cli import LaunchOptions, apply_launch_overrides
from ECL.game import AuthException, NetException
from ECL.services.accounts import AccountError
from ECL.services.game import GameServiceError
from ECL.services.game.launch_target import candidate_roots, resolve_launch_target
from ECL.services.maintenance import DebugMaintenanceError
from ECL.services.wardrobe import WardrobeError
from ECL.utils import atomic_write_text, get_logger
from ECL.utils.config import default_config
from ECL.utils.logging import get_frontend_log_history
from ECL.utils.network import download_proxy_url
from ECL.utils.operation_logging import operation_scope


class ImagePolicy:
    """
    远程图片下载、编码和缓存使用的共享限制与 MIME 映射。

    文件处理器与桥接辅助函数共用本策略，避免跨模块复制配置值。
    """

    mime_by_extension = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
    }
    extension_by_mime = {mime: ext for ext, mime in mime_by_extension.items()}
    extension_by_mime["image/jpeg"] = ".jpg"
    max_remote_image_bytes = 50 * 1024 * 1024
    remote_image_timeout_seconds = 15.0
    remote_image_chunk_bytes = 64 * 1024
    read_cache_max_items = 32


@functools.lru_cache(maxsize=ImagePolicy.read_cache_max_items)
def _read_image_data_url(file_path: Path, mtime_ns: int, size: int) -> tuple[str, str, int]:
    # 读取图片并编码为 Data URL（LRU 缓存），返回 Data URL、MIME 与 Base64 长度。
    ext = file_path.suffix.lower() or ".png"
    data_url, b64 = _encode_image_bytes(file_path.read_bytes(), ext)
    return data_url, ImagePolicy.mime_by_extension.get(ext, "image/jpeg"), len(b64)


def _is_http_url(url: str, *, scheme_lower: bool = False) -> bool:
    # 校验 URL 是否为带主机的 HTTP(S) 地址。
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    scheme = parsed.scheme.lower() if scheme_lower else parsed.scheme
    return scheme in ("http", "https") and bool(parsed.hostname)


def _normalize_image_url(value: Any) -> str | None:
    # 规整远程图片地址，校验 HTTP(S) 协议与主机并去除首尾标点。
    if not isinstance(value, str):
        return None
    url = value.strip().rstrip(",.;:\n\r")
    if not url:
        return None
    return url if _is_http_url(url, scheme_lower=True) else None


def _extract_filename_from_header(header: str | None) -> str | None:
    # 从 Content-Disposition 响应头解析文件名，兼容 filename* 与 filename 两种格式。
    if not header:
        return None
    try:
        if "filename*=" in header:
            part = header.split("filename*=")[1].split(";")[0].strip().strip('"')
            if "''" in part:
                encoding, _, name = part.partition("''")
                return unquote(name, encoding=encoding or "utf-8")
        if "filename=" in header:
            part = header.split("filename=")[1].split(";")[0].strip().strip('"')
            return unquote(part)
    except (ValueError, LookupError):
        pass
    return None


def _guess_image_extension(response: httpx.Response, url: str) -> str:
    # 按响应头文件名、Content-Type 与 URL 后缀推断图片扩展名，未知时回退为 .jpg。
    filename = _extract_filename_from_header(response.headers.get("content-disposition"))
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in ImagePolicy.mime_by_extension:
            return ext

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type in ImagePolicy.extension_by_mime:
        return ImagePolicy.extension_by_mime[content_type]

    parsed = urlsplit(str(response.url))
    ext = Path(unquote(parsed.path)).suffix.lower()
    if ext in ImagePolicy.mime_by_extension:
        return ext

    return ".jpg"


async def _download_remote_image(url: str) -> tuple[bytes, httpx.Response]:
    # 使用独立客户端流式下载远程图片，并限制响应大小；图片属于下载通道，走游戏下载代理。
    async with (
        httpx.AsyncClient(
            follow_redirects=True,
            timeout=ImagePolicy.remote_image_timeout_seconds,
            headers={"User-Agent": "EuoraCraft-Launcher"},
            proxy=download_proxy_url(),
        ) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes(ImagePolicy.remote_image_chunk_bytes):
            data.extend(chunk)
            if len(data) > ImagePolicy.max_remote_image_bytes:
                raise ValueError("远程图片超过最大大小限制")
        return bytes(data), response


def _encode_image_bytes(
    image_bytes: bytes,
    ext: str,
) -> tuple[str, str]:
    # 将图片字节按扩展名编码为 Data URL 与 Base64 数据。
    mime = ImagePolicy.mime_by_extension.get(ext, "image/jpeg")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}", b64


def _open_folder(path: str) -> None:
    # 使用操作系统默认方式在文件管理器中打开指定路径。
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"路径不存在: {target}")
    if sys.platform == "win32":
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)
    else:
        subprocess.run(["xdg-open", str(target)], check=False)


def _make_error_response(exc: Exception, fallback_code: str, events: Any | None = None) -> ApiResponse:
    # 根据异常与错误码构造失败响应，严重错误附带弹窗元数据并上报事件。
    error_code = getattr(exc, "error_code", fallback_code)
    raw_message = str(exc).strip() or type(exc).__name__
    is_unexpected_file_error = isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError)
    if error_code in _FrontendState.modal_error_codes or is_unexpected_file_error:
        error_id = uuid4().hex
        title = "启动器无法完成本地数据操作"
        message = (
            _FrontendState.file_error_message
            if is_unexpected_file_error
            else _FrontendState.modal_error_messages.get(error_code, _FrontendState.file_error_message)
        )
        payload = {"error_id": error_id, "title": title, "message": message, "detail": raw_message}
        if events is not None:
            events.emit("launcher:error", payload)
        return failure(message, error_code, presentation="modal", error_id=error_id, title=title, detail=raw_message)
    return failure(raw_message, error_code)


def _make_unexpected_error_response(state: Any, operation: str, exc: Exception) -> ApiResponse:
    # 为未预期异常构造严重错误响应，记录堆栈并上报错误事件。
    error_id = uuid4().hex
    message = "启动器执行操作时发生内部错误，请导出日志以便排查"
    title = "启动器发生内部错误"
    raw_message = str(exc).strip() or type(exc).__name__
    if _should_report_failure(state, operation, "INTERNAL_ERROR"):
        state.logger.exception("%s发生内部异常；错误编号：%s", _log_spec(operation).title, error_id)
    state.events.emit(
        "launcher:error", {"error_id": error_id, "title": title, "message": message, "detail": raw_message}
    )
    return failure(
        message,
        "INTERNAL_ERROR",
        presentation="modal",
        error_id=error_id,
        title=title,
        detail=raw_message,
    )


def _make_timeout_response(state: Any, operation: str) -> ApiResponse:
    # 为操作超时构造失败响应，提示用户检查网络。
    if _should_report_failure(state, operation, "OPERATION_TIMEOUT"):
        state.logger.warning("%s请求超时，已请求取消；后台执行是否停止由执行方确认", _log_spec(operation).title)
    return failure("操作超时，请检查网络后重试", "OPERATION_TIMEOUT")


async def _guarded_call(
    state: Any, operation: str, fallback_code: str, awaitable: Any, timeout: float | None = None
) -> Any:
    # 统一的 IPC 异常边界：捕获已知错误与未知异常并转换为响应。
    try:
        if timeout is not None:
            return await asyncio.wait_for(awaitable, timeout)
        return await awaitable
    except TimeoutError:
        return _make_timeout_response(state, operation)
    except _FrontendState.ipc_errors as exc:
        if _should_report_failure(state, operation, getattr(exc, "error_code", fallback_code)):
            if isinstance(exc, ValidationError):
                state.logger.warning("%s请求未通过结构检查；错误码：%s", _log_spec(operation).title, fallback_code)
            elif isinstance(exc, httpx.HTTPError):
                state.logger.warning("%s远程请求失败：%s", _log_spec(operation).title, exc)
            elif isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError):
                state.logger.exception("%s执行失败", _log_spec(operation).title)
            else:
                state.logger.warning("%s执行失败：%s", _log_spec(operation).title, exc)
        return _make_error_response(exc, fallback_code, state.events)
    except Exception as exc:
        return _make_unexpected_error_response(state, operation, exc)


def _log_spec(operation: str) -> IpcLogSpec:
    return IpcLogPolicy.commands.get(operation, IpcLogSpec("未分类操作", "query"))


def _should_report_failure(state: Any, operation: str, code: str) -> bool:
    spec = _log_spec(operation)
    if spec.kind == "channel":
        return False
    runtime = getattr(state, "_ipc_log_runtime", None)
    return spec.kind != "poll" or runtime is None or runtime.record_failure(operation, code)


def guard_ipc_handler(state: Any, operation: str, handler: Any, timeout: float | None = None) -> Any:
    """
    为正式 IPC 命令记录操作生命周期并补齐统一异常边界。

    日志通道不记录自身调用，查询使用 DEBUG，轮询仅记录限频失败和恢复。
    后台任务返回时只记录提交，不宣称执行完成；不输出请求体和配置值。

    :param state: 拥有日志与应用事件总线的前端 API 门面
    :param operation: 注册到 PyTauri 的稳定命令名
    :param handler: 原始异步命令处理器
    :param timeout: 可选的请求超时秒数；取消等待不代表后台线程已停止
    :return: 捕获所有异常并返回 ``ApiResponse`` 的异步处理器
    """

    if getattr(state, "_ipc_log_runtime", None) is None:
        state._ipc_log_runtime = IpcLogRuntime()
    runtime = state._ipc_log_runtime
    spec = _log_spec(operation)

    @functools.wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> ApiResponse:
        if spec.kind == "channel":
            return await _guarded_call(state, operation, "INTERNAL_ERROR", handler(*args, **kwargs), timeout)
        level = logging.INFO if spec.kind == "action" else logging.DEBUG
        with operation_scope(spec.title) as trace:
            if spec.kind != "poll":
                trace.log(state.logger, f"开始{spec.title}", level)
            try:
                response = await _guarded_call(state, operation, "INTERNAL_ERROR", handler(*args, **kwargs), timeout)
            except asyncio.CancelledError:
                trace.log(state.logger, f"{spec.title}请求已取消；后台任务仍以执行方状态为准", logging.WARNING)
                raise
            if isinstance(response, dict) and response.get("success") is False:
                code = str(response.get("errorCode", "INTERNAL_ERROR"))
                if spec.kind != "poll" or runtime.record_failure(operation, code):
                    trace.log(state.logger, f"{spec.title}失败；错误码：{code}", logging.WARNING)
            elif spec.kind == "poll":
                if runtime.record_recovery(operation):
                    trace.log(state.logger, f"{spec.title}已恢复", logging.INFO)
            else:
                data = response.get("data") if isinstance(response, dict) else None
                task_id = (
                    (data.get("operationId") or data.get("taskId") or data.get("task_id"))
                    if isinstance(data, dict)
                    else None
                )
                message = f"{spec.title}任务已提交" if task_id else f"完成{spec.title}"
                if (
                    not task_id
                    and isinstance(data, dict)
                    and isinstance(data.get("status"), str)
                    and data.get("status") in {"pending", "running", "downloading"}
                ):
                    message = f"{spec.title}请求已受理，等待执行结果"
                trace.log(state.logger, message, level)
            return response

    return guarded


def _ipc_handler(fallback_code: str = "INTERNAL_ERROR", timeout: float | None = None):
    # 装饰器，为 IPC 命令处理器补齐统一异常边界与错误呈现元数据。
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            return await _guarded_call(self, func.__name__, fallback_code, func(self, *args, **kwargs), timeout)

        return wrapper

    return decorator


def _validate_body(model: Any, body: dict[str, Any]) -> tuple[Any, ApiResponse | None]:
    # 将 IPC 请求体校验为 Pydantic 模型，失败时返回稳定的校验错误响应。
    try:
        return model.model_validate(body), None
    except ValidationError as exc:
        return None, _FrontendState._invalid_request(exc)


class _FrontendState:
    """
    前端 IPC 处理器的共享状态门面，聚合日志、应用事件与各服务句柄。
    """

    queued_frontend_events = frozenset(
        {
            "launcher:error",
            "launcher:popup",
            "launcher:log",
            "game:instances_changed",
            "process:instance_log",
            "process:instances_changed",
        }
    )
    max_pending_frontend_events = 50
    runtime_option_fields = (
        ("memory", "memory", lambda c: c.get("memory_size", default_config["game"]["memory_size"])),
        ("width", "width", lambda c: c.get("game_width", default_config["game"]["game_width"])),
        ("height", "height", lambda c: c.get("game_height", default_config["game"]["game_height"])),
        ("fullscreen", "fullscreen", lambda c: bool(c.get("fullscreen", default_config["game"]["fullscreen"]))),
        ("jvm_args", "jvm_args", lambda c: c.get("jvm_args", [])),
    )
    ipc_errors = (
        AccountError,
        WardrobeError,
        GameServiceError,
        DebugMaintenanceError,
        AuthException,
        NetException,
        httpx.HTTPError,
        OSError,
        ValueError,
    )
    modal_error_codes = frozenset(
        {
            "ACCOUNT_SAVE_FAILED",
            "ECL_CONFIG_WRITE_FAILED",
            "MOD_COPY_FAILED",
            "VERSION_UNINSTALL_FAILED",
            "WARDROBE_FILE_READ_FAILED",
            "WARDROBE_METADATA_INVALID",
        }
    )
    modal_error_messages = {
        "ACCOUNT_SAVE_FAILED": "账号数据保存失败，请重试。若问题持续，请导出日志以便排查。",
        "ECL_CONFIG_WRITE_FAILED": "配置保存失败，请重试。若问题持续，请导出日志以便排查。",
        "MOD_COPY_FAILED": "模组文件复制失败，请重试。若问题持续，请导出日志以便排查。",
        "VERSION_UNINSTALL_FAILED": "版本卸载失败，请检查文件是否被占用后再重试。",
        "WARDROBE_FILE_READ_FAILED": "衣柜数据读取失败，请重试。若问题持续，请导出日志以便排查。",
        "WARDROBE_METADATA_INVALID": "衣柜数据格式异常，请重试。若问题持续，请导出日志以便排查。",
    }
    file_error_message = "启动器执行本地文件操作时遇到问题，请关闭后重试。若问题持续，请导出日志以便排查。"

    def __init__(self, context: ApplicationContext):
        """
        收集应用上下文中的日志、事件与各服务句柄。
        """
        self.logger = get_logger("FrontendApi")
        self.events = context.events
        self.launcher = context.state
        self.context = context
        self.config = context.config
        self.accounts = context.accounts
        self.wardrobe = context.wardrobe
        self.http = context.http
        self.info_card = context.info_card
        self.connector = context.connector
        self.game = context.game
        self.plugins = context.plugins
        self.processes = context.processes
        self.background_media = getattr(context, "background_media", None)
        self.startup_update = getattr(context, "startup_update", None)
        self.app_path: Path = self.launcher.app_path
        self.data_path: Path = self.launcher.data_path
        self._webview: WebviewWindow | None = None
        self._webviews: dict[str, WebviewWindow] = {}
        self._window_metadata: dict[str, dict[str, Any]] = {}
        self._plugins_frontend_ready = False
        self._cli_launch_dispatched = False  # 命令行快捷启动是否已派发（每次进程运行至多一次）
        self._cli_launch_task: asyncio.Task[None] | None = None  # 持有任务引用，避免被垃圾回收
        self._single_instance = getattr(context, "single_instance", None)
        self._cli_event_loop: asyncio.AbstractEventLoop | None = None
        self._pending_cli_launches: deque[LaunchOptions] = deque()
        self._active_cli_request: LaunchOptions | None = None
        self._launch_closing = False
        self.events.subscribe("launcher:launch_request", self._on_cli_request)
        self.events.subscribe("launcher:closing", self._on_launcher_closing)
        self._main_window_event_bound = False
        self._pending_frontend_events: list[tuple[str, Any]] = []
        self._pending_error_presentations: dict[str, dict[str, Any]] = {}
        self._frontend_event_lock = RLock()
        self.is_dev_mode_tips = False
        self.is_dev_mode_no_client_id_tips = False
        self.is_dev_mode_no_curseforge_key_tips = False

    @staticmethod
    def _invalid_request(exc: ValidationError) -> ApiResponse:
        # 将 Pydantic 边界校验错误转换为稳定 IPC 响应。
        message = exc.errors(include_url=False)[0].get("msg", "请求参数无效")
        return failure(str(message), "INVALID_REQUEST")

    def _queue_frontend_event(self, event: str, payload: Any) -> None:
        # 将需排队的前端事件追加到待发送缓冲，超过上限时丢弃最早的事件。
        if event not in self.queued_frontend_events:
            return
        with self._frontend_event_lock:
            self._pending_frontend_events.append((event, payload))
            if len(self._pending_frontend_events) > self.max_pending_frontend_events:
                self._pending_frontend_events.pop(0)

    def emit_to_frontend(self, event: str, payload: Any, *, target_label: str | None = None) -> None:
        """
        向前端发送事件。

        :param event: 事件名称，如 ``config:updated``
        :param payload: 事件或请求携带的数据
        """
        if target_label is None and self._window_metadata:
            labels = [
                label
                for label, metadata in self._window_metadata.items()
                if label in self._webviews and self._window_event_allowed(metadata, event, payload)
            ]
            if labels:
                for label in labels:
                    self.emit_to_frontend(event, payload, target_label=label)
                return
        if self._webview is None and not self._webviews:
            self._queue_frontend_event(event, payload)
            return
        webview = self._webviews.get(target_label) if target_label else self._webview
        webview = webview or self._webview or next(iter(self._webviews.values()))
        if webview is None:
            self._queue_frontend_event(event, payload)
            return
        serialized = json.dumps(payload, ensure_ascii=False)

        def emit_on_main_thread() -> None:
            try:
                targeted = getattr(EventTarget, "WebviewWindow", None)
                target = targeted(target_label) if target_label and callable(targeted) else EventTarget.Any()
                _Emitter.emit_str_to(webview, target, event, serialized)
            except (OSError, TypeError, ValueError, RuntimeError):
                self.logger.exception("向前端推送事件失败: %s", event)
                self._queue_frontend_event(event, payload)

        try:
            # 游戏退出和崩溃分析回调运行在工作线程。WebView 发射必须切回 Tauri
            # 主线程，否则事件会被排队在一个已经完成 frontend_ready 的会话中。
            webview.run_on_main_thread(emit_on_main_thread)
        except (OSError, TypeError, ValueError, RuntimeError):
            # 前端窗口已关闭时停止发送，避免关闭期间反复报错
            failed_label = target_label
            if failed_label:
                self._webviews.pop(failed_label, None)
                self._window_metadata.pop(failed_label, None)
            elif self._webview is webview:
                self._webview = None
            self.logger.warning("前端已关闭，停止推送事件: %s", event)

    @staticmethod
    def _window_event_allowed(metadata: dict[str, Any], event: str, payload: Any) -> bool:
        window_type = metadata.get("windowType", "main")
        if window_type == "main":
            return True
        if window_type != "plugin":
            return False
        allowed = metadata.get("allowedEvents") or []
        if event in allowed:
            return True
        plugin = metadata.get("plugin")
        return event.startswith("plugin:") and isinstance(payload, dict) and payload.get("plugin") == plugin

    def authorize_window_command(
        self, operation: str, body: dict[str, Any], webview_window: WebviewWindow
    ) -> dict[str, Any] | None:
        """
        Reject capabilities not granted to the invoking registered WebView.
        """
        label_getter = getattr(webview_window, "label", None)
        if not callable(label_getter):
            return None
        label = str(label_getter())
        if label == "main":
            return None
        metadata = self._window_metadata.get(label)
        if metadata is None:
            return None if operation == "frontend_ready" else failure("窗口尚未由宿主注册", "WINDOW_NOT_REGISTERED")
        window_type = metadata.get("windowType")
        common = {"frontend_ready", "window_close", "window_focus", "window_update_bounds"}
        if operation in common:
            return None
        if window_type != "plugin":
            return failure("未知窗口类型", "WINDOW_COMMAND_DENIED")
        plugin = metadata.get("plugin")
        if operation in {"plugin_get_settings", "plugin_update_setting"}:
            if body.get("plugin_name") != plugin:
                return failure("插件窗口不能访问其他插件设置", "WINDOW_DATA_DENIED")
            allowed_settings = metadata.get("allowedSettings") or []
            if operation == "plugin_update_setting" and body.get("key") not in allowed_settings:
                return failure("插件设置字段未在窗口 schema 中授权", "WINDOW_DATA_DENIED")
            return None
        if operation == "plugin_call_command":
            command = body.get("command")
            allowed_commands = metadata.get("allowedCommands") or []
            if (
                not isinstance(command, str)
                or not command.startswith(f"{plugin}:")
                or command.split(":", 1)[1] not in allowed_commands
            ):
                return failure("插件命令未在窗口 schema 中授权", "WINDOW_COMMAND_DENIED")
            return None
        return failure("插件窗口无权调用该宿主命令", "WINDOW_COMMAND_DENIED")

    def emit_popup_to_frontend(self, payload: dict[str, Any]) -> None:
        """
        向前端发送弹窗事件。

        :param payload: 待呈现的弹窗内容
        """
        if isinstance(payload, dict):
            self.emit_to_frontend("launcher:popup", payload)

    def emit_error_to_frontend(self, payload: dict[str, Any]) -> None:
        """
        规范化严重错误并向前端推送，同时为其保留待确认呈现的内存副本。

        :param payload: 包含错误信息的原始数据
        """
        if not isinstance(payload, dict):
            return
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return
        normalized = {
            "error_id": str(payload.get("error_id") or uuid4().hex),
            "title": str(payload.get("title") or "启动器发生错误"),
            "message": message.strip(),
        }
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            normalized["detail"] = detail.strip()
        if payload.get("kind") == "game_crash" and isinstance(payload.get("crash"), dict):
            normalized["kind"] = "game_crash"
            normalized["crash"] = payload["crash"]
        with self._frontend_event_lock:
            self._pending_error_presentations[normalized["error_id"]] = normalized
            while len(self._pending_error_presentations) > self.max_pending_frontend_events:
                oldest = next(iter(self._pending_error_presentations))
                self._pending_error_presentations.pop(oldest, None)
        self.emit_to_frontend("launcher:error", normalized)

    def focus_window(self) -> bool:
        """
        在前端主线程激活并置顶启动器窗口。
        """
        webview = self._webview or self._webviews.get("main")
        if webview is None:
            return False

        def focus() -> None:
            webview.unminimize()
            webview.show()
            webview.set_focus()

        try:
            webview.run_on_main_thread(focus)
        except (OSError, RuntimeError):
            self.logger.exception("激活启动器窗口失败")
            return False
        return True

    def _flush_pending_frontend_events(self) -> None:
        # 将排队的前端事件全部转发给已就绪的 WebView。
        with self._frontend_event_lock:
            pending_events = self._pending_frontend_events
            self._pending_frontend_events = []
        for event, payload in pending_events:
            self.emit_to_frontend(event, payload)

    def _get_effective_config(self) -> dict[str, Any]:
        # 合并持久化配置与运行时覆盖，构造前端可见的有效配置；
        # 命令行启动参数在此兜底叠加，使界面展示的即会话实际生效值。
        config = dict(self.config.get_config())
        launcher_config = dict(config.get("launcher") or {})
        runtime_config = (self.launcher.config or {}).get("launcher") or {}
        launcher_config.update(runtime_config)
        launcher_config["debug"] = bool(self.launcher.debug)
        launcher_config["debug_log_level"] = getattr(self.launcher, "debug_log_level", None) or "info"
        config["launcher"] = launcher_config
        return apply_launch_overrides(config, getattr(self.launcher, "launch_options", None))

    @staticmethod
    def _normalize_authlib_server_url(value: Any) -> str | None:
        # 规整外置登录服务器地址，缺少协议时补全为 HTTPS。
        if not isinstance(value, str):
            return None
        server_url = value.strip()
        if not server_url or " " in server_url:
            return None
        if "://" not in server_url:
            server_url = f"https://{server_url}"
        return server_url if _is_http_url(server_url) else None

    @property
    def _authlib_servers_file(self) -> Path:
        return Path.home() / ".ECL" / "accounts" / "authlib" / "servers.json"

    def _load_authlib_servers(self) -> list[dict[str, str]]:
        # 从磁盘读取外置登录服务器历史。
        file = self._authlib_servers_file
        try:
            if file.is_file():
                raw = json.loads(file.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [
                        {"url": str(item.get("url") or ""), "email": str(item.get("email") or "")}
                        for item in raw
                        if isinstance(item, dict)
                    ]
        except (OSError, ValueError, TypeError):
            self.logger.warning("读取外置登录服务器历史失败，使用空列表: %s", file)
        return []

    def _save_authlib_servers(self, servers: list[dict[str, str]]) -> None:
        # 原子写入外置登录服务器历史。
        file = self._authlib_servers_file
        file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(file, json.dumps(servers, ensure_ascii=False, indent=2))

    def _get_authlib_servers(self) -> list[dict[str, str]]:
        # 规整并去重外置登录服务器历史。
        servers: list[dict[str, str]] = []
        for item in self._load_authlib_servers():
            url = self._normalize_authlib_server_url(item.get("url"))
            if not url or any(server["url"] == url for server in servers):
                continue
            email = item.get("email") if isinstance(item, dict) else ""
            servers.append({"url": url, "email": email if isinstance(email, str) else ""})
        return servers

    def _remember_authlib_login(self, server_url: str, email: str) -> None:
        # 将一次成功登录的外置服务器置于历史队首并保存。
        servers = [server for server in self._get_authlib_servers() if server["url"] != server_url]
        servers.insert(0, {"url": server_url, "email": email})
        self._save_authlib_servers(servers[:20])

    async def frontend_log(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        记录前端通过 IPC 上报的运行日志，供统一归档排查。

        前端 console 输出会被拦截后转发到这里，写入与后端一致的文件日志。

        :param body: 符合 ``FrontendLogRequest`` 的日志数据
        :return: 空的成功响应
        """
        request, invalid = _validate_body(FrontendLogRequest, body)
        if invalid is not None:
            return invalid
        message = request.message
        if request.detail:
            message = f"{message}\n{request.detail}"
        if request.logger:
            message = f"[{request.logger}] {message}"
        log_level = request.level.value
        if log_level == "warn":
            log_level = "warning"
        log_method = getattr(self.logger, log_level)
        log_method("[Frontend] %s", message)
        return {"success": True}

    async def logs_get_history(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回最近缓存的启动器日志，供前端日志终端打开时补全历史。

        :param body: 空请求数据
        :return: 包含 ``logs`` 列表的成功响应
        """
        return {"success": True, "data": {"logs": get_frontend_log_history()}}

    async def process_instances(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回注册表内登记的子进程实例列表。

        :param body: 必须为空的请求对象
        :return: 包含 ``instances`` 列表的成功响应
        """
        if body:
            return failure("process_instances 不接受请求参数", "INVALID_REQUEST")
        return {"success": True, "data": {"instances": self.processes.list()}}

    @_ipc_handler("PROCESS_INPUT_FAILED")
    async def process_input(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        向指定子进程实例写入一行标准输入。

        :param body: 符合 ``ProcessInputRequest`` 的请求数据
        :return: 是否成功写入的标准输入标识
        """
        request, invalid = _validate_body(ProcessInputRequest, body)
        if invalid is not None:
            return invalid
        return {"success": True, "data": {"sent": self.processes.send_stdin(request.instance_id, request.data)}}

    @_ipc_handler("PROCESS_STOP_FAILED")
    async def process_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        停止指定子进程实例；Minecraft 实例转发到游戏服务以正确结算统计。

        :param body: 符合 ``ProcessStopRequest`` 的请求数据
        :return: 进程是否已结束的成功响应
        """
        request, invalid = _validate_body(ProcessStopRequest, body)
        if invalid is not None:
            return invalid
        instance_type = next(
            (item.get("type") for item in self.processes.list() if item.get("id") == request.instance_id),
            None,
        )
        if instance_type == "Minecraft":
            await to_thread.run_sync(self.game.stop_instance, request.instance_id)
            return {"success": True, "data": {"stopped": True}}
        return {"success": True, "data": {"stopped": self.processes.stop(request.instance_id)}}

    async def debug_process_spawn(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        在调试模式下启动一个子进程实例，供开发自测实例终端。

        :param body: 符合 ``DebugProcessSpawnRequest`` 的请求数据
        :return: 包含 ``instanceId`` 的成功响应
        """
        if not self.launcher.debug:
            return failure("调试命令仅在调试模式下可用", "INVALID_STATE")
        request, invalid = _validate_body(DebugProcessSpawnRequest, body)
        if invalid is not None:
            return invalid
        try:
            instance_id = self.processes.spawn(
                request.name,
                request.type,
                request.args,
                cwd=request.cwd,
                stdin=request.stdin,
            )
        except ValueError as exc:
            return failure(str(exc), "INVALID_REQUEST")
        return {"success": True, "data": {"instanceId": instance_id}}

    def _game_runtime_options(self, body: dict[str, Any]) -> dict[str, Any]:
        # 汇总启动游戏所需的运行时参数，优先使用调用方显式指定的值。
        config = self._get_effective_config()
        game_config = config.get("game") or {}
        download_config = config.get("download") or {}
        minecraft_paths = game_config.get("minecraft_paths") or []
        first_path = None
        if minecraft_paths:
            first_item = minecraft_paths[0]
            first_path = first_item.get("path") if isinstance(first_item, dict) else first_item
        java_path = body.get("java_path")
        if java_path is None and not game_config.get("java_auto", True):
            java_path = game_config.get("java_path") or None
        options: dict[str, Any] = {
            "game_path": body.get("game_path") or game_config.get("last_install_path") or first_path,
            "source": download_config.get("mirror_source") or "official",
            "java_path": java_path,
        }
        for field, body_key, config_lookup in self.runtime_option_fields:
            value = body.get(body_key)
            if value is None:
                value = config_lookup(game_config)
            options[field] = value
        options["game_args"] = body.get("game_args") or []
        options["version_isolation"] = bool(body.get("version_isolation", False))
        return options

    async def frontend_ready(  # noqa: C901
        self, body: dict[str, Any], webview_window: WebviewWindow
    ) -> dict[str, Any]:
        """
        处理前端就绪。

        :param body: 经过边界校验的 IPC 请求数据
        :param webview_window: 前端 WebView 窗口实例
        """
        label_getter = getattr(webview_window, "label", None)
        label = str(label_getter()) if callable(label_getter) else "main"
        known_metadata = self._window_metadata.get(label)
        # 非主窗口身份只能来自后端 window_open 创建的注册项，不能信任前端自报。
        if known_metadata is None and label != "main":
            # 未经后端注册的窗口不得自报为主窗口，避免顶替主窗口身份与事件目标。
            self.logger.warning("未注册的前端窗口调用 frontend_ready：窗口标识：%s", label)
            window_type = "unregistered"
            session_id = None
        else:
            window_type = str(known_metadata.get("windowType", "main")) if known_metadata else "main"
            session_id = known_metadata.get("sessionId") if known_metadata else None
        self._webviews[label] = webview_window
        if known_metadata is not None:
            metadata = dict(known_metadata)
        elif window_type == "unregistered":
            metadata = {"label": label, "descriptorId": "", "windowType": "unregistered"}
        else:
            metadata = {"label": label, "descriptorId": "main", "windowType": "main"}
        metadata.update({"label": label, "sessionId": session_id, "ready": True})
        self._window_metadata[label] = metadata
        self.logger.info("前端窗口已就绪：窗口标识：%s；类型：%s", label, window_type)
        if window_type not in {"main", "unregistered"}:
            self.emit_to_frontend("window:ready", metadata)
        if window_type == "main" or label == "main":
            self._webview = webview_window
        if window_type == "main" and not self._main_window_event_bound:
            on_window_event = getattr(webview_window, "on_window_event", None)
            if callable(on_window_event):

                def handle_main_window_event(*args: Any) -> None:
                    event = args[-1] if args else None
                    event_name = type(event).__name__.lower()
                    if "destroy" not in event_name and "close" not in event_name:
                        return
                    for child_label, child_metadata in list(self._window_metadata.items()):
                        if child_label == label or not child_metadata.get("followMain"):
                            continue
                        child = self._webviews.pop(child_label, None)
                        self._window_metadata.pop(child_label, None)
                        if child is not None:
                            with suppress(OSError, RuntimeError):
                                child.run_on_main_thread(child.close)

                on_window_event(handle_main_window_event)
                self._main_window_event_bound = True
        if label != "main":
            webview_window.show()
            if window_type not in {"main", "unregistered"}:
                with suppress(OSError, RuntimeError):
                    webview_window.unminimize()
                    webview_window.set_focus()
        if self._webview is webview_window:
            self._flush_pending_frontend_events()
        if not self._plugins_frontend_ready and window_type == "main":
            self.plugins.on_frontend_ready()
            self._plugins_frontend_ready = True
        if window_type != "main":
            return {"success": True, "data": self._window_metadata[label]}
        if bool(self.launcher.debug) and (self.is_dev_mode_tips is False):
            self.is_dev_mode_tips = True
            self.emit_popup_to_frontend(
                {
                    "id": "launcher-development-mode",
                    "title": "开发模式提示",
                    "content": (
                        "当前启动器正以 **开发模式** 运行，部分功能可能尚未完成或存在不稳定行为。\n\n"
                        "如果遇到问题，请保留相关日志以便排查。"
                    ),
                    "level": "warning",
                    "dismissible": True,
                    "cacheable": True,
                }
            )
        if (not self.game.curseforge_available()) and (self.is_dev_mode_no_curseforge_key_tips is False):
            self.is_dev_mode_no_curseforge_key_tips = True
            self.emit_popup_to_frontend(
                {
                    "id": "curseforge-key-required",
                    "title": "CurseForge API Key 未配置",
                    "content": (
                        "在线模组搜索的 **CurseForge** 来源需要配置 API Key，未配置时该来源会被禁用。\n\n"
                        "请在启动器根目录的 `.env` 文件中添加 `CURSEFORGE_API_KEY=你的密钥`，"
                        "或设置同名系统环境变量后重启启动器。"
                    ),
                    "level": "warning",
                    "dismissible": True,
                    "cacheable": True,
                }
            )
        if (not self.accounts.microsoft_login_config().get("available")) and (
            self.is_dev_mode_no_client_id_tips is False
        ):
            self.is_dev_mode_no_client_id_tips = True
            self.emit_popup_to_frontend(
                {
                    "id": "microsoft-client-id-required",
                    "title": "Microsoft client_id 未配置",
                    "content": (
                        "正版（Microsoft）登录需要配置 **client_id**，未配置时无法使用正版登录。\n\n"
                        "请在启动器根目录的 `.env` 文件中添加 `MICROSOFT_CLIENT_ID=你的应用ID`，"
                        "或设置同名系统环境变量后重启启动器。"
                    ),
                    "level": "warning",
                    "dismissible": True,
                    "cacheable": True,
                }
            )
        if self.startup_update is not None:
            startup_update_result = self.startup_update.result()
            if startup_update_result is not None:
                self.emit_to_frontend("update:check_completed", startup_update_result)
        if window_type == "main":
            self._cli_event_loop = asyncio.get_running_loop()
            if not self._cli_launch_dispatched:
                self._cli_launch_dispatched = True
                options = getattr(self.launcher, "launch_options", None)
                if isinstance(options, LaunchOptions) and (options.launch_target or options.open_page):
                    self._pending_cli_launches.append(options)
            self._start_queued_launches()
        self.logger.info("前端加载完成")
        return {"success": True}

    def _on_cli_request(self, _payload: object) -> None:
        """
        从监听线程唤醒主事件循环，初始化阶段保留服务内请求。
        """
        loop = self._cli_event_loop
        if loop is not None and not loop.is_closed() and not self._launch_closing:
            loop.call_soon_threadsafe(self._start_queued_launches)

    def _on_launcher_closing(self, _payload: object) -> None:
        """
        关闭后停止调度并取消持有的启动任务，防止服务销毁后继续消费。
        """
        self._launch_closing = True
        loop = self._cli_event_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._cancel_cli_requests)

    def _cancel_cli_requests(self) -> None:
        self._pending_cli_launches.clear()
        if self._cli_launch_task is not None:
            self._cli_launch_task.cancel()

    def _start_queued_launches(self) -> None:
        if self._launch_closing:
            return
        if self._single_instance is not None:
            for request in self._single_instance.take_launch_requests():
                if request != self._active_cli_request and request not in self._pending_cli_launches:
                    self._pending_cli_launches.append(request)
        if self._pending_cli_launches and (self._cli_launch_task is None or self._cli_launch_task.done()):
            self._cli_launch_task = asyncio.create_task(self._drain_cli_requests())

    async def _drain_cli_requests(self) -> None:
        try:
            while self._pending_cli_launches and not self._launch_closing:
                options = self._pending_cli_launches.popleft()
                self._active_cli_request = options
                if options.open_page:
                    self.emit_to_frontend("launcher:open_page", {"page": options.open_page})
                else:
                    await self._run_cli_launch(options)
        finally:
            self._active_cli_request = None

    async def _run_cli_launch(self, options: LaunchOptions | None = None) -> None:
        """
        处理命令行快捷启动目标。

        解析 ``--launch`` 指向的实例并复用 ``game_launch`` 完成启动，与用户
        在界面点击「启动」按钮的行为保持一致；解析或启动过程中的任何失败都
        以弹窗事件呈现，不阻断启动器运行。

        :raises None: 本方法不向调用方抛出异常，全部失败均转为弹窗事件
        """
        options = options or getattr(self.launcher, "launch_options", None)
        target = str(getattr(options, "launch_target", "") or "").strip()
        try:
            game_config = self._get_effective_config().get("game") or {}
            roots = [options.game_dir] if options is not None and options.game_dir else candidate_roots(game_config)
            game_path, version_id = await to_thread.run_sync(resolve_launch_target, target, roots)
            body: dict[str, Any] = {"version_id": version_id, "game_path": str(game_path)}
            if isinstance(options, LaunchOptions):
                body.update(options.game_overrides())
            server_target = str(getattr(options, "server_target", "") or "")
            world_target = str(getattr(options, "world_target", "") or "")
            if server_target:
                body["quick_target"] = {"type": "server", "address": server_target}
            elif world_target:
                body["quick_target"] = {"type": "world", "world_id": world_target}
        except GameServiceError as exc:
            self.logger.warning("快捷启动目标解析失败：目标：%s；错误码：%s", target, exc.error_code)
            self.emit_popup_to_frontend({"id": "cli-launch-failed", "title": "快捷启动失败", "content": str(exc)})
            return
        except Exception:
            self.logger.exception("快捷启动目标解析出现意外错误：目标：%s", target)
            self.emit_popup_to_frontend(
                {
                    "id": "cli-launch-failed",
                    "title": "快捷启动失败",
                    "content": f"无法解析快捷启动目标：{target}",
                }
            )
            return
        self.logger.info("正在通过命令行启动实例：版本：%s；目录：%s", version_id, game_path)
        try:
            response = await self.game_launch(body)
        except Exception:
            self.logger.exception("命令行启动实例失败：版本：%s", version_id)
            self.emit_popup_to_frontend(
                {
                    "id": "cli-launch-failed",
                    "title": "快捷启动失败",
                    "content": f"启动实例 {version_id} 失败，请导出日志排查。",
                }
            )
            return
        if not isinstance(response, dict) or response.get("success") is not True:
            message = str((response or {}).get("message") or "未知错误")
            self.logger.warning("命令行启动实例被拒绝：版本：%s；说明：%s", version_id, message)
            self.emit_popup_to_frontend({"id": "cli-launch-failed", "title": "快捷启动失败", "content": message})
            return
        self.logger.info("命令行实例启动流程完成：版本：%s", version_id)
