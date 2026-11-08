from collections.abc import Callable
from mcstatus import JavaServer
import threading
import socket

class MinecraftLANBroadcaster:
    """
    持续检测 Minecraft 服务器，在线时自动向局域网广播。
    广播内容包含动态 MOTD（含玩家数量），离线时停止广播。
    """
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25565,
        broadcast_interval=3,
        check_interval=5,
        log_callback: Callable[[str, str], None] = lambda l, m: print(f"[{l}] {m}")
    ):
        """
        :param host: 目标服务器 IP 或域名
        :param port: 目标服务器端口
        :param broadcast_interval: 广播包发送间隔（秒），一般 1.5~3
        :param check_interval: 状态检查间隔（秒），检测服务器是否在线
        :param log_callback: 日志回调, 接受一个这样的函数 (level: str, message: str) -> None
        """
        self.host = host
        self.port = port
        self.broadcast_interval = broadcast_interval
        self.check_interval = check_interval
        self.log_callback = log_callback

        self.broadcast_addr = "255.255.255.255"
        self.broadcast_port = 4445
        self._broadcasting = False
        self._stop_event = threading.Event()

        self._server_online = False
        self._motd = "Minecraft Server"
        self._lock = threading.Lock()

        self._monitor_thread = None
        self._broadcast_thread = None

    def _update_status(self):
        """使用 mcstatus 查询服务器信息，更新共享状态"""
        try:
            server = JavaServer.lookup(f"{self.host}:{self.port}")
            status = server.status()
            with self._lock:
                self._server_online = True
                motd = status.description
                if isinstance(motd, dict):
                    motd = motd.get("text", str(motd))
                elif motd is None:
                    motd = "Minecraft Server"
                self._motd = f"{motd} [{status.players.online}/{status.players.max}]"
        except Exception:
            with self._lock:
                self._server_online = False

    def _broadcast_loop(self):
        """广播线程主循环，持续发送 UDP 包直到收到停止信号"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        while not self._stop_event.is_set():
            with self._lock:
                if not self._server_online:
                    if self._stop_event.wait(self.broadcast_interval):
                        break
                    continue
                motd = self._motd
                port = self.port

            message = f"[MOTD]{motd}[/MOTD][AD]{port}[/AD]"
            try:
                sock.sendto(message.encode("utf-8"), (self.broadcast_addr, self.broadcast_port))
            except Exception as e:
                self.log_callback("ERROR", f"广播发送失败: {e}")

            if self._stop_event.wait(self.broadcast_interval):
                break

        sock.close()
        self.log_callback("INFO", "广播线程已停止")

    def _monitor_loop(self):
        """监控线程主循环，定期检查服务器状态，控制广播线程"""
        while not self._stop_event.is_set():
            self._update_status()

            with self._lock:
                online = self._server_online

            if online and not self._broadcasting:
                self._start_broadcaster()
            elif not online and self._broadcasting:
                self._stop_broadcaster()

            if self._stop_event.wait(self.check_interval):
                break

        if self._broadcasting:
            self._stop_broadcaster()
        self.log_callback("INFO", "监控线程已停止")

    def _start_broadcaster(self):
        """启动广播线程"""
        if self._broadcasting:
            if self._broadcast_thread and self._broadcast_thread.is_alive():
                return
            self._broadcasting = False

        self._stop_event.clear()
        self._broadcast_thread = threading.Thread(target=self._broadcast_loop, daemon=True)
        self._broadcast_thread.start()
        self._broadcasting = True
        self.log_callback("INFO", "广播已启动")

    def _stop_broadcaster(self):
        """停止广播线程"""
        if not self._broadcasting:
            return
        self._stop_event.set()
        if self._broadcast_thread and self._broadcast_thread.is_alive():
            self._broadcast_thread.join(timeout=2)
        self._broadcasting = False
        self.log_callback("INFO", "广播已停止")

    def start(self):
        """启动监控服务（非阻塞）"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            self.log_callback("WARNING", "端口监控服务已在运行")
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        self.log_callback("INFO", "端口监控服务已启动")

    def stop(self):
        """停止整个服务"""
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)
        if self._broadcasting:
            self._stop_broadcaster()
        self.log_callback("INFO", "广播服务已完全停止")
