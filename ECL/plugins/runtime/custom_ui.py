# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：管理当前 Worker 的专属原生前端窗口、消息准入及退出收尾。
#
# 公开接口：
#   - class RuntimeCustomUi — 固定运行时、内容快照与原始许可绑定的窗口控制。
# ============================================================

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import Field, ValidationError

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.runtime.custom_ui_models import CustomUiDeclaration, CustomUiRequest
from ECL.plugins.runtime.custom_ui_shell import build_custom_ui_shell
from ECL.plugins.runtime.models import RequestModel
from ECL.utils import get_logger

if TYPE_CHECKING:
    from pytauri import AppHandle
    from pytauri.ffi.webview import WebviewWindow

    from ECL.plugins.runtime.broker import PluginHostBroker


class _WindowQuery(RequestModel):
    window_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


@dataclass(slots=True)
class _Window:
    lease: GrantLease
    secret: str = field(default_factory=lambda: secrets.token_hex(32))
    webview: WebviewWindow | None = None
    ready: Event = field(default_factory=Event)
    destroyed: Event = field(default_factory=Event)
    is_closed: bool = False
    error_code: str | None = None
    requests: dict[str, tuple[str, dict[str, JsonValue] | None]] = field(default_factory=dict)


class RuntimeCustomUi:
    """
    不把第三方 JS 放入主窗口，只经本窗口原生桥调用自身命令、设置和任务控制。

    消息处理不在 WebView STA 上等待 Worker；关闭先撤销窗口准入，再请求关闭原生窗口。
    """

    bridge_methods: ClassVar[frozenset[str]] = frozenset(
        {
            "runtime.info",
            "permissions.list",
            "commands.call",
            "settings.get",
            "settings.set",
            "operations.get",
            "operations.list",
            "operations.cancel",
        }
    )

    def __init__(
        self,
        broker: PluginHostBroker,
        declaration: CustomUiDeclaration | None,
        data_path: Path,
        app_handle: Callable[[], AppHandle | None],
    ) -> None:
        """
        绑定清单声明及应用句柄的明确提供方，不接受插件上报的原生窗口或所有者。

        :param broker: 当前 Worker 的唯一授权与业务门面
        :param declaration: 审阅快照中的前端声明
        :param data_path: 宿主分配的应用私有目录
        :param app_handle: 真实 Tauri 启动完成后提供原生应用句柄
        """
        self._broker = broker
        self._declaration = declaration
        self._data_path = data_path
        self._app_handle = app_handle
        self._lock = RLock()
        self._windows: dict[str, _Window] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._slots = BoundedSemaphore(4)
        self._closed = False

    def _check(self, window: _Window) -> None:
        with self._lock:
            if self._closed or window.is_closed:
                raise ProtocolError("插件窗口已关闭", "PLUGIN_SESSION_EXPIRED")
        self._broker.session.check(window.lease)

    def handle(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """
        创建或控制自身随机窗口引用，不接纳 label、路径、HTML 或脚本参数。

        :param method: open、get、focus 或 close
        :param params: 空创建参数或自身 window_ref
        :return: 创建及实际销毁状态
        :raises ProtocolError: 输入、来源、许可、内容或原生运行时不可用时抛出
        """
        if method == "ui.windows.open":
            RequestModel.model_validate(params)
            return self._open()
        query = _WindowQuery.model_validate(params)
        with self._lock:
            window = self._windows.get(query.window_ref)
        if window is None:
            raise ProtocolError("未找到自身窗口", "PLUGIN_RESOURCE_NOT_FOUND")
        if method == "ui.windows.get":
            self._broker.session.authorize("plugin.base")
        else:
            self._check(window)
        if method == "ui.windows.close":
            self._close_window(window)
        elif method == "ui.windows.focus":
            webview = window.webview
            if webview is not None:
                webview.show()
                webview.set_focus()
        elif method != "ui.windows.get":
            raise ProtocolError("窗口方法尚未定义", "PLUGIN_API_METHOD_NOT_FOUND")
        return self._view(query.window_ref, window)

    @staticmethod
    def _view(reference: str, window: _Window) -> dict[str, JsonValue]:
        return {
            "window_ref": reference,
            "ready": window.ready.is_set() and not window.is_closed,
            "closing": window.is_closed and not window.destroyed.is_set(),
            "closed": window.destroyed.is_set(),
            "error_code": window.error_code,
        }

    def _html(self) -> str:
        """
        文件字节须与已批准快照一致，不通过本地 URL 暴露整个代码或宿主目录。
        """
        declaration = self._declaration
        if declaration is None:
            raise ProtocolError("清单未声明自定义前端", "PLUGIN_RESOURCE_NOT_FOUND")
        snapshot = self._broker.session.binding.snapshot
        record = next((item for item in snapshot.files if item.name == declaration.entry_file), None)
        if record is None or record.size > 524288:
            raise ProtocolError("前端入口不在批准内容内或超过上限", "PLUGIN_CONTENT_CHANGED")
        try:
            with (snapshot.root_path / declaration.entry_file).open("rb") as source:
                raw = source.read(record.size + 1)
            if len(raw) != record.size or hashlib.sha256(raw).hexdigest() != record.sha256:
                raise ProtocolError("前端内容已经变化", "PLUGIN_CONTENT_CHANGED")
            return raw.decode("utf-8")
        except (OSError, UnicodeError):
            raise ProtocolError("前端文件不能读取", "PLUGIN_CONTENT_CHANGED") from None

    def _open(self) -> dict[str, JsonValue]:
        """
        空白原生窗口先安装拒绝策略，再加载受信壳；初始化失败不执行插件代码。
        """
        lease = self._broker.session.authorize("ui.custom.execute")
        try:
            if version("pytauri-wheel") != "0.8.0+ecl.1":
                raise ProtocolError("当前运行时不提供插件隔离", "PLUGIN_UI_ISOLATION_UNAVAILABLE")
        except PackageNotFoundError:
            raise ProtocolError("缺少固定原生运行时", "PLUGIN_UI_ISOLATION_UNAVAILABLE") from None
        app = self._app_handle()
        if app is None:
            raise ProtocolError("原生应用尚未就绪", "PLUGIN_UI_ISOLATION_UNAVAILABLE")
        html = self._html()
        reference, window = uuid4().hex, _Window(lease)
        shell_url = build_custom_ui_shell(html, window.secret)
        with self._lock:
            self._check(window)
            if len(self._windows) >= 16 or sum(not item.destroyed.is_set() for item in self._windows.values()) >= 2:
                raise ProtocolError("当前运行窗口已达上限", "PLUGIN_RATE_LIMITED")
            self._windows[reference] = window
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="plugin_ui")
        try:
            self._create(window, reference, app, shell_url)
            if not window.ready.wait(10):
                raise ProtocolError("原生插件窗口初始化超时", "PLUGIN_UI_ISOLATION_UNAVAILABLE")
            if window.error_code:
                raise ProtocolError("原生插件窗口初始化失败", window.error_code)
            self._check(window)
        except (ProtocolError, PluginGrantError):
            self._close_window(window)
            raise
        except Exception:
            self._close_window(window)
            get_logger("RuntimeCustomUi").exception("插件原生窗口初始化失败")
            raise ProtocolError("原生隔离不可用", "PLUGIN_UI_ISOLATION_UNAVAILABLE") from None
        return self._view(reference, window)

    def _create(self, window: _Window, reference: str, app: AppHandle, shell_url: str) -> None:
        """
        使用固定绑定契约创建独立浏览器配置，主窗口和其他插件不共用该目录。
        """
        from pytauri import WebviewUrl, WindowEvent
        from pytauri.ffi.webview import WebviewWindowBuilder

        assert self._declaration is not None
        profile = (self._data_path / "plugin_webviews" / self._broker.session.run_id / reference).resolve()
        profile.mkdir(parents=True, exist_ok=False)
        webview = WebviewWindowBuilder.build(
            app,
            "plugin-v2-" + reference,
            WebviewUrl.External("about:blank"),
            plugin_isolation=True,
            visible=False,
            data_directory=profile,
            decorations=True,
            title=self._declaration.title,
            inner_size=(self._declaration.width, self._declaration.height),
            min_inner_size=(480, 360),
            devtools=False,
        )
        window.webview = webview

        def event(value: object) -> None:
            if isinstance(value, WindowEvent.CloseRequested):
                window.is_closed = True
            elif isinstance(value, WindowEvent.Destroyed):
                window.is_closed = True
                window.webview = None
                window.requests.clear()
                window.destroyed.set()
                window.ready.set()

        def ready(success: bool) -> None:
            try:
                self._check(window)
                if not success:
                    raise ProtocolError("原生策略未能安装", "PLUGIN_UI_ISOLATION_UNAVAILABLE")
                webview.navigate(shell_url)
            except (ProtocolError, PluginGrantError):
                window.error_code = "PLUGIN_UI_ISOLATION_UNAVAILABLE"
                window.ready.set()
                self._close_window(window)

        webview.on_window_event(event)
        webview.install_plugin_isolation(shell_url, lambda uri: self._message(window, uri), ready)

    def _message(self, window: _Window, uri: str) -> None:
        """
        原生已确认顶层文档；STA 只做有限校验与排队，不等待插件回调。
        """
        try:
            self._check(window)
            if uri == "ecl-plugin-ready://loaded":
                webview = window.webview
                if webview is not None:
                    webview.eval("globalThis.__eclStart()")
                    webview.show()
                window.ready.set()
                return
            parts = urlsplit(uri)
            if (
                parts.scheme != "ecl-plugin-message"
                or parts.netloc != "request"
                or not secrets.compare_digest(parts.path, "/" + window.secret)
            ):
                raise ProtocolError("窗口消息凭据无效", "PLUGIN_RESOURCE_DENIED")
            values = parse_qs(parts.query, strict_parsing=True, max_num_fields=1)
            raw = values.get("payload", [])
            if len(raw) != 1 or len(raw[0].encode()) > 32768:
                raise ProtocolError("窗口消息超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
            query = CustomUiRequest.model_validate(_json_value(json.loads(raw[0])))
            if not self._slots.acquire(blocking=False):
                raise ProtocolError("窗口请求超过上限", "PLUGIN_RATE_LIMITED")
            try:
                assert self._executor is not None
                self._executor.submit(self._execute, window, query)
            except RuntimeError:
                self._slots.release()
                raise
        except (ProtocolError, PluginGrantError, ValidationError, ValueError, RuntimeError):
            window.error_code = "PLUGIN_UI_MESSAGE_REJECTED"
            self._close_window(window)

    def _execute(self, window: _Window, query: CustomUiRequest) -> None:
        """
        窗口主体不可覆盖；同一请求身份不得改变内容，所有变更复用 Broker 去重。
        """
        try:
            self._check(window)
            if query.method == "window.close":
                RequestModel.model_validate(query.params)
                self._close_window(window)
                return
            if query.method not in self.bridge_methods:
                raise ProtocolError("自定义前端无权调用该接口", "PLUGIN_API_METHOD_NOT_FOUND")
            fingerprint = hashlib.sha256(query.model_dump_json().encode()).hexdigest()
            with self._lock:
                previous = window.requests.get(query.id)
                if previous is not None:
                    if previous[0] != fingerprint or previous[1] is None:
                        raise ProtocolError("窗口请求重复或内容改变", "PLUGIN_MUTATION_CONFLICT")
                    response = previous[1]
                else:
                    if len(window.requests) >= 256:
                        raise ProtocolError("窗口请求记录超过上限", "PLUGIN_RATE_LIMITED")
                    window.requests[query.id] = (fingerprint, None)
                    response = None
            if response is None:
                try:
                    value = self._broker.handle(
                        RpcRequest(query.method, query.params, monotonic() + 20, query.mutation_key)
                    )
                    response = {"id": query.id, "ok": True, "data": value}
                except (ProtocolError, PluginGrantError) as exc:
                    response = {"id": query.id, "ok": False, "error_code": exc.error_code}
                self._check(window)
                with self._lock:
                    window.requests[query.id] = (fingerprint, response)
            self._reply(window, response)
        except (ProtocolError, PluginGrantError, ValidationError) as exc:
            code = exc.error_code if isinstance(exc, ProtocolError | PluginGrantError) else "PLUGIN_SCHEMA_INVALID"
            # 关闭屏障后不再向已经销毁的页面交付结果。
            with suppress(ProtocolError, PluginGrantError, RuntimeError):
                self._reply(window, {"id": query.id, "ok": False, "error_code": code})
        except (OSError, RuntimeError):
            get_logger("RuntimeCustomUi").exception("插件独立窗口消息交付失败")
            self._close_window(window)
        finally:
            self._slots.release()

    def _reply(self, window: _Window, response: dict[str, JsonValue]) -> None:
        self._check(window)
        webview = window.webview
        if webview is not None:
            webview.eval("globalThis.__eclReply(" + json.dumps(response, ensure_ascii=True) + ")")

    @staticmethod
    def _close_window(window: _Window) -> None:
        """
        先使凭据失效；原生 Destroyed 事件确认前保留句柄及清理状态。
        """
        window.is_closed = True
        webview = window.webview
        if webview is None:
            window.destroyed.set()
            window.ready.set()
            return
        try:
            webview.close()
        except (OSError, RuntimeError):
            get_logger("RuntimeCustomUi").warning("插件原生窗口尚未确认关闭，保留引用等待重试")

    def refresh_permissions(self) -> None:
        """
        原始授权失效后关闭对应窗口，不为旧页面更换准入；未销毁窗口继续重试关闭。
        """
        with self._lock:
            windows = tuple(self._windows.values())
        for window in windows:
            try:
                self._check(window)
            except (ProtocolError, PluginGrantError):
                if not window.destroyed.is_set():
                    self._close_window(window)

    def close(self) -> None:
        """
        撤销本运行所有窗口准入并停止新消息；实际销毁通过 is_idle 继续确认。
        """
        with self._lock:
            self._closed = True
            executor = self._executor
        self.refresh_permissions()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def is_idle(self) -> bool:
        """
        所有原生窗口实际销毁后才允许运行服务释放本轮资源。

        :return: 不再持有未销毁原生窗口
        """
        self.refresh_permissions()
        with self._lock:
            return all(window.destroyed.is_set() for window in self._windows.values())
