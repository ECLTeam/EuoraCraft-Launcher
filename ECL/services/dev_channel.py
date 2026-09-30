# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：开发者通道服务：本地 WebSocket 鉴权、方法分发与日志回放。
#
# 公开接口：
#   - class DevChannelError — 携带协议错误码的通道异常，用于把插件操作失败映射为响应错误信封。
#   - class DevChannelService — 开发者通道服务：在本地回环地址上提供 WebSocket 服务，把插件管理与日志能力
#       - port() -> int | None — 返回实际监听端口，服务未启动时为 None。
#       - frontend_url() -> str | None — 返回内嵌前端入口地址；未托管或服务未启动时为 None。
#       - install_frontend_handlers(handlers) -> None — 装入启动器前端命令表，使工具箱内嵌前端可通过 frontend.invoke 驱动后端。
#       - start() -> None — 启动后台线程承载 WebSocket 服务，就绪后写入连接发现文件。
#       - close() -> None — 停止服务线程、退订全部事件并删除连接发现文件，重复调用无副作用。
#       - dispatch_log(record) -> None — 把一条日志转换为协议条目，写入历史缓存并推送给已订阅客户端。
# ============================================================

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import mimetypes
import os
import secrets
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from ECL.plugins import PluginActionResult
from ECL.services.frontend_events import subscribe_frontend_event
from ECL.utils import PluginCommandError
from ECL.utils.files import atomic_write_text
from ECL.utils.logging import LoggingPolicy, get_logger

if TYPE_CHECKING:
    from ECL.events import EventBus
    from ECL.plugins import PluginManager


class DevChannelError(Exception):
    """
    携带协议错误码的通道异常，用于把插件操作失败映射为响应错误信封。
    """

    def __init__(self, code: str, message: str) -> None:
        """
        记录协议错误码与人类可读信息。

        :param code: protocol.json errorCodes 中定义的错误码
        :param message: 返回给客户端的中文说明
        """
        super().__init__(message)
        self.code = code  # 协议错误码
        self.message = message  # 人类可读的失败原因


class _Session:
    """
    单个客户端连接的订阅状态，连接关闭时统一退订全部事件。
    """

    def __init__(self, websocket: ServerConnection) -> None:
        self.websocket = websocket  # 已通过鉴权的连接
        self.log_subscribed = False  # 是否订阅了 log.line 推送
        self.event_unsubscribers: dict[str, Callable[[], None]] = {}  # 事件名到退订函数的映射
        self.frontend_subscribed: set[str] = set()  # 已订阅的前端事件名集合
        self.frontend_unsubscribers: dict[str, list[Callable[[], None]]] = {}  # 前端事件名到退订函数列表
        self.preview_nonces: set[str] = set()  # 当前连接创建的预览会话，断开后立即失效


@dataclass(frozen=True, slots=True)
class _PreviewSession:
    """
    保存一次受控预览会话的来源、随机数与过期时间。
    """

    nonce: str
    parent_origin: str
    expires_at: float


class _ChannelLogHandler(logging.Handler):
    """
    把启动器日志转换为协议格式并推送给已订阅客户端的日志处理器。
    """

    def __init__(self, service: DevChannelService) -> None:
        """
        绑定所属服务，由服务负责安装与移除。

        :param service: 拥有推送目标列表的开发者通道服务
        """
        super().__init__()
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        """
        序列化一条日志并分发给所有已订阅的客户端。
        """
        self._service.dispatch_log(record)


class DevChannelService:
    """
    开发者通道服务：在本地回环地址上提供 WebSocket 服务，把插件管理与日志能力
    暴露给 ECLPluginDevTool 等外部开发工具。

    服务运行在独立后台线程的事件循环中；连接发现信息（端口与一次性令牌）写入
    数据目录下的 ``dev_channel.json``，由工具箱读取后完成鉴权接入。
    """

    protocol_version = 1
    auth_timeout_seconds = 5
    max_payload_bytes = 1_048_576
    discovery_filename = "dev_channel.json"
    log_history_limit = 500
    preview_session_limit = 8
    preview_session_ttl_seconds = 300
    event_prefix_whitelist = ("plugin:", "game:", "launcher:", "process:", "config:", "accounts:")

    def __init__(
        self,
        *,
        plugins: PluginManager,
        events: EventBus,
        data_path: Path,
        launcher_version: str,
        debug: bool,
        frontend_dist: Path | None = None,
    ) -> None:
        """
        收集依赖并生成一次性访问令牌，服务尚未启动。

        :param plugins: 插件管理器，提供列表与生命周期操作
        :param events: 应用事件总线，用于转发白名单事件
        :param data_path: 启动器数据目录，发现文件写入其中
        :param launcher_version: 当前启动器版本号
        :param debug: 是否处于调试模式
        :param frontend_dist: 前端构建产物目录，存在时在同端口托管内嵌前端，供工具箱加载
        """
        self.logger = get_logger("DevChannel")  # 服务专用日志器
        self._plugins = plugins  # 插件管理器
        self._events = events  # 应用事件总线
        self._data_path = Path(data_path)  # 启动器数据目录
        self._launcher_version = launcher_version  # 启动器版本号
        self._debug = debug  # 调试模式标记
        self._frontend_dist = Path(frontend_dist) if frontend_dist else None  # 前端构建产物目录
        self._frontend_enabled = bool(self._frontend_dist and self._frontend_dist.is_dir())  # 是否托管内嵌前端
        self._index_template: bytes | None = None  # 注入连接信息后的内嵌前端入口缓存
        self._discovery_path = self._data_path / self.discovery_filename  # 连接发现文件路径
        self._token = secrets.token_urlsafe(32)  # 一次性访问令牌，随进程生成
        self._port: int | None = None  # 实际监听端口
        self._loop: asyncio.AbstractEventLoop | None = None  # 服务线程的事件循环
        self._stop_future: asyncio.Future[None] | None = None  # 通知服务线程退出的信号
        self._thread: threading.Thread | None = None  # 承载服务事件循环的后台线程
        self._log_handler: _ChannelLogHandler | None = None  # 挂在根日志器上的推送处理器
        self._log_history: deque[dict[str, Any]] = deque(maxlen=self.log_history_limit)  # 最近日志缓存
        self._sessions: set[_Session] = set()  # 已通过鉴权的连接会话
        self._pending_sends: set[asyncio.Task[None]] = set()  # 进行中的通知发送任务，防止任务被提前回收
        self._preview_sessions: dict[str, _PreviewSession] = {}  # nonce 到受控 iframe 会话的映射
        self._closed = False  # 服务是否已关闭（用于幂等）
        self._methods: dict[str, Callable[[_Session, dict[str, Any]], Any]] = {
            "launcher.info": self._method_launcher_info,
            "plugin.list": self._method_plugin_list,
            "plugin.enable": self._method_plugin_enable,
            "plugin.disable": self._method_plugin_disable,
            "plugin.reload": self._method_plugin_reload,
            "plugin.unload": self._method_plugin_unload,
            "plugin.install": self._method_plugin_install,
            "plugin.inspect": self._method_plugin_inspect,
            "plugin.call_command": self._method_plugin_call_command,
            "plugin.get_settings": self._method_plugin_get_settings,
            "logs.subscribe": self._method_logs_subscribe,
            "logs.unsubscribe": self._method_logs_unsubscribe,
            "events.subscribe": self._method_events_subscribe,
            "events.unsubscribe": self._method_events_unsubscribe,
            "frontend.invoke": self._method_frontend_invoke,
            "frontend.subscribe": self._method_frontend_subscribe,
            "frontend.unsubscribe": self._method_frontend_unsubscribe,
            "preview.session.create": self._method_preview_session_create,
            "preview.session.close": self._method_preview_session_close,
        }
        self._frontend_handlers: dict[str, Callable[..., Any]] = {}  # 启动器前端命令表，装入后供预览调用

    @property
    def port(self) -> int | None:
        """
        返回实际监听端口，服务未启动时为 None。
        """
        return self._port

    @property
    def frontend_url(self) -> str | None:
        """
        返回内嵌前端入口地址；未托管或服务未启动时为 None。
        """
        if not self._frontend_enabled or self._port is None:
            return None
        return f"http://127.0.0.1:{self._port}/"

    def install_frontend_handlers(self, handlers: dict[str, Callable[..., Any]]) -> None:
        """
        装入启动器前端命令表，使工具箱内嵌前端可通过 frontend.invoke 驱动后端。

        :param handlers: command_handlers(FrontendApi(context)) 返回的命令名到处理器映射
        """
        self._frontend_handlers.clear()
        self._frontend_handlers.update(handlers)
        self.logger.debug("前端命令表已装入: count=%d", len(handlers))

    def start(self) -> None:
        """
        启动后台线程承载 WebSocket 服务，就绪后写入连接发现文件。

        :raises RuntimeError: 服务线程在超时时间内未能完成端口绑定
        """
        if self._thread is not None or self._closed:
            return
        self._log_handler = _ChannelLogHandler(self)
        logging.getLogger(LoggingPolicy.logger_name).addHandler(self._log_handler)
        started = threading.Event()
        failures: list[BaseException] = []
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(started, failures),
            name="ECL-DevChannel",
            daemon=True,
        )
        self._thread.start()
        if not started.wait(timeout=10):
            raise RuntimeError("开发者通道服务启动超时")
        if failures:
            self._remove_log_handler()
            raise failures[0]
        self._write_discovery_file()
        self.logger.info("开发者通道已开启，监听端口 %s", self._port)

    def close(self) -> None:
        """
        停止服务线程、退订全部事件并删除连接发现文件，重复调用无副作用。
        """
        if self._closed:
            return
        self._closed = True
        self._remove_log_handler()
        loop, future, thread = self._loop, self._stop_future, self._thread
        if loop is not None and future is not None:
            loop.call_soon_threadsafe(self._resolve_stop_future, future)
        if thread is not None:
            thread.join(timeout=5)
        for session in tuple(self._sessions):
            self._cleanup_session(session)
        self._sessions.clear()
        with suppress(OSError):
            self._discovery_path.unlink()
        self.logger.debug("开发者通道已关闭")

    def dispatch_log(self, record: logging.LogRecord) -> None:
        """
        把一条日志转换为协议条目，写入历史缓存并推送给已订阅客户端。

        :param record: 标准库日志记录
        """
        entry = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        self._log_history.append(entry)
        for session in tuple(self._sessions):
            if session.log_subscribed:
                self._notify(session, "log.line", entry)

    def _run_loop(self, started: threading.Event, failures: list[BaseException]) -> None:
        # 在独立事件循环中承载 WebSocket 服务，直到收到关闭信号。
        async def main() -> None:
            try:
                server = await serve(
                    self._handle,
                    "127.0.0.1",
                    0,
                    max_size=self.max_payload_bytes,
                    process_request=self._process_http_request,
                )
            except OSError as exc:
                failures.append(exc)
                started.set()
                return
            self._port = server.sockets[0].getsockname()[1]
            self._loop = asyncio.get_running_loop()
            self._stop_future = self._loop.create_future()
            started.set()
            async with server:
                await self._stop_future

        asyncio.run(main())

    @staticmethod
    def _resolve_stop_future(future: asyncio.Future[None]) -> None:
        # 在服务线程的事件循环内完成关闭信号。
        if not future.done():
            future.set_result(None)

    def _remove_log_handler(self) -> None:
        # 从根日志器移除推送处理器，避免关闭后继续分发。
        if self._log_handler is None:
            return
        logging.getLogger(LoggingPolicy.logger_name).removeHandler(self._log_handler)
        self._log_handler = None

    def _write_discovery_file(self) -> None:
        # 写入端口与令牌，供工具箱发现并接入本次运行的启动器。
        payload = {
            "port": self._port,
            "token": self._token,
            "pid": os.getpid(),
            "launcherVersion": self._launcher_version,
            "protocolVersion": self.protocol_version,
            "frontendUrl": self.frontend_url,
        }
        self._data_path.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._discovery_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _cleanup_session(self, session: _Session) -> None:
        # 退订该会话的全部后端事件、前端事件与日志订阅，标记一并复位。
        for unsubscribe in session.event_unsubscribers.values():
            unsubscribe()
        session.event_unsubscribers.clear()
        for unsubscribers in session.frontend_unsubscribers.values():
            for unsubscribe in unsubscribers:
                unsubscribe()
        session.frontend_unsubscribers.clear()
        session.frontend_subscribed.clear()
        session.log_subscribed = False
        for nonce in session.preview_nonces:
            self._preview_sessions.pop(nonce, None)
        session.preview_nonces.clear()

    async def _handle(self, websocket: ServerConnection) -> None:
        # 完成鉴权后循环处理请求信封，断开时统一退订。
        if self._closed:
            return
        if not await self._authenticate(websocket):
            return
        session = _Session(websocket)
        self._sessions.add(session)
        try:
            async for raw in websocket:
                await self._process_message(session, raw)
        finally:
            self._cleanup_session(session)
            self._sessions.discard(session)

    def _process_http_request(self, _connection: Any, request: Request) -> Response | None:
        # 同端口托管内嵌前端：普通 HTTP 请求返回静态资源，WebSocket 升级请求继续握手。
        if not self._frontend_enabled:
            return None
        if (request.headers.get("Upgrade") or "").strip().lower() == "websocket":
            return None
        resolved = self._resolve_static(str(request.path))
        if resolved is None:
            return None
        status, body, content_type = resolved
        headers = Headers(
            {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
            }
        )
        return Response(status, HTTPStatus(status).phrase, headers, body)

    def _resolve_static(self, request_path: str) -> tuple[int, bytes, str] | None:
        # 映射请求路径到静态资源，返回 (状态码, 字节, 内容类型)；禁用内嵌前端时返回 None。
        if not self._frontend_dist or not self._frontend_dist.is_dir():
            return None
        base = self._frontend_dist.resolve()
        index = base / "index.html"
        parsed_request = urlsplit(request_path)
        rel = unquote(parsed_request.path).lstrip("/")
        if not rel:
            rel = "index.html"
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            target = index  # 越界路径一律回退到位移后的资源根目录入口
        if target.is_file():
            if target == index:
                # 入口页需注入连接信息；路径为 / 或 /index.html 时都命中此处。
                return (
                    HTTPStatus.OK.value,
                    self._injected_index(self._preview_session(parsed_request.query)),
                    "text/html; charset=utf-8",
                )
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return HTTPStatus.OK.value, target.read_bytes(), content_type
        # 带扩展名的缺包资源返回 404，无扩展名路径按页面路由回退到入口。
        if Path(rel).suffix:
            return HTTPStatus.NOT_FOUND.value, b"not found", "text/plain; charset=utf-8"
        if index.is_file():
            return (
                HTTPStatus.OK.value,
                self._injected_index(self._preview_session(parsed_request.query)),
                "text/html; charset=utf-8",
            )
        return None

    def _injected_index(self, preview_session: _PreviewSession | None = None) -> bytes:
        # 在内嵌前端入口注入 Dev Channel 连接信息，使嵌入页面无需宿主转发即可连回启动器。
        if preview_session is None and self._index_template is not None:
            return self._index_template
        assert self._frontend_dist is not None
        html = (self._frontend_dist / "index.html").read_bytes().decode("utf-8", errors="replace")
        script = f'<script>window.__ECL_DEV_WS__={{port:{self._port},token:"{self._token}"}};</script>'
        if preview_session is not None:
            preview_config = json.dumps(
                {"nonce": preview_session.nonce, "parentOrigin": preview_session.parent_origin}, ensure_ascii=False
            )
            script += (
                "<script>window.__ECL_PREVIEW_SESSION__="
                f"{preview_config};(function(){{const session=window.__ECL_PREVIEW_SESSION__;"
                "if(!session||window.parent===window)return;"
                "const send=(type,data)=>window.parent.postMessage({channel:'ecl-preview',type,nonce:session.nonce,data:data||{}},session.parentOrigin);"
                "window.addEventListener('message',event=>{const message=event.data;if(event.origin!==session.parentOrigin||event.source!==window.parent||!message||message.channel!=='ecl-preview'||message.nonce!==session.nonce||message.type!=='connect')return;send('connected',{path:window.location.pathname});});"
                "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>send('ready'));else send('ready');"
                "}})();</script>"
            )
        if "</head>" in html:
            html = html.replace("</head>", script + "</head>", 1)
        else:
            html += script
        rendered = html.encode("utf-8")
        if preview_session is None:
            self._index_template = rendered
        return rendered

    def _preview_session(self, query: str) -> _PreviewSession | None:
        """
        从 iframe 请求查询参数解析仍有效的预览会话。

        过期、格式异常或已关闭的 nonce 都不注入桥接脚本，普通前端托管请求继续保持
        原有行为；这样链接泄露后也不能无限期获得父窗口通信权限。
        """

        nonce = parse_qs(query).get("eclPreviewSession", [""])[0]
        if not isinstance(nonce, str):
            return None
        session = self._preview_sessions.get(nonce)
        if session is None:
            return None
        if session.expires_at <= monotonic():
            self._preview_sessions.pop(nonce, None)
            return None
        return session

    async def _authenticate(self, websocket: ServerConnection) -> bool:
        # 等待首条鉴权消息并校验令牌，失败时回复错误信封并断开。
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.auth_timeout_seconds)
            message = json.loads(raw)
        except (TimeoutError, ConnectionError, ValueError, TypeError):
            return False
        token_ok = (
            isinstance(message, dict)
            and message.get("op") == "auth"
            and secrets.compare_digest(str(message.get("token", "")), self._token)
        )
        if token_ok:
            await websocket.send(
                json.dumps(
                    {
                        "op": "auth_ok",
                        "protocolVersion": self.protocol_version,
                        "launcherVersion": self._launcher_version,
                    },
                    ensure_ascii=False,
                )
            )
            return True
        await websocket.send(
            json.dumps({"op": "auth_failed", "message": "鉴权失败：token 错误或超时"}, ensure_ascii=False)
        )
        await websocket.close()
        return False

    async def _process_message(self, session: _Session, raw: str | bytes) -> None:
        # 解析请求信封并分发到方法表，异常统一映射为错误信封。
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        handler = self._methods.get(method) if isinstance(method, str) else None
        if handler is None:
            await self._send_error(websocket=session.websocket, request_id=request_id, code="METHOD_NOT_FOUND")
            return
        try:
            data = handler(session, params if isinstance(params, dict) else {})
            if inspect.isawaitable(data):
                data = await data
        except DevChannelError as exc:
            await self._send_error(
                websocket=session.websocket, request_id=request_id, code=exc.code, message=exc.message
            )
            return
        except Exception:
            self.logger.exception("开发者通道方法 %s 执行失败", method)
            await self._send_error(websocket=session.websocket, request_id=request_id, code="INTERNAL_ERROR")
            return
        await session.websocket.send(
            json.dumps({"id": request_id, "ok": True, "data": data}, ensure_ascii=False, default=str)
        )

    @staticmethod
    async def _send_error(
        websocket: ServerConnection,
        request_id: Any,
        code: str,
        message: str | None = None,
    ) -> None:
        # 发送协议错误响应信封，message 缺省时使用错误码的标准描述。
        messages = {
            "AUTH_FAILED": "鉴权失败：token 错误或超时",
            "METHOD_NOT_FOUND": "方法不存在",
            "INVALID_PARAMS": "参数缺失或非法",
            "INTERNAL_ERROR": "服务端内部错误",
            "PLUGIN_NOT_FOUND": "插件不存在",
            "NOT_READY": "依赖的服务未就绪",
        }
        payload = {
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message or messages.get(code, code)},
        }
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    def _notify(self, session: _Session, event: str, data: Any) -> None:
        # 从任意线程向客户端推送通知，实际发送调度回服务线程的事件循环。
        loop = self._loop
        if loop is None or self._closed:
            return
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        loop.call_soon_threadsafe(self._schedule_send, session, payload)

    def _schedule_send(self, session: _Session, payload: str) -> None:
        # 在服务线程内异步发送；客户端已断开时静默放弃本次推送。
        task = asyncio.ensure_future(self._safe_send(session.websocket, payload))
        self._pending_sends.add(task)
        task.add_done_callback(self._pending_sends.discard)

    @staticmethod
    async def _safe_send(websocket: ServerConnection, payload: str) -> None:
        # 发送失败仅影响单个客户端，且不能在此记录日志以免触发日志处理器递归。
        with suppress(Exception):
            await websocket.send(payload)

    @staticmethod
    def _require_param_str(params: dict[str, Any], key: str) -> str:
        # 校验必填字符串参数，缺失或为空时抛出参数错误。
        value = params.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DevChannelError("INVALID_PARAMS", f"参数 {key} 缺失或非法")
        return value

    async def _method_frontend_invoke(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 按命令名分发到启动器前端命令表，供工具箱内嵌前端复用与 Tauri 相同的后端处理器。
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            raise DevChannelError("INVALID_PARAMS", "参数 command 缺失或非法")
        body = params.get("payload")
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise DevChannelError("INVALID_PARAMS", "参数 payload 必须为对象")
        handler = self._frontend_handlers.get(command)
        if handler is None:
            raise DevChannelError("METHOD_NOT_FOUND", f"前端命令不存在: {command}")
        result = await handler(body)
        return {"command": command, "result": result}

    async def _method_frontend_subscribe(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 按前端事件名订阅（与 Tauri 主窗口同一套事件转换），把结果转发给工具箱内嵌前端。
        events = params.get("events")
        if not isinstance(events, list) or not all(isinstance(item, str) for item in events):
            raise DevChannelError("INVALID_PARAMS", "参数 events 必须为字符串数组")
        subscribed = []
        for event in events:
            if event in session.frontend_subscribed:
                subscribed.append(event)
                continue

            def emit(frontend_event: str, payload: Any) -> None:
                self._notify(session, frontend_event, payload)

            unsubscribers = subscribe_frontend_event(self._events, event, emit)
            if not unsubscribers:
                continue  # 未在事件桥注册的前端事件名，直接忽略
            session.frontend_unsubscribers[event] = unsubscribers
            session.frontend_subscribed.add(event)
            subscribed.append(event)
        return {"subscribed": subscribed}

    async def _method_frontend_unsubscribe(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 退订指定前端事件，未订阅的名称直接忽略。
        events = params.get("events")
        if not isinstance(events, list) or not all(isinstance(item, str) for item in events):
            raise DevChannelError("INVALID_PARAMS", "参数 events 必须为字符串数组")
        unsubscribed = []
        for event in events:
            unsubscribers = session.frontend_unsubscribers.pop(event, None)
            if not unsubscribers:
                continue
            for unsubscribe in unsubscribers:
                unsubscribe()
            session.frontend_subscribed.discard(event)
            unsubscribed.append(event)
        return {"unsubscribed": unsubscribed}

    def _method_preview_session_create(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        """
        为一个工作台 iframe 创建短生命周期、来源绑定的预览会话。

        开发工具已通过 Dev Channel token 鉴权，但 iframe 仍必须携带独立 nonce 并只向
        请求方声明的本地工作台来源发送受限消息。会话随 websocket 断开、显式关闭或
        超时失效，返回值不包含令牌、DOM、账户或插件运行数据。

        :param session: 已鉴权的 Dev Channel 会话
        :param params: 必须包含 parentOrigin 的预览会话参数
        :return: nonce、过期时间和带会话参数的启动器前端 URL
        :raises DevChannelError: 前端未托管、来源非法或会话数量已达上限时抛出
        """

        if self.frontend_url is None:
            raise DevChannelError("NOT_READY", "启动器前端预览尚未就绪")
        parent_origin = self._preview_parent_origin(params)
        self._purge_preview_sessions()
        if len(self._preview_sessions) >= self.preview_session_limit:
            raise DevChannelError("NOT_READY", "预览会话数量已达上限")
        nonce = secrets.token_urlsafe(24)
        preview_session = _PreviewSession(
            nonce=nonce,
            parent_origin=parent_origin,
            expires_at=monotonic() + self.preview_session_ttl_seconds,
        )
        self._preview_sessions[nonce] = preview_session
        session.preview_nonces.add(nonce)
        parsed_url = urlsplit(self.frontend_url)
        preview_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, parsed_url.path, urlencode({"eclPreviewSession": nonce}), "")
        )
        return {
            "nonce": nonce,
            "url": preview_url,
            "expiresInSeconds": self.preview_session_ttl_seconds,
        }

    def _method_preview_session_close(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        """
        显式关闭当前连接创建的预览会话。

        :param session: 已鉴权的 Dev Channel 会话
        :param params: 必须包含本连接创建的 nonce
        :return: 已关闭的 nonce
        :raises DevChannelError: nonce 缺失或不属于当前连接时抛出
        """

        nonce = self._require_param_str(params, "nonce")
        if nonce not in session.preview_nonces:
            raise DevChannelError("INVALID_PARAMS", "预览会话不存在或不属于当前连接")
        session.preview_nonces.discard(nonce)
        self._preview_sessions.pop(nonce, None)
        return {"nonce": nonce}

    @staticmethod
    def _preview_parent_origin(params: dict[str, Any]) -> str:
        """
        校验工作台父页面来源，只接受本机 HTTP(S) 或 tauri://localhost。

        :param params: preview.session.create 的参数对象
        :return: 无路径、查询和片段的标准化来源
        :raises DevChannelError: 来源不是安全的本地开发工具 origin 时抛出
        """

        origin = DevChannelService._require_param_str(params, "parentOrigin")
        parsed = urlsplit(origin)
        has_path = parsed.path not in {"", "/"} or bool(parsed.query) or bool(parsed.fragment)
        loopback_hosts = {"127.0.0.1", "localhost", "tauri.localhost"}
        is_local_http = parsed.scheme in {"http", "https"} and parsed.hostname in loopback_hosts
        is_tauri = parsed.scheme == "tauri" and parsed.netloc == "localhost"
        if has_path or not (is_local_http or is_tauri):
            raise DevChannelError("INVALID_PARAMS", "parentOrigin 必须是本机工作台来源")
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    def _purge_preview_sessions(self) -> None:
        """
        清除过期会话，避免不再使用的 nonce 占用有限预览槽位。
        """

        now = monotonic()
        expired_nonces = [nonce for nonce, item in self._preview_sessions.items() if item.expires_at <= now]
        for nonce in expired_nonces:
            self._preview_sessions.pop(nonce, None)

    def _method_launcher_info(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 返回启动器基本信息，供工具箱展示当前接入的运行实例。
        return {
            "version": self._launcher_version,
            "pid": os.getpid(),
            "debug": self._debug,
            "devChannel": True,
            "dataPath": str(self._data_path),
            "pluginDir": str(self._data_path / "plugins"),
            "frontendUrl": self.frontend_url,
        }

    def _method_plugin_list(self, session: _Session, params: dict[str, Any]) -> list[dict[str, Any]]:
        # 转换插件管理器条目为协议的驼峰字段结构。
        return [
            {
                "name": entry.get("name", ""),
                "title": entry.get("title", ""),
                "version": entry.get("version", ""),
                "description": entry.get("description", ""),
                "author": entry.get("author", ""),
                "status": entry.get("status", ""),
                "error": entry.get("error"),
                "dependencies": entry.get("dependencies") or {},
                "isSystem": bool(entry.get("is_system", False)),
                "pendingRestart": bool(entry.get("pending_restart", False)),
                "installedVersion": entry.get("installed_version", entry.get("version", "")),
            }
            for entry in self._plugins.list_plugins()
        ]

    def _run_plugin_action(self, name: str, action: Callable[[], PluginActionResult]) -> None:
        # 执行插件操作并把失败状态映射为协议错误码。
        result = action()
        if result.success:
            return
        if result.status == "not_found":
            raise DevChannelError("PLUGIN_NOT_FOUND", result.message or f"插件不存在: {name}")
        raise DevChannelError("INTERNAL_ERROR", result.message or f"插件操作失败: {name}")

    async def _method_plugin_enable(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 启用指定插件。
        name = self._require_param_str(params, "name")
        await asyncio.to_thread(self._run_plugin_action, name, lambda: self._plugins.enable(name))
        return {"name": name}

    async def _method_plugin_disable(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 禁用指定插件。
        name = self._require_param_str(params, "name")
        await asyncio.to_thread(self._run_plugin_action, name, lambda: self._plugins.disable(name))
        return {"name": name}

    async def _method_plugin_reload(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 重载指定插件，是工具箱热重载的核心入口。
        name = self._require_param_str(params, "name")
        await asyncio.to_thread(self._run_plugin_action, name, lambda: self._plugins.reload(name))
        return {"name": name}

    async def _method_plugin_unload(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 卸载指定插件。
        name = self._require_param_str(params, "name")
        await asyncio.to_thread(self._run_plugin_action, name, lambda: self._plugins.unload(name))
        return {"name": name}

    async def _method_plugin_install(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 目录安装保持兼容；归档准备和激活在线程中完成。
        source_path = self._require_param_str(params, "path")
        if source_path.lower().endswith(".eclplugin"):
            result = await asyncio.to_thread(
                self._plugins.install,
                source_path,
                confirm_unverified_source=params.get("confirm_unverified_source") is True,
                allow_network=params.get("allow_network") is True,
            )
        else:
            result = await asyncio.to_thread(self._plugins.install, source_path)
        if not result.success:
            if result.status == "not_found":
                raise DevChannelError("PLUGIN_NOT_FOUND", result.message or "插件不存在")
            raise DevChannelError("INTERNAL_ERROR", result.message or f"插件安装失败: {source_path}")
        return {"name": result.plugin_name, "status": result.status, "message": result.message}

    async def _method_plugin_inspect(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 只读归档预检，不下载运行时或运行插件代码。
        source_path = self._require_param_str(params, "path")
        try:
            preflight = await asyncio.to_thread(self._plugins.inspect_package, source_path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise DevChannelError("INVALID_PARAMS", str(exc)) from exc
        return asdict(preflight)

    def _method_plugin_call_command(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 调用插件命令，命令执行失败按内部错误返回。
        command = self._require_param_str(params, "command")
        command_params = params.get("params") or {}
        if not isinstance(command_params, dict):
            raise DevChannelError("INVALID_PARAMS", "参数 params 必须为对象")
        timeout = params.get("timeout", 30.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            timeout = 30.0
        try:
            result = self._plugins.call_command(command, command_params, timeout=float(timeout))
        except PluginCommandError as exc:
            raise DevChannelError("INTERNAL_ERROR", str(exc)) from exc
        return {"result": result}

    def _method_plugin_get_settings(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 返回插件设置的结构与当前值。
        name = self._require_param_str(params, "name")
        if self._plugins.get_plugin(name) is None:
            raise DevChannelError("PLUGIN_NOT_FOUND", f"插件不存在: {name}")
        settings = self._plugins.get_settings(name)
        return {"schema": settings.get("schema") or [], "values": settings.get("values") or {}}

    def _method_logs_subscribe(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 开启日志推送并返回最近缓存的历史日志。
        session.log_subscribed = True
        return {"history": list(self._log_history)}

    def _method_logs_unsubscribe(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 关闭日志推送。
        session.log_subscribed = False
        return {}

    def _method_events_subscribe(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 按白名单前缀订阅事件总线，忽略重复订阅与非白名单名称。
        events = params.get("events")
        if not isinstance(events, list) or not all(isinstance(item, str) for item in events):
            raise DevChannelError("INVALID_PARAMS", "参数 events 必须为字符串数组")
        subscribed = []
        for event in events:
            if not event.startswith(self.event_prefix_whitelist):
                continue
            if event in session.event_unsubscribers:
                subscribed.append(event)
                continue
            session.event_unsubscribers[event] = self._events.subscribe(
                event,
                self._make_event_forwarder(session, event),
                owner="DevChannelService",
            )
            subscribed.append(event)
        return {"subscribed": subscribed}

    def _method_events_unsubscribe(self, session: _Session, params: dict[str, Any]) -> dict[str, Any]:
        # 退订指定事件，未订阅的名称直接忽略。
        events = params.get("events")
        if not isinstance(events, list) or not all(isinstance(item, str) for item in events):
            raise DevChannelError("INVALID_PARAMS", "参数 events 必须为字符串数组")
        unsubscribed = []
        for event in events:
            unsubscribe = session.event_unsubscribers.pop(event, None)
            if unsubscribe is not None:
                unsubscribe()
                unsubscribed.append(event)
        return {"unsubscribed": unsubscribed}

    def _make_event_forwarder(self, session: _Session, event: str) -> Callable[..., None]:
        # 构造事件转发器：单个载荷保持原值，多参数载荷退化为列表。
        def forward(*args: Any, **kwargs: Any) -> None:
            if kwargs:
                data = kwargs
            elif len(args) == 1:
                data = args[0]
            else:
                data = list(args)
            self._notify(session, event, data)

        return forward


__all__ = ["DevChannelError", "DevChannelService"]
