from collections.abc import Callable
import asyncio
import struct
import json


MAX_PACKET_BODY_BYTES = 1024 * 1024


class AsyncFloroldingClient:
    def __init__(
        self,
        machine_id: str,
        easytier_id: str | int,
        player_name: str,
        server_host: str = "127.0.0.1",
        server_port: int = 3939,
        vendor: str = "Florolding",
        log_callback: Callable[[str, str], None] = lambda l, m: print(f"[{l}] {m}")
    ):
        """
        实例化一个异步联机客户端
        :param machine_id: 机器码(其实是什么无所谓, 别和别人的重复就行:)
        :param easytier_id: 自己的 EasyTier peer ID
        :param player_name: 玩家昵称
        :param server_host: 联机大厅服务端 IP
        :param server_port: 联机大厅服务端端口
        :param vendor: 客户端信息
        :param log_callback: 日志回调, 接受一个这样的函数 (level: str, message: str) -> None
        """
        self.player_name = player_name
        self.machine_id = machine_id
        self.easytier_id = easytier_id
        self.vendor = vendor
        self.server_host = server_host
        self.server_port = server_port
        self.log_callback = log_callback
        self.reader = None
        self.writer = None
        # 协商后的可用协议；None 表示尚未协商（协商本身只能用核心协议）。
        self.negotiated_protocols: set[str] | None = None

        self.player_data = {
            "name": self.player_name,
            "machine_id": self.machine_id,
            "easytier_id": self.easytier_id,
            "vendor": self.vendor
        }

        # 支持的协议列表
        self.supported_protocols = [
            "c:ping",
            "c:protocols",
            "c:server_port",
            "c:player_easytier_id",
            "c:player_ping",
            "c:player_profiles_list"
        ]

        self.heartbeat_body = json.dumps(self.player_data).encode("utf-8")

        self.heartbeat_task = None
        self.request_lock = asyncio.Lock()

        self.loop = None

    async def c_ping(self, data: bytes = b"ping") -> bool:
        """
        c:ping
        :param data: Data
        :return: bool
        """
        status, response_body = await self.send_request("c:ping", data)
        self.log_callback("INFO", f"[c:ping] 状态: {status}; 请求数据: {data}; 响应数据: {response_body}")
        if status == 0 and response_body == data:
            return True
        return False

    def sync_c_ping(self, data: bytes = b"ping") -> bool:
        """
        c:ping
        :param data: Data
        :return: bool
        """
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.c_ping(data), self.loop).result()
        return False

    async def c_protocols(self) -> list[str] | None:
        """
        c:protocols - 与服务端交换协议信息
        :return: 服务端支持的协议
        """
        request_body = "\0".join(self.supported_protocols).encode("ascii")
        status, response_body = await self.send_request("c:protocols", request_body)
        self.log_callback("INFO", f"[c:protocols] 状态: {status}")
        self.log_callback("DEBUG", f"[c:protocols] 客户端协议: {self.supported_protocols}")
        if status == 0:
            server_protocols = response_body.decode("ascii").split("\0")
            common = set(self.supported_protocols) & set(server_protocols)
            self.negotiated_protocols = common
            self.log_callback("DEBUG", f"[c:protocols] 服务器协议: {server_protocols}")
            self.log_callback("DEBUG", f"[c:protocols] 共同协议: {common}")
            self.log_callback("INFO", "[c:protocols] 协议协商成功")
            return server_protocols
        else:
            self.log_callback("ERROR", f"[c:protocols] 错误: {response_body.decode('utf-8')}")
            self.log_callback("INFO", "[c:protocols] 协议协商失败")
            return None

    def sync_c_protocols(self) -> list[str] | None:
        """
        c:protocols - 与服务端交换协议信息
        :return: 服务端支持的协议
        """
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.c_protocols(), self.loop).result()
        return None

    async def c_server_port(self) -> int | None:
        """
        c:server_port - 获取 Minecraft 端口
        :return: 成功返回 Minecraft 端口, 失败返回 None
        """
        status, response_body = await self.send_request("c:server_port", b"")
        self.log_callback("INFO", f"[c:server_port] 状态: {status}")
        if status == 0:
            try:
                port = struct.unpack(">H", response_body)[0]
                self.log_callback("DEBUG", f"[c:server_port] Minecraft 服务器端口(虚拟 IP): {port}")
                self.log_callback("INFO", "[c:server_port] 服务器端口请求成功")
                return int(port)
            except struct.error as e:
                self.log_callback("ERROR", f"[c:server_port] 请求体长度: {len(response_body)}; 异常: {e}")
        elif status == 32:
            self.log_callback("INFO", "[c:server_port] 服务器未启动")
            return None
        else:
            self.log_callback("ERROR", f"[c:server_port] 错误: {response_body.decode('utf-8')}")
            self.log_callback("INFO", "[c:server_port] 服务器端口请求失败")
            return None

    def sync_c_server_port(self) -> int | None:
        """
        c:server_port - 获取 Minecraft 端口
        :return: 成功返回 Minecraft 端口, 失败返回 None
        """
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.c_server_port(), self.loop).result()
        return None

    async def c_player_profiles_list(self) -> list[dict] | None:
        """
        c:player_profiles_list - 获取成员列表
        :return: 成功返回 成员列表, 失败返回 None
        """
        status, response_body = await self.send_request("c:player_profiles_list", b"")
        self.log_callback("INFO", f"[c:player_profiles_list] 状态: {status}")
        if status == 0:
            try:
                players = json.loads(response_body.decode("utf-8"))
                self.log_callback("INFO", f"[c:player_profiles_list] 在线玩家 {len(players)} 人")
                return players
            except json.decoder.JSONDecodeError:
                self.log_callback("ERROR", f"[c:player_profiles_list] Json 解析出错, 源数据: {response_body}")
        else:
            self.log_callback("ERROR", f"[c:player_profiles_list] 错误: {response_body.decode('utf-8')}")
            self.log_callback("INFO", "[c:player_profiles_list] 玩家列表请求失败")
            return None

    def sync_c_player_list(self) -> list[dict] | None:
        """
        c:player_profiles_list - 获取成员列表
        :return: 成功返回 成员列表, 失败返回 None
        """
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.c_player_profiles_list(), self.loop).result()
        return None

    async def start_heartbeat(self, interval: int = 5):
        """定时发送心跳"""
        async def heartbeat_loop():
            while True:
                try:
                    await self.send_request("c:player_ping", self.heartbeat_body)
                    # self.log_callback("DEBUG", f"[c:player_ping] 心跳发送成功")
                except Exception as e:
                    self.log_callback("ERROR", f"[c:player_ping] 心跳发送失败: {e}")
                if not self.writer or self.writer.is_closing():
                    break
                await asyncio.sleep(interval)

        self.heartbeat_task = asyncio.create_task(heartbeat_loop())
        self.log_callback("INFO", f"[c:player_ping] 开始定时心跳, 间隔: {interval}s")

    @staticmethod
    def _create_request(protocol_type: str, request_body: bytes = b'') -> bytes:
        """创建 TCP 请求"""
        # 编码协议类型
        protocol_bytes = protocol_type.encode("ascii")
        # 构建请求: [类型长度(1字节)][类型][请求体长度(4字节)][请求体]
        request = struct.pack(">B", len(protocol_bytes))  # 类型长度
        request += protocol_bytes  # 类型
        request += struct.pack(">I", len(request_body))  # 请求体长度
        request += request_body  # 请求体
        return request


    async def send_request(self, protocol_type: str, request_body: bytes = b"") -> tuple:
        """发送请求并接收响应"""
        if not self.writer:
            raise RuntimeError("未连接到服务器")
        if self.negotiated_protocols is not None and protocol_type not in self.negotiated_protocols:
            # 房主没有声明该协议时不能盲发：对方可能直接断开连接，
            # 让上层按「该扩展本次不可用」处理即可。
            raise RuntimeError(f"房主未声明该协议: {protocol_type}")
        async with self.request_lock:
            self.writer.write(self._create_request(protocol_type, request_body))
            await self.writer.drain()
            header = await self.reader.readexactly(5)
            status = header[0]
            body_length = struct.unpack(">I", header[1:5])[0]
            if body_length > MAX_PACKET_BODY_BYTES:
                raise ValueError("响应数据过大")
            response_body = await self.reader.readexactly(body_length)
            return status, response_body

    async def connect(self):
        """连接到基于 Scaffolding 协议的服务器"""
        self.reader, self.writer = await asyncio.open_connection(
            self.server_host, self.server_port
        )
        self.loop = asyncio.get_running_loop()
        self.log_callback("INFO", f"已连接到服务器 {self.server_host}:{self.server_port}")

        await self.start_heartbeat()

    async def disconnect(self):
        """断开连接"""
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
            self.reader = None
            self.writer = None
            self.log_callback("INFO", "已断开与服务器的连接")

    async def _serve_forever(self):
        """
        保活: 保持事件循环运行直到连接关闭
        防止 asyncio.run 在 connect 返回后取消心跳任务并销毁事件循环
        """
        while self.writer and not self.writer.is_closing():
            await asyncio.sleep(1)

    def start(self):
        """
        线程入口: 连接服务器并保活事件循环
        用法: Thread(target=client.start, daemon=True).start()
        """
        async def _run():
            await self.connect()
            await self._serve_forever()
        asyncio.run(_run())

    async def __aenter__(self):
        """进入异步上下文连接服务器"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出异步上下文断开连接"""
        await self.disconnect()
