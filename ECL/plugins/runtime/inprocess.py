# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为系统插件提供进程内运行桥：无管道直连宿主 broker、
#   受信入口装载与回调分派，与 Worker 管道 RPC 保持同一应答信封。
#
# 公开接口：
#   - class InProcessPeer — 实现 HostClient 所需 call 接口的进程内对端。
#   - load_entry_module(root_path, entry_point) — 校验包内入口并执行模块。
#   - instantiate_plugin(module, class_name, host) — 校验并构造统一插件实例。
#   - invoke_plugin_callback(plugin, params) — 按回调标识分派插件回调。
# ============================================================

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from types import ModuleType
from typing import TYPE_CHECKING
from uuid import uuid4

from ECL.plugins.contracts import JsonValue, ProtocolError
from ECL.plugins.sdk._rpc import RpcRequest
from ECL.utils import get_logger

if TYPE_CHECKING:
    from ECL.plugins.sdk.plugin import Plugin


class InProcessPeer:
    """
    实现 HostClient 所需的进程内对端：把 SDK 调用直接交给宿主处理器。

    应答信封与管道 RPC 一致（ok/data/error），插件代码不感知执行形态差异；
    未预期异常只保留稳定错误码，不把宿主堆栈泄漏给插件。
    """

    def __init__(self, handler: Callable[[RpcRequest], dict[str, JsonValue]]) -> None:
        """
        绑定当前系统插件运行的宿主请求处理器。

        :param handler: 已绑定运行身份的 broker 请求入口
        """
        self._handler = handler

    def call(
        self,
        method: str,
        params: dict[str, JsonValue],
        timeout: float,
        *,
        mutation_key: str = "",
    ) -> dict[str, JsonValue]:
        """
        以本地截止时间构造请求并同步执行，信封语义与 RpcPeer 一致。

        :param method: 已定义的 Host API 名称
        :param params: 有界 JSON 输入
        :param timeout: 调用等待总预算
        :param mutation_key: 本运行内的提交去重键
        :return: ok/data 或 ok/error 应答信封
        """
        request = RpcRequest(method, params, monotonic() + timeout, mutation_key=mutation_key, request_id=uuid4().hex)
        try:
            return {"ok": True, "data": self._handler(request)}
        except ProtocolError as exc:
            return {"ok": False, "error": {"code": exc.error_code}}
        except Exception:
            get_logger("PluginInProcess").exception("插件宿主调用发生未预期异常")
            return {"ok": False, "error": {"code": "PLUGIN_HOST_FAILED"}}

    def close(self) -> None:
        """
        进程内对端没有独立资源，仅为与 RpcPeer 接口对称而保留。
        """


def load_entry_module(root_path: Path, entry_point: str) -> tuple[ModuleType, str]:
    """
    校验清单入口位于插件目录内并执行模块，返回模块与入口类名。

    使用隔离模块名避免与宿主或其他系统插件命名冲突；入口代码与本进程
    共享解释器，仅适用于内置可信分发。

    :param root_path: 系统插件代码目录
    :param entry_point: 清单声明的 ``模块:类名`` 入口
    :return: 已执行的模块与入口类名
    :raises ProtocolError: 入口格式、越界或文件缺失时抛出
    """
    module_name, _, class_name = entry_point.partition(":")
    class_name = class_name or "Plugin"
    if (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*", module_name) is None
        or not class_name.isidentifier()
    ):
        raise ProtocolError("插件入口必须是包内 Python 文件和合法类名", "PLUGIN_ENTRY_INVALID")
    main_py = root_path / f"{module_name}.py"
    if not main_py.resolve().is_relative_to(root_path.resolve()):
        raise ProtocolError("插件入口文件超出插件目录", "PLUGIN_ENTRY_INVALID")
    if not main_py.is_file():
        raise ProtocolError("插件入口文件不存在", "PLUGIN_ENTRY_INVALID")
    spec = importlib.util.spec_from_file_location(f"plugin_inprocess_{uuid4().hex[:12]}", main_py)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    source = spec.loader.get_source(spec.name)
    exec(compile(source, str(main_py), "exec", dont_inherit=True), module.__dict__)
    return module, class_name


def instantiate_plugin(module: ModuleType, class_name: str, host: object) -> Plugin:
    """
    校验入口类继承统一插件基类并构造实例。

    :param module: 已执行的插件模块
    :param class_name: 清单声明的入口类名
    :param host: 已绑定进程内对端的 HostClient
    :return: 就绪的插件实例
    :raises ProtocolError: 入口类不是统一插件基类时抛出
    """
    from ECL.plugins.sdk.plugin import Plugin

    plugin_class = getattr(module, class_name, None)
    if not isinstance(plugin_class, type) or not issubclass(plugin_class, Plugin):
        raise ProtocolError("插件入口不是统一插件基类", "PLUGIN_ENTRY_INVALID")
    plugin = plugin_class(host)
    if not isinstance(plugin, Plugin):
        raise ProtocolError("插件构造结果不满足统一插件基类", "PLUGIN_ENTRY_INVALID")
    return plugin


def invoke_plugin_callback(plugin: Plugin, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """
    按回调标识分派插件回调，语义与 Worker 侧 callbacks.invoke 一致。

    :param plugin: 当前运行的插件实例
    :param params: 含 callback_ref 与 args 的宿主回调请求
    :return: 含 value 的应答
    :raises ProtocolError: 参数结构或回调标识无效时抛出
    """
    reference, arguments = params.get("callback_ref"), params.get("args")
    if set(params) != {"callback_ref", "args"} or not isinstance(reference, str) or not isinstance(arguments, dict):
        raise ProtocolError("插件回调参数无效")
    return {"value": plugin._invoke_callback(reference, arguments)}
