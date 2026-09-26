# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在插件专用 Python 环境内加载单个插件，并通过本地 JSON 消息执行调用。
#
# 公开接口：
#   - main(argv) -> int — 连接宿主 IPC 并处理有界的 Worker 请求。
# ============================================================

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from contextlib import suppress
from multiprocessing.connection import Client, Connection
from pathlib import Path

max_frame_bytes = 1024**2
protocol_version = 1
identifier_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _message(connection: Connection, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_frame_bytes:
        encoded = json.dumps(
            {
                "version": protocol_version,
                "request_id": payload.get("request_id"),
                "success": False,
                "error": "响应过大",
            }
        ).encode("utf-8")
    connection.send_bytes(encoded)


def _load_plugin(code_path: Path) -> object:
    metadata_path = code_path / "plugin.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("plugin.json 必须是 JSON 对象")
    entry_point = metadata.get("entry_point", "main:Plugin")
    if not isinstance(entry_point, str):
        raise ValueError("插件入口无效")
    parts = entry_point.split(":", 1)
    module_name = parts[0]
    class_name = parts[1] if len(parts) == 2 else "Plugin"
    if identifier_pattern.fullmatch(module_name) is None or identifier_pattern.fullmatch(class_name) is None:
        raise ValueError("插件入口仅支持模块名:类名")
    module_path = code_path / f"{module_name}.py"
    if not module_path.is_file():
        raise FileNotFoundError("插件入口文件不存在")
    spec = importlib.util.spec_from_file_location(f"ecl_worker_plugin_{module_name}", module_path)
    if spec is None or spec.loader is None:
        raise ImportError("插件入口模块无法加载")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(code_path))
    spec.loader.exec_module(module)
    plugin_class = getattr(module, class_name)
    return plugin_class()


def _dispatch_call(plugin: object, request: dict[str, object]) -> object:
    method_name = request.get("method")
    args = request.get("args", [])
    kwargs = request.get("kwargs", {})
    if (
        not isinstance(method_name, str)
        or identifier_pattern.fullmatch(method_name) is None
        or method_name.startswith("_")
        or not isinstance(args, list)
        or not isinstance(kwargs, dict)
        or any(not isinstance(key, str) for key in kwargs)
    ):
        raise ValueError("Worker 方法调用参数无效")
    return getattr(plugin, method_name)(*args, **kwargs)


def _call_hook(plugin: object, hook_name: str) -> None:
    hook = getattr(plugin, hook_name, None)
    if hook is not None:
        hook()


class _WorkerState:
    def __init__(self) -> None:
        self.plugin: object | None = None
        self.enabled = False

    def dispatch(self, request: dict[str, object]) -> object:
        operation = request.get("op")
        if operation == "load":
            if self.plugin is not None:
                raise ValueError("插件已加载")
            raw_path = request.get("code_path")
            if not isinstance(raw_path, str):
                raise ValueError("插件代码路径无效")
            candidate = _load_plugin(Path(raw_path))
            try:
                _call_hook(candidate, "on_load")
            except Exception:
                with suppress(Exception):
                    _call_hook(candidate, "on_unload")
                raise
            self.plugin = candidate
            return None
        if self.plugin is None:
            raise ValueError("插件尚未加载")
        if operation == "enable":
            if not self.enabled:
                _call_hook(self.plugin, "on_enable")
                self.enabled = True
            return None
        if operation == "disable":
            if self.enabled:
                _call_hook(self.plugin, "on_disable")
                self.enabled = False
            return None
        if operation == "call":
            return _dispatch_call(self.plugin, request)
        raise ValueError("未知 Worker 操作")

    def close(self) -> None:
        if self.plugin is None:
            return
        try:
            if self.enabled:
                _call_hook(self.plugin, "on_disable")
        finally:
            _call_hook(self.plugin, "on_unload")


def _serve(connection: Connection) -> None:
    state = _WorkerState()
    while True:
        try:
            request = json.loads(connection.recv_bytes(max_frame_bytes).decode("utf-8"))
        except EOFError:
            return
        if not isinstance(request, dict) or request.get("version") != protocol_version:
            return
        request_id = request.get("request_id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return
        operation = request.get("op")
        try:
            if operation == "close":
                state.close()
                result = None
            else:
                result = state.dispatch(request)
            _message(
                connection, {"version": protocol_version, "request_id": request_id, "success": True, "result": result}
            )
        except Exception as exc:
            _message(
                connection,
                {
                    "version": protocol_version,
                    "request_id": request_id,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        if operation == "close":
            return


def main(argv: list[str] | None = None) -> int:
    """
    使用标准库连接宿主的命名管道或 Unix 域套接字并处理 JSON 请求。

    不使用 pickle；连接断开、帧超限或协议错误时立即退出。Worker 是依赖隔离，
    不是文件、剪贴板或网络权限沙箱。

    :param argv: 可选命令行参数
    :return: 正常关闭时返回 0
    """
    parser = argparse.ArgumentParser(description="EuoraCraft 插件 Worker")
    parser.add_argument("--address", required=True)
    parser.add_argument("--family", required=True, choices=("AF_PIPE", "AF_UNIX"))
    parser.add_argument("--authkey", required=True)
    args = parser.parse_args(argv)
    authkey = bytes.fromhex(args.authkey)
    with Client(args.address, family=args.family, authkey=authkey) as connection:
        _serve(connection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
