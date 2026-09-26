# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：用插件 venv 启动独立 Worker，并经有界 JSON IPC 调用其生命周期方法。
#
# 公开接口：
#   - class PluginWorkerError — Worker 启动、协议或插件调用失败。
#   - class PluginWorkerCallError — 插件方法失败但 Worker 连接仍然健康。
#   - class PluginWorkerProcess — 启动、调用及关闭单个插件 Worker。
# ============================================================

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
from multiprocessing.connection import Connection, Listener, wait
from pathlib import Path
from uuid import uuid4

max_frame_bytes = 1024**2
startup_timeout_seconds = 15.0
request_timeout_seconds = 30.0
close_timeout_seconds = 5.0
protocol_version = 1


class PluginWorkerError(RuntimeError):
    """
    表示插件 Worker 未能启动、响应超时或返回明确的调用失败。

    Worker 失败不会自动回退到宿主进程执行插件代码。
    """


class PluginWorkerCallError(PluginWorkerError):
    """
    表示插件方法已在 Worker 内失败，连接仍可继续处理后续请求。
    """


class PluginWorkerProcess:
    """
    管理一个插件专用子进程及其本地 IPC 连接。

    该边界只提供独立解释器与故障隔离，不限制插件的 OS 权限。请求串行化，
    任何协议或超时失败都会关闭子进程，避免复用状态未知的 Worker。
    """

    def __init__(self, python_path: Path, worker_path: Path, code_path: Path) -> None:
        """
        指定已准备的 venv Python、随运行时发布的 Worker 脚本和插件代码。

        :param python_path: 插件环境池返回的 Python 可执行文件
        :param worker_path: 固定哈希运行时中已校验的 Worker 脚本
        :param code_path: 已校验但尚未激活的插件代码目录
        """
        self.python_path = Path(python_path)
        self.worker_path = Path(worker_path)
        self.code_path = Path(code_path)
        self._connection: Connection | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._request_lock = threading.Lock()

    def start(self) -> None:
        """
        启动 Worker、完成本地连接并在子进程加载插件模块。

        启动失败会关闭管道并终止子进程，不在宿主解释器导入插件。

        :raises PluginWorkerError: 可执行文件、连接、加载或握手失败时抛出
        """
        if self._process is not None:
            raise PluginWorkerError("Worker 已启动")
        if not self.python_path.is_file() or not self.worker_path.is_file() or not self.code_path.is_dir():
            raise PluginWorkerError("Worker 启动文件或插件代码不存在")
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        address = (
            rf"\\.\pipe\ecl-plugin-{uuid4().hex}"
            if os.name == "nt"
            else str(Path(tempfile.gettempdir()) / f"eclw-{uuid4().hex[:16]}.sock")
        )
        authkey = os.urandom(32)
        listener = Listener(address, family=family, authkey=authkey)
        accepted: list[Connection | BaseException] = []
        accepted_event = threading.Event()

        def accept_connection() -> None:
            try:
                accepted.append(listener.accept())
            except (OSError, EOFError) as exc:
                accepted.append(exc)
            finally:
                accepted_event.set()

        accept_thread = threading.Thread(target=accept_connection, name="plugin_worker_accept", daemon=True)
        try:
            accept_thread.start()
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._process = subprocess.Popen(
                [
                    str(self.python_path),
                    "-I",
                    str(self.worker_path),
                    "--address",
                    address,
                    "--family",
                    family,
                    "--authkey",
                    authkey.hex(),
                ],
                cwd=self.code_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            if (
                not accepted_event.wait(startup_timeout_seconds)
                or not accepted
                or isinstance(accepted[0], BaseException)
            ):
                raise PluginWorkerError("Worker 未能在限定时间内连接")
            self._connection = accepted[0]
            self._request({"op": "load", "code_path": str(self.code_path)})
        except (OSError, PluginWorkerError) as exc:
            self.close()
            if isinstance(exc, PluginWorkerError):
                raise
            raise PluginWorkerError("Worker 启动失败") from exc
        finally:
            listener.close()
            if os.name != "nt":
                Path(address).unlink(missing_ok=True)
            accept_thread.join(timeout=1)

    def _request(self, payload: dict[str, object], timeout: float = request_timeout_seconds) -> object:
        """
        发送有界 JSON 请求并等待匹配请求号的响应，拒绝未知状态。
        """
        connection = self._connection
        if connection is None:
            raise PluginWorkerError("Worker 未连接")
        with self._request_lock:
            self._request_id += 1
            request_id = self._request_id
            request = {"version": protocol_version, "request_id": request_id, **payload}
            try:
                encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(encoded) > max_frame_bytes:
                    raise PluginWorkerError("Worker 请求超过大小上限")
                connection.send_bytes(encoded)
                if not wait([connection], timeout):
                    raise PluginWorkerError("Worker 响应超时")
                response = json.loads(connection.recv_bytes(max_frame_bytes).decode("utf-8"))
                if (
                    not isinstance(response, dict)
                    or response.get("version") != protocol_version
                    or response.get("request_id") != request_id
                    or not isinstance(response.get("success"), bool)
                ):
                    raise PluginWorkerError("Worker 响应协议无效")
                if response["success"] is False:
                    raise PluginWorkerCallError(str(response.get("error", "Worker 调用失败")))
                return response.get("result")
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise PluginWorkerError("Worker 通信失败") from exc

    def call(self, method: str, *args: object, timeout: float = request_timeout_seconds, **kwargs: object) -> object:
        """
        以 JSON 参数调用已加载插件的公开方法。

        超时或连接损坏时终止 Worker，不在宿主进程重试或回退。

        :param method: 插件实例上的公开方法名
        :param args: 可 JSON 序列化的位置参数
        :param timeout: 响应等待秒数
        :param kwargs: 可 JSON 序列化的关键字参数
        :return: 插件方法返回的 JSON 值
        :raises PluginWorkerError: 插件调用、超时或协议失败时抛出
        """
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise PluginWorkerError("Worker 超时必须在 0 到 120 秒之间")
        try:
            return self._request({"op": "call", "method": method, "args": args, "kwargs": kwargs}, timeout)
        except PluginWorkerCallError:
            raise
        except PluginWorkerError:
            self.close()
            raise

    def close(self) -> None:
        """
        有界关闭连接与子进程；重复调用不产生副作用。

        :return: 无
        """
        connection = self._connection
        process = self._process
        self._connection = None
        self._process = None
        if connection is not None:
            try:
                if process is not None and process.poll() is None:
                    self._request_id += 1
                    request = {"version": protocol_version, "request_id": self._request_id, "op": "close"}
                    connection.send_bytes(json.dumps(request).encode("utf-8"))
                    if wait([connection], close_timeout_seconds):
                        connection.recv_bytes(max_frame_bytes)
            except (OSError, EOFError):
                pass
            finally:
                connection.close()
        if process is not None:
            try:
                process.wait(timeout=close_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=close_timeout_seconds)

    def __enter__(self) -> PluginWorkerProcess:
        """
        以 context manager 启动并持有 Worker。

        :return: 已连接的 Worker 实例
        """
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        """
        退出 context manager 时回收子进程。

        :return: 无
        """
        self.close()
