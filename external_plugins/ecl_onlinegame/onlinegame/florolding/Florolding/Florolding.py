from collections.abc import Callable
from ipaddress import IPv4Address
from threading import Thread
from hashlib import sha256
from copy import deepcopy
import asyncio
import socket
import time

import easytier_pyo3

from . import Scaffolding
from .F_Client import AsyncFloroldingClient
from .F_Server import AsyncFloroldingServer
from .Broadcaster import MinecraftLANBroadcaster

HOST_READY_TIMEOUT = 15


# 房客先给直连这么多秒，超过就降级到中转；避免对称 NAT 下等满整个连接超时。
direct_wait_seconds = 10.0

# Scaffolding 房主节点名的标准前缀与默认服务端口；其他启动器命名不完全一致时用默认端口兜底。
host_prefix = "scaffolding-mc-server-"
host_prefix_loose = "scaffolding-mc"
default_scf_port = 3939


class Florolding:
    def __init__(
        self,
        launcher_info: str = "EuoraCraft-Launcher",
        log_callback: Callable[[str, str], None] = lambda level, message: print(f"[{level}] {message}"),
        p2p_only: bool = True,
    ):
        """
        :param launcher_info: 启动器信息
        :param log_callback: 日志回调, 接受一个这样的函数 (level: str, message: str) -> None
        :param p2p_only: 房主是否只允许直连；默认开启，直连质量最好，无法直连的房客会被拒绝
        """
        self.vendor = f"{launcher_info} (Florolding 1.0) / EasyTier {easytier_pyo3.version()} for EasyTier-PyO3"
        self.log_callback = log_callback
        self.nodes = []
        self.easytier_config = {
            "dhcp": True,
            "tcp_whitelist": ["0"],
            "udp_whitelist": ["0"],
            "compression": "zstd",
            "p2p_only": p2p_only,
            "listeners": ["udp://0.0.0.0:0", "tcp://0.0.0.0:0"],
            "flags": {
                "no_tun": True,
                "bind_device": False,
                "latency_first": True,
                "multi_thread": True,
                "enable_kcp_proxy": True,
            },
        }

    def set_nodes(self, nodes: list[str]) -> None:
        """
        设置 EasyTier 节点列表
        :param nodes: 节点列表 ["URI"]
        :return: None
        """
        self.nodes = [{"uri": node} for node in nodes]

    def create_room(
        self,
        player_name: str,
        minecraft_port: int = 25565,
        conn_timeout: int = HOST_READY_TIMEOUT,
        protocol_handlers: dict[str, Callable] | None = None,
    ) -> tuple[str, AsyncFloroldingServer, easytier_pyo3.Node]:
        """
        创建一个房间(注意一定要管理好 EasyTier 节点的生命周期!!!!!)
        :param player_name: 玩家昵称
        :param minecraft_port: Minecraft 端口
        :param conn_timeout: 等待房主节点连接公共节点的超时秒数
        :param protocol_handlers: 注册一些协议
        :return: (房间码, 服务端实例, EasyTierPyo3 Node)
        """
        room_code = Scaffolding.generate_code()
        machine_id = Scaffolding.machine_id()
        easytier_config: dict = deepcopy(self.easytier_config)
        server_port = find_free_port()
        easytier_config.update(
            {
                "dhcp": False,
                "ipv4": self.host_virtual_ipv4(room_code),
                "hostname": f"scaffolding-mc-server-{server_port}",
                "network_identity": {
                    "network_name": f"scaffolding-mc-{room_code[2:11]}",
                    "network_secret": room_code[12:21],
                },
                "peer": self.nodes,
                "tcp_whitelist": [str(server_port), str(minecraft_port)],
            }
        )

        easytier_node = easytier_pyo3.Node(easytier_config)
        easytier_node.start()
        try:
            self.wait_for_host_ready(easytier_node, conn_timeout)
        except Exception:
            easytier_node.stop()
            raise

        server = AsyncFloroldingServer(
            machine_id=machine_id,
            easytier_id=easytier_node.peer_id(),
            player_name=player_name,
            server_port=server_port,
            minecraft_port=minecraft_port,
            vendor=self.vendor,
            log_callback=self.log_callback,
        )
        for protocol, handler in (protocol_handlers or {}).items():
            server.register_protocol_handler(protocol, handler)
        Thread(target=asyncio.run, args=(server.start(),), daemon=True).start()

        return room_code, server, easytier_node

    @staticmethod
    def host_virtual_ipv4(room_code: str) -> str:
        """根据房间码稳定生成该大厅独立的私有 IPv4 网段。"""
        digest = sha256(room_code.encode("ascii")).digest()
        return f"10.{digest[0]}.{digest[1]}.1/24"

    @staticmethod
    def wait_for_host_ready(easytier_node: easytier_pyo3.Node, conn_timeout: int = HOST_READY_TIMEOUT) -> None:
        """等待房主获得虚拟地址并至少连通一个公共节点。"""
        start_time = time.time()
        while True:
            node_info = easytier_node.node_info()
            connectors = easytier_node.connectors()
            has_ipv4 = bool(node_info.get("ipv4_addr"))
            has_connected_peer = any(
                str(connector.get("status", "")).casefold() == "connected" for connector in connectors
            )
            if has_ipv4 and has_connected_peer:
                return
            latest_error = easytier_node.latest_error()
            if latest_error:
                raise RuntimeError(f"EasyTier 节点启动失败: {latest_error}")
            if time.time() - start_time > conn_timeout:
                raise TimeoutError("连接超时, 无法连接到公共联机节点")
            time.sleep(0.25)

    def join_room(
        self,
        room_code: str,
        player_name: str,
        conn_timeout: int = 30,
        supported_protocols: list[str] | None = None,
    ) -> tuple[AsyncFloroldingClient, easytier_pyo3.Node]:
        """
        加入一个房间(注意一定要管理好 EasyTier 节点的生命周期!!!!!)
        :param room_code: 房间码
        :param player_name: 玩家昵称
        :param conn_timeout: 连接超时时间(单位: 秒)
        :param supported_protocols: 注册一些协议
        :return: (客户端实例, EasyTierPyo3 Node)
        """
        if not Scaffolding.validate_code(room_code):
            raise ValueError("房间码不合规")

        machine_id = Scaffolding.machine_id()
        deadline = time.time() + conn_timeout

        # 第一轮优先直连：直连质量最好，也不占用中转带宽。
        easytier_node, host_node_info = self._open_guest_node(room_code, p2p_only=True, deadline=deadline)
        host_peer_id = host_node_info.get("peer_id")
        direct_budget = min(direct_wait_seconds, max(0.0, deadline - time.time()))
        direct_ready = host_peer_id is None or self._wait_for_host_peer(
            easytier_node, host_peer_id, direct_budget, require_direct=True
        )

        if not direct_ready:
            # 直连建不起来（常见于对称 NAT）时降级到允许中转，否则这类网络永远进不了房间。
            self.log_callback("WARN", "与房主的直连未建立，正在降级为中转连接")
            easytier_node.stop()
            easytier_node, host_node_info = self._open_guest_node(room_code, p2p_only=False, deadline=deadline)
            host_peer_id = host_node_info.get("peer_id")
            if host_peer_id is not None and not self._wait_for_host_peer(
                easytier_node, host_peer_id, max(0.0, deadline - time.time()), require_direct=False
            ):
                easytier_node.stop()
                raise TimeoutError("等待与房主的连接超时：直连与中转均不可用")

        server_port = self._host_server_port(str(host_node_info.get("hostname", "")))
        if server_port is None:
            easytier_node.stop()
            raise ValueError("联机大厅端口不合规")

        host_virtual_ip = IPv4Address(host_node_info["ipv4_addr"]["address"]["addr"]).exploded
        bind_server_port = find_free_port()
        easytier_node.apply_config(
            {
                "port_forward": [
                    {
                        "bind_addr": f"127.0.0.1:{bind_server_port}",
                        "dst_addr": f"{host_virtual_ip}:{server_port}",
                        "proto": "tcp",
                    }
                ]
            }
        )

        client = AsyncFloroldingClient(
            machine_id=machine_id,
            easytier_id=easytier_node.peer_id(),
            player_name=player_name,
            server_port=bind_server_port,
            vendor=self.vendor,
            log_callback=self.log_callback,
        )
        if supported_protocols:
            client.supported_protocols = list(dict.fromkeys([*client.supported_protocols, *supported_protocols]))
        Thread(target=client.start, daemon=True).start()
        while not client.writer:
            time.sleep(1)

        # 马上协商协议, 防止某些 Scaffolding 实现会炸
        client.sync_c_protocols()

        return client, easytier_node

    @staticmethod
    def _host_server_port(hostname: str) -> int | None:
        """
        从房主节点名解析 Scaffolding 服务端口。

        标准命名以端口结尾；非标准命名时回退默认端口，避免直接判定失败。

        :param hostname: 房主节点名
        :return: 服务端口，无法确定时为空
        """
        suffix = hostname.rsplit("-", 1)[-1]
        if suffix.isdigit() and 0 < int(suffix) <= 65535:
            return int(suffix)
        return default_scf_port

    def get_host_node_info(self, easytier_node: easytier_pyo3.Node, conn_timeout: int = 30):
        """
        获取房主的 EasyTier 节点信息
        :param easytier_node: EasyTierPyo3 Node
        :param conn_timeout: 连接超时时间(单位: 秒)
        :return: EasyTier 节点信息
        """
        host_node_info = {}
        fallback_info = {}
        seen: list[str] = []
        start_time = time.time()
        while True:
            for node_info in easytier_node.routes():
                hostname = str(node_info.get("hostname", ""))
                if not node_info.get("ipv4_addr", {}).get("address", {}).get("addr"):
                    continue
                seen.append(hostname)
                if hostname.lower().startswith(host_prefix):
                    host_node_info = node_info
                    break
                # 其他启动器可能不按标准前缀命名，先记下来，找不到标准节点时再兜底。
                if not fallback_info and hostname.lower().startswith(host_prefix_loose):
                    fallback_info = node_info
            if host_node_info:
                break
            time.sleep(1)
            if time.time() - start_time > conn_timeout:
                if fallback_info:
                    self.log_callback(
                        "WARN",
                        f"未找到标准命名的房主节点，改用 {fallback_info.get('hostname')}",
                    )
                    return fallback_info
                easytier_node.stop()
                raise TimeoutError(f"连接超时, 无法找到联机大厅（可见节点：{len(set(seen))} 个）")
        return host_node_info

    def _open_guest_node(self, room_code: str, *, p2p_only: bool, deadline: float):
        """
        以指定直连策略创建房客节点并读取房主信息。

        :param room_code: 房间码
        :param p2p_only: 本轮是否只允许直连
        :param deadline: 整次加入的截止时间戳
        :return: (节点, 房主节点信息)
        :raises TimeoutError: 在截止前找不到联机大厅时抛出
        """
        easytier_config: dict = deepcopy(self.easytier_config)
        easytier_config["p2p_only"] = p2p_only
        easytier_config.update(
            {
                "network_identity": {
                    "network_name": f"scaffolding-mc-{room_code[2:11]}",
                    "network_secret": room_code[12:21],
                },
                "peer": self.nodes,
            }
        )
        easytier_node = easytier_pyo3.Node(easytier_config)
        easytier_node.start()
        remaining = max(1, int(deadline - time.time()))
        return easytier_node, self.get_host_node_info(easytier_node, remaining)

    @staticmethod
    def _wait_for_host_peer(
        easytier_node: easytier_pyo3.Node, host_peer_id, timeout: float, *, require_direct: bool
    ) -> bool:
        """
        等待房主 peer 可被本机连通。

        首个 SCF 请求必须在隧道真正可用之后发送，否则请求会丢在未建立的通道里；
        ``require_direct`` 为真时只接受直连，为假时直连或中转都算连通。

        :param easytier_node: 房客节点
        :param host_peer_id: 房主 peer 标识
        :param timeout: 本轮等待上限（秒）
        :param require_direct: 是否必须为直连
        :return: 是否在超时前连通
        """
        start_time = time.time()
        while True:
            for peer in easytier_node.peers():
                if peer.get("peer_id") != host_peer_id:
                    continue
                if require_direct and not (peer.get("directly_connected_conns") or []):
                    continue
                return True
            if time.time() - start_time > timeout:
                return False
            time.sleep(0.5)

    def bind_mc_port(
        self,
        client: AsyncFloroldingClient,
        easytier_node: easytier_pyo3.Node,
        bind_mc_port: int = 25565,
        timeout: int = 30,
        broadcast: bool = True,
    ) -> bool | tuple[bool, MinecraftLANBroadcaster | None]:
        """
        绑定房主在虚拟 IP 上的 Minecraft 端口至本地
        :param client: Florolding Client
        :param easytier_node: EasyTierPyo3 Node
        :param bind_mc_port: 绑定的本地端口
        :param timeout: 超时时间(单位: 秒)
        :param broadcast: 是否向 UDP 播组广播 Minecraft 多人游戏
        :return: (bool, MinecraftLANBroadcaster | None)
        """
        mc_port = client.sync_c_server_port()
        if not mc_port:
            return False, None

        host_node_info = self.get_host_node_info(easytier_node, timeout)
        host_virtual_ip = IPv4Address(host_node_info["ipv4_addr"]["address"]["addr"]).exploded
        server_port = host_node_info["hostname"].removeprefix("scaffolding-mc-server-")
        if not server_port.isnumeric() or not (0 < int(server_port) <= 65535):
            raise ValueError("联机大厅端口不合规")
        # apply_config 替换整个转发列表；保留已连接的控制通道，避免扩展与心跳被断开。
        easytier_node.apply_config(
            {
                "port_forward": [
                    {
                        "bind_addr": f"127.0.0.1:{client.server_port}",
                        "dst_addr": f"{host_virtual_ip}:{server_port}",
                        "proto": "tcp",
                    },
                    {
                        "bind_addr": f"0.0.0.0:{bind_mc_port}",
                        "dst_addr": f"{host_virtual_ip}:{mc_port}",
                        "proto": "tcp",
                    }
                ]
            }
        )

        if broadcast:
            broadcaster = MinecraftLANBroadcaster(
                host="127.0.0.1",
                port=bind_mc_port,
                log_callback=self.log_callback,
            )
            broadcaster.start()

            return True, broadcaster

        return True, None


def find_free_port() -> int:
    """创建一个临时socket，绑定到端口0，让操作系统分配一个空闲端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # 绑定到端口0，操作系统会分配一个临时端口
        s.bind(("", 0))
        # 获取实际分配到的端口号
        port = s.getsockname()[1]
        return port
