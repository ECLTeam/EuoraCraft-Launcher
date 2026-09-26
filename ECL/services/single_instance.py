# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：单实例检测：同一数据目录仅允许一个主实例并支持请求窗口置前。
#
# 公开接口：
#   - class SingleInstanceService — 主实例监听服务（发现文件 + 本地 TCP）。
#       - start() -> None — 绑定端口、写发现文件并启动守护监听线程。
#       - close() -> None — 停止监听并清理发现文件，幂等。
#   - probe_running_instance(data_path, argv, timeout=…) -> bool — 探测已运行的主实例并请求置前。
# ============================================================

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

from ECL.events import EventBus
from ECL.utils import atomic_write_text
from ECL.utils.logging import get_logger

discovery_filename = "single_instance.json"
protocol_version = 1  # 单实例请求协议版本；第三期将扩展 launch 转发动作
_read_buffer_limit = 65536


class SingleInstanceService:
    """
    主实例单例监听服务。

    服务启动后在本机回环地址绑定随机端口并写入发现文件，接受来自后续
    启动进程的 JSON 行请求；请求经令牌校验后转换为事件派发（当前仅支持
    ``focus`` 置前动作，协议为后续参数转发预留 ``action`` 扩展位）。
    """

    def __init__(self, events: EventBus, data_path: Path, launcher_version: str) -> None:
        """
        创建服务实例；实际监听经 :meth:`start` 启动。

        :param events: 应用上下文的事件总线
        :param data_path: 启动器数据目录，发现文件写入其中
        :param launcher_version: 当前启动器版本号，随发现文件暴露
        """
        self.logger = get_logger("SingleInstance")
        self.events = events
        self.data_path = Path(data_path)
        self.launcher_version = launcher_version
        self.discovery_path = self.data_path / discovery_filename
        self._token = secrets.token_urlsafe(32)  # 一次性访问令牌，随进程生成
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        """
        绑定本机回环随机端口、写入发现文件并启动守护监听线程。

        端口绑定失败时记录告警并降级为无监听运行（后续启动将无法被互斥，
        但不影响本进程功能）。

        :raises OSError: 发现文件写入失败时抛出，由组合根按统一异常路径处理
        """
        if self._closed or self._thread is not None:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(4)
        except OSError:
            self.logger.exception("单实例监听端口绑定失败，本次运行不提供单实例互斥")
            server.close()
            return
        self._server_socket = server
        port = server.getsockname()[1]
        self._write_discovery(port)
        self._thread = threading.Thread(
            target=self._serve,
            args=(server,),
            name="ECL-SingleInstance",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("单实例监听已开启: port=%d", port)

    def close(self) -> None:
        """
        停止监听线程并删除发现文件，重复调用无副作用。
        """
        if self._closed:
            return
        self._closed = True
        server = self._server_socket
        self._server_socket = None
        if server is not None:
            with suppress(OSError):
                server.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                server.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with suppress(OSError):
            self.discovery_path.unlink(missing_ok=True)

    def _write_discovery(self, port: int) -> None:
        # 原子写入发现文件；监听先行就绪，后续进程读到文件即可立即连接。
        payload = {
            "port": port,
            "token": self._token,
            "pid": os.getpid(),
            "launcherVersion": self.launcher_version,
            "protocolVersion": protocol_version,
        }
        atomic_write_text(self.discovery_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _serve(self, server: socket.socket) -> None:
        # 守护线程 accept 循环：每连接读取单行 JSON 请求并回执处理结果。
        while not self._closed:
            try:
                conn, _addr = server.accept()
            except OSError:
                break  # 套接字已在关闭流程中
            with conn:
                try:
                    conn.settimeout(5.0)
                    payload = self._read_request(conn)
                    ok = self._dispatch(payload)
                    conn.sendall((json.dumps({"ok": ok}) + "\n").encode("utf-8"))
                except (OSError, ValueError):
                    self.logger.debug("单实例请求处理失败", exc_info=True)

    @staticmethod
    def _read_request(conn: socket.socket) -> dict[str, Any]:
        # 读取以换行结尾的单行 JSON 请求，超限或格式非法视为无效请求。
        buffer = bytearray()
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)
            if b"\n" in buffer or len(buffer) > _read_buffer_limit:
                break
        first_line = bytes(buffer).split(b"\n", 1)[0]
        document = json.loads(first_line.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("单实例请求不是 JSON 对象")
        return document

    def _dispatch(self, payload: dict[str, Any]) -> bool:
        # 校验令牌后按动作派发事件；未知动作直接拒绝，为后续扩展保留协议位。
        if not secrets.compare_digest(str(payload.get("token", "")), self._token):
            self.logger.warning("单实例请求令牌校验失败，已拒绝")
            return False
        action = str(payload.get("action") or "")
        if action == "focus":
            argv = payload.get("argv")
            self.events.emit("launcher:focus_request", {"argv": argv if isinstance(argv, list) else []})
            return True
        self.logger.warning("未知的单实例请求动作: %s", action or "<空>")
        return False


def probe_running_instance(data_path: Path, argv: Sequence[str], *, timeout: float = 3.0) -> bool:
    """
    探测同一数据目录下正在运行的主实例，命中时请求其窗口置前。

    发现文件缺失、记录的 pid 已失效或连接握手失败时视为无主实例；
    pid 失效或连接被拒绝时清理残留发现文件，供本次运行接管为新的主实例。

    :param data_path: 启动器数据目录
    :param argv: 当前进程的命令行参数，随 focus 请求一并发送
    :param timeout: 连接与应答的超时秒数
    :return: True 表示已存在主实例且置前请求送达
    """
    discovery_path = Path(data_path) / discovery_filename
    if not discovery_path.is_file():
        return False
    try:
        document = json.loads(discovery_path.read_text(encoding="utf-8"))
        port = int(document["port"])
        token = str(document["token"])
        pid = int(document["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        with suppress(OSError):
            discovery_path.unlink(missing_ok=True)
        return False
    if not psutil.pid_exists(pid):
        with suppress(OSError):
            discovery_path.unlink(missing_ok=True)
        return False
    request = {"token": token, "action": "focus", "argv": list(argv)}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            conn.settimeout(timeout)
            conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
            reply = conn.recv(4096)
    except OSError:
        with suppress(OSError):
            discovery_path.unlink(missing_ok=True)
        return False
    try:
        return bool(json.loads(reply.decode("utf-8") or "{}").get("ok"))
    except ValueError:
        return False


__all__ = ["SingleInstanceService", "probe_running_instance"]
