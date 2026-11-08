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
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock
from time import monotonic
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, ValidationError

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value
from ECL.plugins.run_models import PluginLease
from ECL.plugins.run_store import PluginRunError
from ECL.plugins.runtime.custom_ui_models import CustomUiDeclaration, CustomUiRequest
from ECL.plugins.runtime.models import RequestModel
from ECL.utils import get_logger

if TYPE_CHECKING:
    from pytaurix import AppHandle
    from pytaurix.webview import WebviewWindow

    from ECL.plugins.runtime.broker import PluginHostBroker


class _WindowQuery(RequestModel):
    window_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


@dataclass(slots=True)
class _Window:
    lease: PluginLease
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
            self._broker.session.lease()
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
        请求创建原生隔离窗口；隔离原语不可用时按契约显式拒绝。

        pytaurix 0.1.0a1 尚未提供顶层导航锁定与消息 URI 拦截等隔离原语，
        旧自维护 wheel 的 install_plugin_isolation 已随运行时退役。
        在等价策略能力落地并重新验收前，不退回无隔离的窗口实现。
        """
        self._broker.session.lease()
        raise ProtocolError("原生隔离策略在当前运行时不可用", "PLUGIN_UI_ISOLATION_UNAVAILABLE")

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
        except (ProtocolError, PluginRunError, ValidationError, ValueError, RuntimeError):
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
                except (ProtocolError, PluginRunError) as exc:
                    response = {"id": query.id, "ok": False, "error_code": exc.error_code}
                self._check(window)
                with self._lock:
                    window.requests[query.id] = (fingerprint, response)
            self._reply(window, response)
        except (ProtocolError, PluginRunError, ValidationError) as exc:
            code = exc.error_code if isinstance(exc, ProtocolError | PluginRunError) else "PLUGIN_SCHEMA_INVALID"
            # 关闭屏障后不再向已经销毁的页面交付结果。
            with suppress(ProtocolError, PluginRunError, RuntimeError):
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

    def refresh_leases(self) -> None:
        """
        原始授权失效后关闭对应窗口，不为旧页面更换准入；未销毁窗口继续重试关闭。
        """
        with self._lock:
            windows = tuple(self._windows.values())
        for window in windows:
            try:
                self._check(window)
            except (ProtocolError, PluginRunError):
                if not window.destroyed.is_set():
                    self._close_window(window)

    def close(self) -> None:
        """
        撤销本运行所有窗口准入并停止新消息；实际销毁通过 is_idle 继续确认。
        """
        with self._lock:
            self._closed = True
            executor = self._executor
        self.refresh_leases()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def is_idle(self) -> bool:
        """
        所有原生窗口实际销毁后才允许运行服务释放本轮资源。

        :return: 不再持有未销毁原生窗口
        """
        self.refresh_leases()
        with self._lock:
            return all(window.destroyed.is_set() for window in self._windows.values())
