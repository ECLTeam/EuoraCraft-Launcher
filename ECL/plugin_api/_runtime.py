# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在 Worker 内执行 v2 生命周期与本地回调，不创建宿主业务服务。
#
# 公开接口：
#   - class PluginRuntime — 经许可模块的受控生命周期和回调分派。
# ============================================================

from __future__ import annotations

import asyncio
import inspect
import os
from threading import RLock
from types import ModuleType

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugin_api.plugin import HostClient, Plugin


class PluginRuntime:
    """
    使用独立接收线程外的执行边界运行同步或异步插件，不接受任意函数名调用。
    """

    def __init__(self, module: ModuleType, entry_class: str, host: HostClient) -> None:
        """
        绑定已经过内容许可的模块，构造插件及钩子须等宿主生命周期请求。

        :param module: 已在当前 Worker 中执行的模块
        :param entry_class: 宿主选择并校验的 v2 插件类
        :param host: 当前运行的反向 RPC 门面
        """
        self._module = module
        self._entry_class = entry_class
        self._host = host
        self._plugin: Plugin | None = None
        self._phase = "loaded"
        self._is_lifecycle_busy = False
        self._lock = RLock()

    def handle(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        分派已实现的控制或回调方法，不向宿主动态转发任意方法。

        :param request: 当前认证连接的调用上下文
        :return: 生命周期、回调或运行信息的纯 JSON 结果
        :raises ProtocolError: 方法、状态或参数不允许时抛出
        """
        request.check_deadline()
        if request.method == "runtime.info" and not request.params:
            with self._lock:
                return {"api_version": 2, "pid": os.getpid(), "run_state": self._phase}
        if request.method == "lifecycle.invoke":
            return self._lifecycle(request)
        if request.method == "callbacks.invoke":
            return self._callback(request)
        raise ProtocolError("此 Worker 方法尚未实现", "PLUGIN_API_METHOD_NOT_FOUND")

    def _callback(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        快速选取本实例回调后释放状态锁，允许用户回调中的嵌套双向调用。
        """
        params = request.params
        reference, arguments = params.get("callback_ref"), params.get("args")
        if set(params) != {"callback_ref", "args"} or not isinstance(reference, str) or not isinstance(arguments, dict):
            raise ProtocolError("插件回调参数无效")
        with self._lock:
            plugin = self._plugin
            if plugin is None or self._phase not in {"loaded", "enabling", "enabled"}:
                raise ProtocolError("插件回调运行状态已经失效", "PLUGIN_CALLBACK_EXPIRED")
        request.check_deadline()
        return {"value": plugin._invoke_callback(reference, arguments)}

    def _lifecycle(self, request: RpcRequest) -> dict[str, JsonValue]:
        """
        生命周期状态变更串行，但不在锁内运行插件代码，避免嵌套请求死锁。
        """
        params = request.params
        phase, previous_phase = params.get("phase"), params.get("run_state")
        if (
            set(params) != {"phase", "run_state"}
            or not isinstance(phase, str)
            or not isinstance(previous_phase, str)
            or phase
            not in {
                "load",
                "enable",
                "frontend_ready",
                "disable",
                "unload",
            }
        ):
            raise ProtocolError("插件生命周期参数无效")
        with self._lock:
            if self._is_lifecycle_busy or previous_phase != self._phase:
                raise ProtocolError("插件生命周期版本或顺序失效", "PLUGIN_SESSION_EXPIRED")
            self._validate_phase(str(phase))
            self._is_lifecycle_busy = True
            if phase != "frontend_ready":
                self._phase = self._running_phase(phase)
        try:
            if phase == "load":
                plugin_class = getattr(self._module, self._entry_class, None)
                if not isinstance(plugin_class, type) or not issubclass(plugin_class, Plugin):
                    raise ProtocolError("插件入口不是 API v2 基类", "PLUGIN_API_VERSION_UNSUPPORTED")
                plugin = plugin_class(self._host)
                if not isinstance(plugin, Plugin):
                    raise ProtocolError("插件构造结果不满足 API v2", "PLUGIN_API_VERSION_UNSUPPORTED")
                with self._lock:
                    self._plugin = plugin
            else:
                plugin = self._plugin
                assert plugin is not None
            request.check_deadline()
            result = getattr(plugin, "on_" + str(phase))()
            if inspect.isawaitable(result):
                result = asyncio.run(Plugin._await_result(result))
            if result is not None:
                raise ProtocolError("插件生命周期必须返回 None", "PLUGIN_CALLBACK_RESULT_INVALID")
            request.check_deadline()
            with self._lock:
                self._phase = self._completed_phase(str(phase), str(previous_phase))
                return {"completed": True, "run_state": self._phase}
        except BaseException:
            with self._lock:
                self._phase = "failed"
            raise
        finally:
            with self._lock:
                self._is_lifecycle_busy = False

    def _validate_phase(self, phase: str) -> None:
        """
        不允许构造重复实例或从停止状态继续执行旧回调。
        """
        if phase == "load":
            is_valid = self._plugin is None and self._phase == "loaded"
        elif phase == "enable":
            is_valid = self._plugin is not None and self._phase in {"loaded", "disabled"}
        elif phase in {"frontend_ready", "disable"}:
            is_valid = self._plugin is not None and self._phase == "enabled"
        else:
            is_valid = self._plugin is not None and self._phase in {"loaded", "disabled"}
        if not is_valid:
            raise ProtocolError("插件生命周期顺序不允许", "PLUGIN_SESSION_EXPIRED")

    @staticmethod
    def _running_phase(phase: str) -> str:
        if phase == "load":
            return "loading"
        if phase == "enable":
            return "enabling"
        if phase == "disable":
            return "disabling"
        return "unloading"

    @staticmethod
    def _completed_phase(phase: str, previous_phase: str) -> str:
        if phase == "enable":
            return "enabled"
        if phase == "disable":
            return "disabled"
        if phase == "unload":
            return "unloaded"
        return previous_phase
