from collections.abc import Callable
from re import match as re_match
from copy import deepcopy
import asyncio
import struct
import json

MAX_PACKET_BODY_BYTES = 1024 * 1024


class AsyncFloroldingServer:
    def __init__(
        self,
        machine_id: str,
        easytier_id: int | str,
        player_name: str,
        server_host: str = "0.0.0.0",
        server_port: int = 3939,
        minecraft_port: int = 25565,
        vendor: str = "Florolding",
        log_callback: Callable[[str, str], None] = lambda level, message: print(f"[{level}] {message}"),
    ):
        """
        实例化一个异步联机服务端
        :param machine_id: 机器码(其实是什么无所谓, 别和别人的重复就行:)
        :param easytier_id: 自己的 EasyTier peer ID
        :param player_name: 玩家昵称
        :param server_host: 联机大厅服务端 IP
        :param server_port: 联机大厅服务端端口
        :param minecraft_port: Minecraft 端口
        :param vendor: 客户端信息
        :param log_callback: 日志回调, 接受一个这样的函数 (level: str, message: str) -> None
        """
        player_name = player_name
        self.server_host = server_host
        self.server_port = server_port
        self.minecraft_port = minecraft_port
        self.log_callback = log_callback
        self.server = None

        self.players = {
            machine_id: {
                "name": player_name,
                "machine_id": machine_id,
                "easytier_id": easytier_id,
                "vendor": vendor,
                "kind": "HOST",
            }
        }  # {machine_id: player_info}
        self.machine_ids = {}  # {writer: machine_id}
        self.client_protocols = {}  # {machine_id: protocols}

        self.lock = asyncio.Lock()  # 异步锁

        # 协议处理器映射
        self.protocol_handlers = {
            "c:ping": self._c_ping,
            "c:protocols": self._c_protocols,
            "c:server_port": self._c_server_port,
            "c:player_ping": self._c_player_ping,
            "c:player_profiles_list": self._c_player_profiles_list,
        }

        # 其他支持的协议列表
        self.other_protocols = ["c:player_easytier_id"]

        self.loop = None

    def _supported_protocols(self) -> list[str]:
        supported_protocols = list(self.protocol_handlers.keys())
        supported_protocols.extend(self.other_protocols)
        return supported_protocols

    def register_protocol_handler(self, protocol: str, handler: Callable) -> None:
        """
        注册一个额外的 Scaffolding 协议处理器。
        """
        if not bool(re_match(r"^[a-z0-9_]+:[a-z0-9_]+$", protocol)):
            raise ValueError(f"Invalid protocol type: {protocol}")
        if protocol in self.protocol_handlers or protocol in self.other_protocols:
            raise ValueError(f"Protocol already registered: {protocol}")
        if not callable(handler):
            raise TypeError("Protocol handler must be callable")
        self.protocol_handlers[protocol] = handler

    def set_minecraft_port(self, minecraft_port: int | str):
        """
        设置 Minecraft 端口, 一般不建议修改, 因为市面上的 Scaffolding 实现基本上仅在加入时获取这个端口
        """
        self.minecraft_port = minecraft_port

    @staticmethod
    async def _c_ping(request_body: bytes, *_) -> tuple:
        return 0, request_body  # 返回相同的请求体

    async def _c_protocols(self, request_body: bytes, writer: asyncio.StreamWriter) -> tuple:
        try:
            if writer in self.machine_ids:
                machine_id = self.machine_ids[writer]
                if machine_id not in self.players:
                    self.client_protocols.update({machine_id: list(request_body.decode("ascii").split("\0"))})
            return 0, "\0".join(self._supported_protocols()).encode("ascii")
        except UnicodeDecodeError:
            return 255, b"Invalid protocol format"

    async def get_client_protocols(self) -> dict:
        """获取客户端协议(异步)"""
        try:
            async with self.lock:
                return deepcopy(self.client_protocols)
        except Exception:
            return {}

    def sync_get_client_protocols(self) -> dict:
        """获取客户端协议(同步)"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.get_client_protocols(), self.loop).result()
        return {}

    async def _c_server_port(self, *_) -> tuple:
        return 0, struct.pack(">H", self.minecraft_port)

    async def _c_player_ping(self, request_body: bytes, writer: asyncio.StreamWriter) -> tuple:
        try:
            # 解析JSON请求体
            player_data = json.loads(request_body.decode("utf-8"))
            if not all(field in player_data for field in ["name", "machine_id", "vendor"]):
                return 255, b"Missing required fields"
            machine_id = player_data.get("machine_id")
            async with self.lock:
                if writer not in self.machine_ids:
                    self.machine_ids.update({writer: machine_id})
                    if machine_id not in self.players:
                        player_data.update({"kind": "GUEST"})
                        self.players.update({machine_id: player_data})
            return 0, b""
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return 255, f"Invalid JSON format: {e}".encode()

    async def _c_player_profiles_list(self, *_) -> tuple:
        try:
            # 构建玩家列表
            async with self.lock:
                # self.log_callback("DEBUG", str(self.players))
                return 0, json.dumps(list(self.players.values())).encode("utf-8")
        except Exception as e:
            return 255, f"Error generating player list: {e}".encode()

    async def get_player_list(self) -> list[dict[str, str | int]]:
        """获取玩家列表(异步)"""
        try:
            # 构建玩家列表
            async with self.lock:
                return list(deepcopy(self.players).values())
        except Exception:
            return []

    def sync_get_player_list(self) -> list[dict[str, str | int]]:
        """获取玩家列表(同步)"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(self.get_player_list(), self.loop).result()
        return []


    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> tuple[str, bytes]:
        """按 Scaffolding 长度头读取一个完整请求，正确处理 TCP 半包与粘包。"""
        type_length = (await reader.readexactly(1))[0]
        if type_length == 0:
            raise ValueError("Empty protocol type")
        protocol_bytes = await reader.readexactly(type_length)
        protocol_type = protocol_bytes.decode("ascii")
        if not bool(re_match(r"^[a-z0-9_]+:[a-z0-9_]+$", protocol_type)):
            raise ValueError("Invalid protocol type")
        body_length = struct.unpack(">I", await reader.readexactly(4))[0]
        if body_length > MAX_PACKET_BODY_BYTES:
            raise ValueError("Request body is too large")
        return protocol_type, await reader.readexactly(body_length)

    @staticmethod
    def _create_response(status: int, response_body: bytes = b"") -> bytes:
        """构建请求体"""
        body_length = len(response_body)
        # 构建响应: [状态(1字节)][响应体长度(4字节)][响应体]
        response = struct.pack(">B", status)  # 状态
        response += struct.pack(">I", body_length)  # 响应体长度
        response += response_body  # 响应体
        return response

    async def _remove_player(self, writer: asyncio.StreamWriter):
        """移除玩家"""
        async with self.lock:
            if writer in self.machine_ids:
                machine_id = self.machine_ids.pop(writer)
                if machine_id in self.players:
                    self.players.pop(machine_id)

    async def remove_player(self, machine_id: str) -> None:
        """
        按机器标识移除玩家；连接由房客在收到协议响应后自行关闭。
        """
        async with self.lock:
            self.players.pop(machine_id, None)
            for candidate, candidate_id in tuple(self.machine_ids.items()):
                if candidate_id == machine_id:
                    self.machine_ids.pop(candidate, None)
                    break

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        address = writer.get_extra_info("peername")
        self.log_callback("INFO", f"新的连接: {address}")
        try:
            while True:
                protocol_type, request_body = await self._read_request(reader)
                # self.log_callback("DEBUG", f"{self.machine_ids.get(writer)} 调用: {protocol_type}")
                if protocol_type in self.protocol_handlers:
                    status, response_body = await self.protocol_handlers[protocol_type](request_body, writer)
                    response = self._create_response(status, response_body)
                else:
                    # 不支持的协议
                    error_msg = f"Unsupported protocol: {protocol_type}".encode()
                    response = self._create_response(255, error_msg)
                # 发送响应
                # self.log_callback("DEBUG", f"响应长度: {len(response)}")
                writer.write(response)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            # 客户端断开连接
            pass
        except (UnicodeDecodeError, ValueError) as e:
            self.log_callback("WARNING", f"协议数据无效: {e}")
        except Exception as e:
            self.log_callback("ERROR", f"异常: {e}")
        finally:
            # 客户端断开连接，立即移除相关玩家
            await self._remove_player(writer)
            writer.close()
            await writer.wait_closed()
            self.log_callback("INFO", f"连接关闭: {address}")

    async def start(self):
        """启动Florolding TCP服务器, 基于Scaffolding协议"""
        self.server = await asyncio.start_server(self._handle_client, self.server_host, self.server_port)

        self.loop = asyncio.get_running_loop()

        self.log_callback("INFO", f"FloroldingServer 启动在 {self.server_host}:{self.server_port}")
        self.log_callback("INFO", f"支持的协议: {', '.join(self._supported_protocols())}")
        self.log_callback("INFO", f"Minecraft 端口: {self.minecraft_port}")

        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.exceptions.CancelledError:
            pass

    async def stop(self):
        """停止Florolding TCP服务器"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.log_callback("INFO", "FloroldingServer 已停止")

    def sync_stop(self) -> bool:
        """
        同步线程安全的停止服务端
        :return: 成功停止返回 True
        """
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.stop(), self.loop)
            return True
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出异步上下文自动关闭服务器"""
        await self.stop()
