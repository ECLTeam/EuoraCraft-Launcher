# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：宿主认证游标的有界分页快照，供插件运行时各查询服务复用。
#
# 公开接口：
#   - class ResourcePages — 有界且绑定查询条件的分页快照。
# ============================================================

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from uuid import uuid4

from ECL.plugins.api.contracts import JsonValue, ProtocolError


@dataclass(frozen=True, slots=True)
class _Snapshot:
    signature: str
    generation: str
    expires_at: float
    items: tuple[dict[str, JsonValue], ...]


class ResourcePages:
    """
    最多持有 16 个短期分页快照；游标由宿主认证，不信任客户端 offset。
    """

    def __init__(self) -> None:
        """
        创建只属于一个服务或运行的分页密钥及内存表。
        """
        self._secret = uuid4().bytes
        self._snapshots: dict[str, _Snapshot] = {}
        self._lock = RLock()

    def _cursor(self, snapshot_id: str, index: int) -> str:
        payload = f"{snapshot_id}.{index}"
        digest = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{digest}"

    def page(
        self,
        signature: str,
        generation: str,
        cursor: str | None,
        limit: int,
        load: Callable[[], tuple[dict[str, JsonValue], ...]],
        *,
        project: Callable[[dict[str, JsonValue]], dict[str, JsonValue]] | None = None,
    ) -> dict[str, JsonValue]:
        """
        创建或读取绑定查询及代次的分页，按结果字节数提前分页。

        :param signature: 服务端从调用主体及完整查询条件构造的签名
        :param generation: 授权和资源版本
        :param cursor: 宿主生成的短期游标
        :param limit: 已校验的分页条目上限
        :param load: 锁外执行的实际读取
        :param project: 按页交付时投影最新视图；快照仍只保存原始引用集合
        :return: items 和下一游标
        :raises ProtocolError: 游标失效或容量耗尽时抛出
        """
        now = monotonic()
        with self._lock:
            self._snapshots = {key: value for key, value in self._snapshots.items() if value.expires_at > now}
            if cursor is None:
                snapshot_id, index = uuid4().hex, 0
            else:
                try:
                    snapshot_id, position, _ = cursor.split(".")
                    index = int(position)
                except (ValueError, AttributeError) as exc:
                    raise ProtocolError("资源分页游标无效", "PLUGIN_CURSOR_INVALID") from exc
                if not hmac.compare_digest(cursor, self._cursor(snapshot_id, index)):
                    raise ProtocolError("资源分页游标无效", "PLUGIN_CURSOR_INVALID")
                snapshot = self._snapshots.get(snapshot_id)
                if snapshot is None or snapshot.signature != signature or snapshot.generation != generation:
                    raise ProtocolError("资源分页游标已失效", "PLUGIN_CURSOR_INVALID")
        if cursor is None:
            snapshot = _Snapshot(signature, generation, now + 120, load())
        items: list[JsonValue] = []
        size = 256
        for item in snapshot.items[index : index + limit]:
            if project is not None:
                item = project(item)
            item_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
            if size + item_size > 60000:
                if not items:
                    raise ProtocolError("实例结果超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
                break
            items.append(item)
            size += item_size
        following = index + len(items)
        next_cursor = self._cursor(snapshot_id, following) if following < len(snapshot.items) else None
        with self._lock:
            if next_cursor is not None:
                if cursor is None and len(self._snapshots) >= 16:
                    raise ProtocolError("分页快照数量超过上限", "PLUGIN_RATE_LIMITED")
                self._snapshots[snapshot_id] = snapshot
            else:
                self._snapshots.pop(snapshot_id, None)
        return {"items": items, "next_cursor": next_cursor}

    def discard(self, prefix: str) -> None:
        """
        强制刷新时仅失效指定查询范围的快照，不丢弃其他查询游标。

        :param prefix: 宿主生成的业务查询前缀
        """
        with self._lock:
            self._snapshots = {
                key: item for key, item in self._snapshots.items() if not item.signature.startswith(prefix)
            }

    def clear(self) -> None:
        """
        释放本运行全部分页及旧游标，不影响其他运行。
        """
        with self._lock:
            self._snapshots.clear()
