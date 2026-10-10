# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：管理 EasyTier 连接、可用节点和联机房间状态。
#
# 公开接口：
#   - class ConnectorService — 联机服务，封装 Florolding + EasyTier 的多人联机能力。
#       - available() -> bool — 联机服务是否可用（依赖齐全）。
#       - easytier_available() -> bool — EasyTier 是否可用。
#       - easytier_version() -> str — EasyTier 版本号。
#       - fetch_nodes(force) -> list[str] — 获取可用的 EasyTier 中继节点 URI 列表。
#       - preload_nodes() -> list[str] — 仅预热公共节点，不验证用户地址。
#       - start_node_preload() -> bool — 服务启动后在后台预先读取一次节点列表。
#       - get_status() -> dict[str, Any] — 获取当前联机状态。
#       - get_easytier_status() -> dict[str, Any] — 获取 EasyTier 安装状态。
#       - get_nat_type() -> dict[str, Any] — 使用 EasyTier 内置 STUN 探测检测 NAT 类型。
#       - host_port(port) -> dict[str, Any] — 以指定端口创建联机房间。
#       - host_instance(game_path, version_id) -> dict[str, Any] — 为指定实例创建联机房间。
#       - join(code) -> dict[str, Any] — 加入联机房间。
#       - leave() -> dict[str, Any] — 退出联机房间。
#       - close() -> None — 关闭联机服务，停止运行中的房间与 EasyTier 节点。
#       - kick(machine_id) -> dict[str, Any] — 移出玩家。
#       - detect_ports() -> dict[str, Any] — 探测本机 Java 进程开放的候选端口。
#       - search_mc_port(ports) -> dict[str, Any] — 在候选端口中搜索确认 Minecraft 服务端口。
#   - class ConnectorNatError — NAT 探测失败，携带稳定 IPC 错误码。
# ============================================================

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import struct
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from ipaddress import ip_address
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import easytier_pyo3
import httpx
import psutil

from ECL.plugins.connector import (
    ConnectorExtensionRegistry,
    ConnectorProtocolRequest,
    ConnectorSessionContext,
)
from ECL.services.connector_nodes import ConnectorNodeConfig, ConnectorNodeError, ConnectorNodeSettings
from ECL.services.florolding import Florolding, find_free_port, validate_code
from ECL.utils import ConnectorError, ConnectorNotAvailableError  # noqa: F401  # re-export
from ECL.utils.operation_logging import current_operation, operation_scope, trace_scope

logger = logging.getLogger("EuoraCraft-Launcher.Connector")
ConnectorMode = str  # "idle" | "starting" | "host" | "guest"
EasyTierPhase = str  # "idle" | "resolving" | "downloading" | "extracting" | "installed" | "failed"
NatTypeKind = str  # "cone" | "symmetric" | "blocked" | "unknown"


class ConnectorNatError(ConnectorError):
    """
    表示可安全转换为 IPC 失败响应的 NAT 探测错误。

    :param message: 面向调用方的失败描述
    :param error_code: 超时、忙碌或探测失败的稳定错误码
    """

    error_code: str

    def __init__(self, message: str, error_code: str) -> None:
        """
        保存可安全返回前端的提示和稳定 NAT 错误码。

        :param message: 不含原生节点内部信息的失败提示
        :param error_code: NAT 超时、忙碌或失败的错误码
        """
        super().__init__(message)
        self.error_code = error_code


def _supports_kwarg(func: Callable[..., Any], name: str) -> bool:
    # 检测调用目标是否接受指定关键字参数，用于兼容不同版本的 Florolding API。
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    if name in signature.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())


class ConnectorService:
    """
    联机服务，封装 Florolding + EasyTier 的多人联机能力。

    管理房间的创建和关闭、玩家列表及网络连接状态。
    """

    node_list_url = "https://api.qomicex.top/api/nodes"
    node_user_agent = "ECL"
    default_nodes = [
        "tcp://public.easytier.cn:11010",
        "tcp://8.162.7.222:11010",
        "tcp://103.239.245.69:38867",
        "tcp://mc.lenmei233.top:31010",
    ]
    easytier_schemes = ("tcp://", "udp://", "quic://", "faketcp://", "ws://", "wss://")
    node_fetch_timeout_seconds = 10.0
    node_cache_ttl_seconds = 30 * 60.0
    max_aggregate_workers = 8
    player_list_fetch_timeout_seconds = 5
    extension_request_timeout_seconds = 5
    max_status_packet_bytes = 1_048_576
    max_tcp_port = 65535
    status_probe_timeout_seconds = 0.5
    nat_probe_timeout_seconds = 25.0
    nat_probe_interval_seconds = 0.25

    def __init__(
        self,
        launcher_info: str = "EuoraCraft-Launcher",
        log_callback: Callable[[str, str], None] | None = None,
        player_name: str | None = None,
        http_client: httpx.Client | None = None,
        node_cache_ttl: float | None = None,
        extensions: ConnectorExtensionRegistry | None = None,
        local_player_icon_provider: Callable[[], str | None] | None = None,
        node_settings_provider: Callable[[], ConnectorNodeConfig] | None = None,
    ) -> None:
        self._launcher_info = launcher_info
        self._player_name = player_name or "Player"
        self._http = http_client
        self.extensions = extensions or ConnectorExtensionRegistry()
        self._local_player_icon_provider = local_player_icon_provider
        self._node_settings_provider = node_settings_provider
        if log_callback is None:

            def _default_log(level: str, msg: str) -> None:
                log_func = getattr(logger, level.lower(), logger.info)
                log_func(msg)

            self._log_callback = _default_log
        else:
            self._log_callback = log_callback

        # 运行时状态
        self._mode: ConnectorMode = "idle"
        self._room_code: str | None = None
        self._mc_host: str | None = None
        self._mc_port: int | None = None
        self._room_server: Any = None  # AsyncFloroldingServer
        self._easy_tier_node: Any = None  # easytier_pyo3.Node
        self._nat_probe_lock = Lock()
        self._client: Any = None  # AsyncFloroldingClient
        self._players: list[dict[str, Any]] = []
        self._nodes: list[str] = list(self.default_nodes)
        self._node_cache_ttl = max(0.0, self.node_cache_ttl_seconds if node_cache_ttl is None else node_cache_ttl)
        self._node_cache_refreshed_at: float | None = None
        self._node_cache_lock = RLock()
        self._node_refreshing = False
        self._node_preload_started = False
        self._node_preload_thread: Thread | None = None
        self._node_preload_stopped = Event()
        self._error: str | None = None
        self._started: bool = False
        self._game_info: dict[str, Any] | None = None
        # 建房/加入的状态迁移守卫：防止并发 host/join 互相覆盖房间状态
        self._transition_lock = RLock()
        self._transitioning = False

        # 易用性
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def available(self) -> bool:
        """
        联机服务是否可用（依赖齐全）。
        """
        return True

    @property
    def easytier_available(self) -> bool:
        """
        EasyTier 是否可用。
        """
        return True

    @property
    def easytier_version(self) -> str:
        """
        EasyTier 版本号。
        """
        try:
            return easytier_pyo3.version()
        except Exception:
            return "unknown"

    def _remember_nodes(self, nodes: list[str]) -> list[str]:
        """
        在缓存锁内提交节点副本；服务关闭后拒绝迟到的预热结果。
        """
        with self._node_cache_lock:
            if not self._node_preload_stopped.is_set():
                self._nodes = list(nodes)
                self._node_cache_refreshed_at = monotonic()
                logger.debug("联机节点列表已刷新并写入内存缓存，共 %d 个节点", len(nodes))
            return list(self._nodes)

    def _begin_transition(self) -> None:
        # 进入建房/加入流程前占用状态迁移；非 idle 或已有迁移进行中时拒绝并发。
        with self._transition_lock:
            if self._transitioning or self._mode != "idle":
                raise ConnectorError("当前已有活跃的房间或正在建立连接，请先退出或稍候")
            self._transitioning = True

    def _end_transition(self) -> None:
        # 结束建房/加入流程的状态迁移占用（成功与失败路径都必须调用）。
        with self._transition_lock:
            self._transitioning = False

    def fetch_nodes(self, *, force: bool = False) -> list[str]:
        """
        按当前策略为下一次连接解析节点，活动房间继续使用原节点。

        :param force: 是否强制刷新公共节点缓存
        :return: 保序去重的 EasyTier 节点 URI
        """
        settings = self._node_settings_provider() if self._node_settings_provider else ConnectorNodeConfig()
        if settings.mode == "automatic":
            return self._fetch_public_nodes(force=force)
        settings = ConnectorNodeSettings.for_connection(settings)
        if settings.mode == "custom":
            return list(settings.nodes)
        public_nodes = self._fetch_public_nodes(force=force)
        return list(dict.fromkeys([*settings.nodes, *public_nodes])) if settings.mode == "append" else public_nodes

    def preload_nodes(self) -> list[str]:
        """
        预热公共节点缓存，不校验或连接用户填写的节点。

        :return: 公共节点缓存；仅自定义模式不请求公共节点
        """
        settings = self._node_settings_provider() if self._node_settings_provider else ConnectorNodeConfig()
        return [] if settings.mode == "custom" else self._fetch_public_nodes()

    def start_node_preload(self) -> bool:
        """
        服务启动后在后台读取一次节点列表，立即返回，不等待网络。

        主窗口就绪或兼容 IPC 可以重复调用；线程和去重状态由后端拥有，前端重载
        不会重新创建任务。仅自定义节点模式仍由 preload_nodes 保证不访问公共节点。

        :return: 首次启动返回 True；已启动或已关闭返回 False
        """
        with self._node_cache_lock:
            if self._node_preload_started or self._node_preload_stopped.is_set():
                return False
            context = copy_context()
            thread = Thread(target=lambda: context.run(self._run_node_preload), name="ECL-NodePreload", daemon=True)
            self._node_preload_thread = thread
            self._node_preload_started = True
            thread.start()
            return True

    def _run_node_preload(self) -> None:
        """
        节点预热沿用当前操作的日志信息；服务关闭后忽略预热结果和网络错误。
        """
        if self._node_preload_stopped.is_set():
            return
        with operation_scope("后台加载公共联机节点", timed=True) as operation:
            operation.log(logger, "开始后台加载公共联机节点", logging.DEBUG, include_duration=False)
            try:
                self.preload_nodes()
            except Exception:
                if not self._node_preload_stopped.is_set():
                    logger.exception("后台加载公共联机节点发生未预期异常")
            else:
                if not self._node_preload_stopped.is_set():
                    operation.log(logger, "后台公共联机节点已就绪", logging.DEBUG)

    def _fetch_public_nodes(self, *, force: bool = False) -> list[str]:
        """
        获取可用的 EasyTier 中继节点 URI 列表。

        从 Qomicex 节点服务拉取节点，聚合的 ``https://`` 节点会二次请求解析出实际 URI。
        失败时回退到内置默认节点，保证建房/加入不会因取节点失败而中断。

        :param force: 是否忽略有效缓存并强制刷新
        :returns: 去重保序的节点 URI 列表
        """
        with self._node_cache_lock:
            if self._node_preload_stopped.is_set():
                return list(self._nodes)
            now = monotonic()
            if (
                not force
                and self._node_cache_refreshed_at is not None
                and now - self._node_cache_refreshed_at < self._node_cache_ttl
            ):
                return list(self._nodes)

            cached_nodes = list(self._nodes) if self._node_cache_refreshed_at is not None else []
            if self._node_refreshing:
                # 另一个线程正在刷新：直接返回现有缓存，避免持锁等待网络。
                return cached_nodes or list(self.default_nodes)
            self._node_refreshing = True

        try:
            return self._refresh_nodes(cached_nodes)
        finally:
            with self._node_cache_lock:
                self._node_refreshing = False

    def _refresh_nodes(self, cached_nodes: list[str]) -> list[str]:
        """
        向节点服务刷新缓存；失败时保留旧缓存或使用内置默认节点。
        """
        if self._http is None:
            return self._remember_nodes(cached_nodes or list(self.default_nodes))
        try:
            response = self._http.get(
                self.node_list_url,
                headers={"User-Agent": self.node_user_agent},
                timeout=self.node_fetch_timeout_seconds,
            )
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list):
                raise ValueError("节点服务返回了非数组结构")

            nodes: list[str] = []
            aggregate_urls: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                if self._is_easytier_peer(url):
                    nodes.append(url)
                elif url.startswith("https://"):
                    aggregate_urls.append(url)

            if aggregate_urls:
                with ThreadPoolExecutor(max_workers=min(len(aggregate_urls), self.max_aggregate_workers)) as pool:
                    futures = [
                        pool.submit(copy_context().run, self._resolve_aggregate_node, url) for url in aggregate_urls
                    ]
                    for future in futures:
                        nodes.extend(future.result())
            resolved = list(dict.fromkeys(nodes)) or list(self.default_nodes)
            return self._remember_nodes(resolved)
        except Exception as exc:
            if self._node_preload_stopped.is_set():
                return cached_nodes or list(self.default_nodes)
            fallback = cached_nodes or list(self.default_nodes)
            logger.warning(
                "拉取联机节点列表失败，使用%s: %s",
                "内存中的旧缓存" if cached_nodes else "默认节点",
                exc,
            )
            return self._remember_nodes(fallback)

    def _resolve_aggregate_node(self, url: str) -> list[str]:
        # 解析一个聚合节点地址，得到可用的 EasyTier URI。
        if self._node_preload_stopped.is_set():
            return []
        try:
            response = self._http.get(
                url,
                headers={"User-Agent": self.node_user_agent},
                timeout=self.node_fetch_timeout_seconds,
            )
            response.raise_for_status()
            text = response.text.strip()
            if not text:
                return []
            try:
                payload = response.json()
                if isinstance(payload, list):
                    return [
                        str(node.get("url")).strip()
                        for node in payload
                        if isinstance(node, dict) and self._is_easytier_peer(str(node.get("url") or ""))
                    ]
            except Exception:
                pass
            if self._is_easytier_peer(text):
                return [text]
            return []
        except Exception as exc:
            logger.debug("解析聚合节点失败: %s", exc)
            return []

    @staticmethod
    def _is_easytier_peer(value: str) -> bool:
        # 判断一个字符串是否为 easytier 原生支持的节点 URI。
        return value.startswith(ConnectorService.easytier_schemes)

    @staticmethod
    def _to_frontend_player(player: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": player.get("name", ""),
            "vendor": player.get("vendor", ""),
            "iconBase64": None,
            "kind": "host" if str(player.get("kind", "")).upper() == "HOST" else "guest",
            "machineId": player.get("machine_id"),
        }

    def _current_players(self) -> list[dict[str, Any]]:
        if self._mode == "host" and self._room_server is not None:
            try:
                raw = self._room_server.sync_get_player_list()
                return [self._to_frontend_player(p) for p in raw if isinstance(p, dict)]
            except Exception:
                return []
        if self._mode == "guest" and self._client is not None:
            loop = getattr(self._client, "loop", None)
            writer = getattr(self._client, "writer", None)
            # 连接断开后客户端事件循环可能已关闭；此时提交协程会触发警告
            if loop is not None and writer is not None and not loop.is_closed() and loop.is_running():
                try:
                    status, body = asyncio.run_coroutine_threadsafe(
                        self._client.send_request("c:player_profiles_list", b""),
                        loop,
                    ).result(timeout=self.player_list_fetch_timeout_seconds)
                    if status == 0:
                        raw = json.loads(body.decode("utf-8"))
                        return [self._to_frontend_player(p) for p in raw if isinstance(p, dict)]
                except Exception:
                    pass
            # 列表获取失败时至少展示自己，避免玩家列表为空
            return [
                {
                    "name": self._client.player_name,
                    "vendor": self._client.vendor,
                    "iconBase64": None,
                    "kind": "guest",
                    "machineId": self._client.machine_id,
                }
            ]
        return self._players

    def get_status(self) -> dict[str, Any]:
        """
        获取当前联机状态。

        :returns: 包含 mode, roomCode, mcHost, mcPort, gameInfo, players, error 的字典
        """
        status = {
            "mode": self._mode,
            "roomCode": self._room_code,
            "mcHost": self._mc_host,
            "mcPort": self._mc_port,
            "gameInfo": dict(self._game_info) if self._game_info is not None else None,
            "players": self._current_players(),
            "nodes": self._nodes,
            "error": self._error,
        }
        return self.extensions.enrich_status(self._session_context(), status)

    def _session_context(self) -> ConnectorSessionContext:
        # 向插件提供联机会话信息，但不暴露底层 socket。
        client = self._client
        machine_id = getattr(client, "machine_id", None)
        request = self._send_extension_request if client is not None else None
        return ConnectorSessionContext(
            mode=self._mode,
            room_code=self._room_code,
            machine_id=str(machine_id) if machine_id else None,
            game_info=dict(self._game_info) if self._game_info is not None else None,
            _request=request,
            _local_icon_provider=self._local_player_icon_provider,
        )

    def _send_extension_request(self, protocol: str, body: bytes = b"") -> tuple[int, bytes]:
        # 通过线程安全的方法向当前房主发送扩展协议请求。
        client = self._client
        if client is None:
            raise RuntimeError("当前不是房客会话")
        loop = getattr(client, "loop", None)
        if loop is None or loop.is_closed() or not loop.is_running():
            raise RuntimeError("联机协议客户端尚未就绪")
        return asyncio.run_coroutine_threadsafe(client.send_request(protocol, body), loop).result(
            timeout=self.extension_request_timeout_seconds
        )

    def _extension_protocol_handlers(self) -> dict[str, Callable[..., Any]]:
        # 把插件注册表适配为 Florolding 服务端处理器。
        handlers: dict[str, Callable[..., Any]] = {}
        for protocol in self.extensions.protocol_names():

            async def handle(request_body: bytes, writer: Any, protocol_name: str = protocol) -> tuple[int, bytes]:
                server = self._room_server
                peer_machine_id = None
                remove_player = None
                if server is not None:
                    peer_machine_id = server.machine_ids.get(writer)
                    remove_player = server.remove_player
                request = ConnectorProtocolRequest(
                    protocol=protocol_name,
                    body=request_body,
                    peer_machine_id=peer_machine_id,
                    game_info=dict(self._game_info) if self._game_info is not None else None,
                    _remove_player=remove_player,
                )
                return await self.extensions.dispatch(request)

            handlers[protocol] = handle
        return handlers

    def get_easytier_status(self) -> dict[str, Any]:
        """
        获取 EasyTier 安装状态。

        :returns: 包含 installed, status, progress, speed, error 的字典
        """
        return {
            "installed": True,
            "status": "installed",
            "progress": 100,
            "speed": 0,
            "error": None,
        }

    def get_nat_type(self) -> dict[str, str | int | bool | None]:
        """
        使用 EasyTier 内置 STUN 探测检测 NAT 类型。

        活跃房间会直接复用当前 EasyTier 节点的 STUN 快照；空闲时启动一个无 TUN、
        无中继连接的临时节点，取得结果后立即停止。类型归类沿用 EasyTier
        的判定：开放与各类锥形 NAT 归为 ``cone``，对称映射归为 ``symmetric``。

        同一服务只允许一个探测任务；锁不会阻塞房间状态操作。轮询窗口有界，
        但不能中断卡住的原生调用。临时节点在成功和失败时均尝试停止，复用的
        房间节点不会被本方法关闭。

        :return: NAT 大类、详细类型、公网地址、端口范围与 IPv6 探测状态
        :raises ConnectorNatError: 已有探测、无有效信息而超时或节点操作失败时抛出
        """
        if not self._nat_probe_lock.acquire(blocking=False):
            raise ConnectorNatError("已有 NAT 检测正在进行，请稍后重试", "CONNECTOR_NAT_TYPE_BUSY")
        node = self._easy_tier_node
        owns_node = node is None
        try:
            if owns_node:
                node = easytier_pyo3.Node(
                    {
                        "instance_name": "ecl-nat-probe",
                        "network_identity": {
                            "network_name": f"ecl-nat-probe-{uuid4().hex}",
                            "network_secret": uuid4().hex,
                        },
                        "listeners": [],
                        "peer": [],
                        "dhcp": False,
                        "flags": {"no_tun": True, "bind_device": False, "enable_ipv6": True},
                    }
                )
            if owns_node:
                node.start()
            started_at = monotonic()
            latest_result: dict[str, str | int | bool | None] | None = None
            while monotonic() - started_at < self.nat_probe_timeout_seconds:
                node_info = node.node_info()
                result = self._nat_result_from_stun_info(node_info.get("stun_info"))
                if result is not None:
                    if result["publicIp"] or result["publicPort"]:
                        latest_result = result
                    if result["detailType"] == "unknown":
                        sleep(self.nat_probe_interval_seconds)
                        continue
                    logger.info(
                        "NAT 检测完成：详细结果：%s；IPv6：%s",
                        result["detailType"],
                        ("是" if result["supportsIpv6"] else "否"),
                    )
                    return result
                sleep(self.nat_probe_interval_seconds)
            logger.warning("NAT 检测超时，未取得完整的 EasyTier STUN 类型")
            if latest_result is not None:
                return latest_result
            raise ConnectorNatError("NAT 检测超时，请检查网络后重试", "CONNECTOR_NAT_TYPE_TIMEOUT")
        except ConnectorNatError:
            raise
        except Exception as exc:
            logger.warning("NAT 检测失败", exc_info=True)
            raise ConnectorNatError("NAT 探测失败，请稍后重试", "CONNECTOR_NAT_TYPE_FAILED") from exc
        finally:
            try:
                if owns_node and node is not None:
                    self._stop_nat_probe(node)
            finally:
                self._nat_probe_lock.release()

    @staticmethod
    def _stop_nat_probe(node: Any) -> None:
        """
        尝试关闭已创建的临时原生节点，不覆盖探测本身的结果或异常。

        原生资源清理失败仅记录日志，调用方仍需释放检测锁。
        """
        try:
            node.stop()
        except Exception:
            logger.warning("停止 NAT 临时探测节点失败", exc_info=True)

    @staticmethod
    def _nat_result_from_stun_info(stun_info: Any) -> dict[str, str | int | bool | None] | None:
        """
        校验原生节点快照并转换为稳定的 NAT 结果结构。

        丢弃无效地址和端口，不把缺失 IPv6 数据视为网络不支持 IPv6。
        """
        if not isinstance(stun_info, dict) or not stun_info:
            return None

        raw_type = str(stun_info.get("udp_nat_type") or "Unknown").strip()
        normalized_type = raw_type.casefold().replace("_", "").replace("-", "")
        detail_types = {
            "openinternet": "openInternet",
            "nopat": "noPat",
            "fullcone": "fullCone",
            "restricted": "restricted",
            "portrestricted": "portRestricted",
            "symmetriceasy": "symmetricEasy",
            "symmetriceasyinc": "symmetricEasy",
            "symmetriceasydec": "symmetricEasy",
            "symmetric": "symmetric",
            "symmetricfirewall": "symmetricFirewall",
            "symudpfirewall": "symmetricFirewall",
            "udpblocked": "udpBlocked",
        }
        detail_type = detail_types.get(normalized_type, "unknown")
        if detail_type in {"openInternet", "noPat", "fullCone", "restricted", "portRestricted"}:
            nat_type = "cone"
        elif detail_type in {"symmetricEasy", "symmetric", "symmetricFirewall"}:
            nat_type = "symmetric"
        elif detail_type == "udpBlocked":
            nat_type = "blocked"
        else:
            nat_type = "unknown"

        public_ips = stun_info.get("public_ip")
        if not isinstance(public_ips, list):
            public_ips = []
        valid_ips: list[str] = []
        for value in public_ips:
            try:
                valid_ips.append(str(ip_address(str(value).strip())))
            except ValueError:
                continue
        ipv4 = next((value for value in valid_ips if ":" not in value), None)
        public_ip = ipv4 or (valid_ips[0] if valid_ips else None)

        def valid_port(value: Any) -> int | None:
            try:
                port = int(value)
            except (TypeError, ValueError):
                return None
            return port if 0 < port <= ConnectorService.max_tcp_port else None

        public_port = valid_port(stun_info.get("min_port"))
        public_port_end = valid_port(stun_info.get("max_port"))
        if public_port is None:
            public_port = public_port_end
        if public_port_end is None:
            public_port_end = public_port

        return {
            "type": nat_type,
            "detailType": detail_type,
            "publicIp": public_ip,
            "publicPort": public_port,
            "publicPortEnd": public_port_end,
            "supportsIpv6": any(":" in value for value in valid_ips),
        }

    def host_port(self, port: int) -> dict[str, Any]:
        """
        以指定端口创建联机房间。

        :param port: Minecraft 服务器端口
        :returns: 包含 roomCode 的字典
        :raises ConnectorNotAvailableError: 依赖缺失时抛出
        :raises ConnectorError: 房间创建失败时抛出
        """
        if self._mode != "idle":
            raise ConnectorError("当前已有活跃的房间，请先退出")

        # 占用状态迁移：防止建房期间再次建房/加入覆盖房间状态导致句柄泄漏
        self._begin_transition()
        self._mode = "starting"

        logger.debug(
            "开始创建联机房间；游戏端口：%s；EasyTier：%s", port, "可用" if self.easytier_available else "不可用"
        )
        try:
            logger.info("准备创建联机房间，开始检查节点配置")
            self._nodes = self.fetch_nodes()
            logger.info("联机节点准备完成；节点数量：%s", len(self._nodes))
            florolding = Florolding(
                launcher_info=self._launcher_info,
                log_callback=self._scoped_log_callback(),
            )
            logger.debug("Florolding 实例已创建")

            # 设置 EasyTier 节点列表
            florolding.set_nodes(self._nodes)
            logger.debug("已设置 EasyTier 节点列表")

            create_kwargs: dict[str, Any] = {}
            if _supports_kwarg(florolding.create_room, "protocol_handlers"):
                create_kwargs["protocol_handlers"] = self._extension_protocol_handlers()
            else:
                logger.debug("当前 Florolding 版本不支持 protocol_handlers，跳过插件扩展协议注册")
            room_code, server, easy_tier_node = florolding.create_room(
                player_name=self._player_name,
                minecraft_port=port,
                **create_kwargs,
            )
            logger.debug(
                "房间已创建：房间码：%s；服务端：%s；EasyTier节点编号：%s",
                "<已隐藏>",
                type(server).__name__,
                easy_tier_node.peer_id() if easy_tier_node else "N/A",
            )

            self._mode = "host"
            self._room_code = room_code
            self._mc_host = "127.0.0.1"
            self._mc_port = port
            self._room_server = server
            self._easy_tier_node = easy_tier_node
            self._error = None

            logger.info("联机房间创建成功：房间码：%s；游戏端口：%s", "<已隐藏>", port)
            return {"roomCode": room_code}
        except Exception as exc:
            self._mode = "idle"
            self._error = str(exc)
            self._end_transition()
            logger.exception("创建联机房间失败: %s", exc)
            if isinstance(exc, ConnectorNodeError):
                raise
            raise ConnectorError(f"创建房间失败: {exc}") from exc
        finally:
            if self._mode == "host":
                self._end_transition()

    def host_instance(self, game_path: str, version_id: str) -> dict[str, Any]:
        """
        为指定实例创建联机房间。

        先探测本机 Java 进程监听且确认是 Minecraft 服务的端口，再据此建房；
        实例尚未启动（找不到服务端口）时返回失败，由前端引导用户先启动实例。

        :param game_path: 游戏路径
        :param version_id: 版本 ID
        :returns: 包含 roomCode 的字典
        :raises ConnectorError: 实例未运行或房间创建失败时抛出
        """
        if self._mode != "idle":
            raise ConnectorError("当前已有活跃的房间，请先退出")

        candidates = self._minecraft_listener_ports()
        detected = self.search_mc_port(candidates).get("port")
        if not isinstance(detected, int):
            raise ConnectorError("未检测到运行中的实例服务端口，请先启动实例后再联机")
        logger.debug(
            "实例联机建房：游戏目录：%s；版本标识：%s；检测端口：%s",
            game_path,
            version_id,
            detected,
        )
        return self.host_port(detected)

    def join(self, code: str) -> dict[str, Any]:
        """
        加入联机房间。

        :param code: 房间码（格式 U/NNNN-NNNN-SSSS-SSSS）
        :returns: 包含 mcHost, mcPort 的字典
        :raises ConnectorNotAvailableError: 依赖缺失时抛出
        :raises ConnectorError: 加入失败时抛出
        """
        if self._mode != "idle":
            raise ConnectorError("当前已有活跃的房间，请先退出")

        if not validate_code(code):
            raise ConnectorError("无效的房间码格式")

        # 占用状态迁移：加入期间再次建房/加入会被拒绝
        self._begin_transition()
        self._mode = "starting"
        self._room_code = code
        self._error = None
        logger.debug("开始获取联机节点")
        try:
            self._nodes = self.fetch_nodes()
            logger.info("加入房间前的节点准备完成；节点数量：%s", len(self._nodes))
            florolding = Florolding(
                launcher_info=self._launcher_info,
                log_callback=self._scoped_log_callback(),
            )
            florolding.set_nodes(self._nodes)
            logger.debug("开始加入联机房间：房间码：%s；玩家名称：%s", "<已隐藏>", "<已隐藏>")

            node, mc_port = self._join_room(florolding, code, self._player_name)
            self._mc_host = "127.0.0.1"
            self._mc_port = mc_port
            self._easy_tier_node = node
            self._mode = "guest"
            self._error = None

            logger.info("已加入联机房间：房间码：%s；游戏端口：%s", "<已隐藏>", mc_port)
            return {"mcHost": self._mc_host, "mcPort": self._mc_port}
        except Exception as exc:
            self._mode = "idle"
            self._error = str(exc)
            self._stop_async_thread_client()
            self._end_transition()
            logger.exception("加入联机房间失败: %s", exc)
            if isinstance(exc, ConnectorNodeError):
                raise
            raise ConnectorError(f"加入房间失败: {exc}") from exc
        finally:
            if self._mode == "guest":
                self._end_transition()

    def _scoped_log_callback(self) -> Callable[[str, str], None]:
        """
        保存本次联机的日志信息，让第三方回调线程也能记录同一次操作。

        :return: 执行原回调并记录本次联机日志的回调函数
        """
        trace = current_operation()

        def emit(level: str, message: str) -> None:
            with trace_scope(trace):
                self._log_callback(level, message)

        return emit

    def _join_room(
        self,
        florolding: Florolding,
        room_code: str,
        player_name: str,
        conn_timeout: int = 30,
    ) -> tuple[Any, int]:
        join_kwargs: dict[str, Any] = {}
        if _supports_kwarg(florolding.join_room, "supported_protocols"):
            join_kwargs["supported_protocols"] = self.extensions.protocol_names()
        else:
            logger.debug("当前 Florolding 版本不支持 supported_protocols，跳过插件扩展协议协商")
        client, node = florolding.join_room(
            room_code,
            player_name,
            conn_timeout,
            **join_kwargs,
        )
        self._client = client

        bind_mc_port = find_free_port()
        bind_result = florolding.bind_mc_port(client, node, bind_mc_port=bind_mc_port, timeout=conn_timeout)
        # 新版本返回 (bool, broadcaster)，旧版本返回 bool
        bind_ok = bool(bind_result[0]) if isinstance(bind_result, tuple) else bool(bind_result)
        if not bind_ok:
            node.stop()
            raise ConnectorError("绑定 Minecraft 端口失败")
        self.extensions.guest_joined(self._session_context())
        return node, bind_mc_port

    def leave(self) -> dict[str, Any]:
        """
        退出联机房间。

        :returns: 包含 status 的字典
        """
        previous_context = self._session_context()
        logger.info("开始退出联机房间并清理网络资源")
        if self._mode != "idle":
            self.extensions.before_leave(previous_context)
        self._stop_async_thread_client()
        server = self._room_server
        if server is not None and hasattr(server, "sync_stop"):
            try:
                server.sync_stop()
            except Exception:
                logger.debug("停止联机服务端失败", exc_info=True)

        node = self._easy_tier_node
        if node is not None and hasattr(node, "stop"):
            try:
                node.stop()
            except Exception:
                logger.debug("停止 EasyTier 节点失败", exc_info=True)

        self._mode = "idle"
        self._room_code = None
        self._mc_host = None
        self._mc_port = None
        self._room_server = None
        self._easy_tier_node = None
        self._client = None
        self._game_info = None
        self._error = None
        # 若退出发生在建房/加入迁移进行中，同步释放迁移占用
        self._end_transition()
        self.extensions.reset(
            ConnectorSessionContext(
                mode="idle",
                room_code=None,
                machine_id=previous_context.machine_id,
                game_info=None,
            )
        )
        logger.info("联机房间资源清理完成，已恢复空闲状态")
        return {"status": "left"}

    def close(self) -> None:
        """
        关闭联机服务，停止运行中的房间与 EasyTier 节点。

        应用关闭时，在其他依赖此服务的对象关闭后调用。节点预热停止提交后续网络请求，已进入的
        有限超时请求仅短暂等待收尾，结果不再写入缓存；加入房间守护线程由退出回收。
        """
        self._node_preload_stopped.set()
        with self._node_cache_lock:
            preload_thread = self._node_preload_thread
        if preload_thread is not None and preload_thread is not current_thread():
            preload_thread.join(timeout=0.1)
        try:
            self.leave()
        except Exception:
            logger.debug("关闭联机服务失败", exc_info=True)

    def _stop_async_thread_client(self) -> None:
        # 停止 florolding 客户端线程：取消心跳并断开连接。
        client = self._client
        if client is None:
            return
        loop = getattr(client, "loop", None)
        # 事件循环未运行时无法执行停止任务，直接清理引用
        if loop is None or loop.is_closed() or not loop.is_running():
            self._client = None
            return

        async def _stop() -> None:
            task = getattr(client, "heartbeat_task", None)
            if task is not None:
                task.cancel()
            if getattr(client, "writer", None) is not None:
                await client.disconnect()

        try:
            asyncio.run_coroutine_threadsafe(_stop(), loop)
        except (RuntimeError, TypeError):
            logger.debug("停止 florolding 客户端失败", exc_info=True)
        finally:
            self._client = None

    def kick(self, machine_id: str) -> dict[str, Any]:
        """
        移出玩家。

        :param machine_id: 目标玩家机器 ID
        :returns: 包含 status 的字典
        """
        # 房主模式下玩家列表来自房间服务器，必须同步移除，否则列表刷新后玩家会"复活"。
        server = self._room_server
        if self._mode == "host" and server is not None:
            loop = getattr(server, "loop", None)
            if loop is not None and not loop.is_closed() and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(server.remove_player(machine_id), loop).result(timeout=5)
                except Exception:
                    logger.warning("房间服务器移除玩家失败: %s", machine_id, exc_info=True)
        self._players = [p for p in self._players if p.get("machineId") != machine_id]
        logger.info("已处理移出玩家请求；当前玩家数量：%s", len(self._players))
        return {"status": "kicked"}

    def detect_ports(self) -> dict[str, Any]:
        """
        探测本机 Java 进程开放的候选端口。

        Minecraft 的“对局域网开放”会分配随机端口，不能依赖 25565 等固定端口。
        此处先从 Java 进程的监听套接字缩小范围，供后续搜索 MC 端口使用。

        :returns: 包含 ports 的字典（候选端口列表）
        """
        return {"ports": self._minecraft_listener_ports()}

    def search_mc_port(self, ports: list[int]) -> dict[str, Any]:
        """
        在候选端口中搜索确认 Minecraft 服务端口。

        对每个候选端口发送无副作用的状态请求确认对端确实是 Minecraft 服务端，
        避免把其他应用的开放端口误用于联机。

        :param ports: 候选端口列表
        :returns: 包含 port 的字典（未找到时 port 为 None）
        """
        for port in ports:
            if self._is_minecraft_server(port):
                logger.info("发现本地 Minecraft 服务端口: %s", port)
                return {"port": port}
        logger.debug("未发现本地 Minecraft 服务端口")
        return {"port": None}

    @staticmethod
    def _minecraft_listener_ports() -> list[int]:
        # 获取 Java 进程正在监听的 TCP 端口。
        java_pids: set[int] = set()
        try:
            for process in psutil.process_iter(["pid", "name", "exe"]):
                info = process.info
                executable = " ".join(str(info.get(key) or "") for key in ("name", "exe")).lower()
                if "java" in executable and isinstance(info.get("pid"), int):
                    java_pids.add(info["pid"])
        except (psutil.AccessDenied, psutil.Error) as exc:
            logger.debug("枚举 Java 进程失败: %s", exc)

        if not java_pids:
            return []

        try:
            connections = psutil.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.Error) as exc:
            logger.debug("读取本地监听端口失败: %s", exc)
            return []

        ports = {
            connection.laddr.port
            for connection in connections
            if connection.pid in java_pids
            and connection.status == psutil.CONN_LISTEN
            and connection.laddr
            and isinstance(connection.laddr.port, int)
            and 0 < connection.laddr.port <= ConnectorService.max_tcp_port
        }
        return sorted(ports)

    @staticmethod
    def _is_minecraft_server(port: int, timeout: float | None = None) -> bool:
        # 通过 Minecraft Status 协议确认本地端口的服务类型。
        effective_timeout = ConnectorService.status_probe_timeout_seconds if timeout is None else timeout
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=effective_timeout) as connection:
                connection.settimeout(effective_timeout)
                handshake = b"\x00" + ConnectorService._encode_varint(758) + ConnectorService._encode_varint(9)
                handshake += b"localhost" + struct.pack(">H", port) + b"\x01"
                connection.sendall(ConnectorService._encode_varint(len(handshake)) + handshake)
                connection.sendall(b"\x01\x00")

                packet_length = ConnectorService._read_varint(connection)
                if packet_length is None or not 1 <= packet_length <= ConnectorService.max_status_packet_bytes:
                    return False
                packet = ConnectorService._read_exact(connection, packet_length)
                if packet is None:
                    return False

            packet_id, offset = ConnectorService._decode_varint(packet)
            if packet_id != 0:
                return False
            json_length, offset = ConnectorService._decode_varint(packet, offset)
            if json_length is None or json_length < 2 or offset + json_length != len(packet):
                return False
            status = json.loads(packet[offset : offset + json_length].decode("utf-8"))
            return isinstance(status, dict) and isinstance(status.get("version"), dict)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, struct.error):
            return False

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        # 将非负 Minecraft VarInt 编码为字节串。
        encoded = bytearray()
        while True:
            current = value & 0x7F
            value >>= 7
            encoded.append(current | (0x80 if value else 0))
            if not value:
                return bytes(encoded)

    @staticmethod
    def _read_varint(connection: socket.socket) -> int | None:
        # 从套接字读取一个至多五字节的 Minecraft VarInt。
        encoded = bytearray()
        for _ in range(5):
            byte = ConnectorService._read_exact(connection, 1)
            if byte is None:
                return None
            encoded.extend(byte)
            if not byte[0] & 0x80:
                value, _ = ConnectorService._decode_varint(encoded)
                return value
        return None

    @staticmethod
    def _decode_varint(data: bytes | bytearray, offset: int = 0) -> tuple[int | None, int]:
        # 解析内存中的 Minecraft VarInt，并返回值与下一个偏移量。
        value = 0
        for index in range(5):
            if offset + index >= len(data):
                return None, offset
            byte = data[offset + index]
            value |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                return value, offset + index + 1
        return None, offset

    @staticmethod
    def _read_exact(connection: socket.socket, length: int) -> bytes | None:
        # 读取定长响应，连接提前关闭时返回 None。
        chunks = bytearray()
        while len(chunks) < length:
            chunk = connection.recv(length - len(chunks))
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)
