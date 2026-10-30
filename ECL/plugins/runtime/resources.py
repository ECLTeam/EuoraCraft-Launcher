# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：绑定每次插件运行的实例引用和有界分页，读取及交付均复核资源授权。
#
# 公开接口：
#   - class InstanceQueries — 游戏查询的窄协议。
#   - class ResourcePages — 有界且绑定查询条件的分页快照。
#   - class RuntimeResources — 当前运行唯一持有的共享实例资源表。
# ============================================================

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantError, PluginGrantSession
from ECL.plugins.resource_catalog import PluginResource, PluginResourceCatalog
from ECL.plugins.runtime.models import InstanceGetRequest, InstanceScanRequest, ResourceListRequest
from ECL.utils.instance_reading import InstanceReadBoundary, InstanceReadError

if TYPE_CHECKING:
    from ECL.services.game.instance_profile_transactions import InstanceProfileTarget
    from ECL.services.game.instance_queries import InstanceSummary
    from ECL.services.game.plugin_games import GameResourceTarget, InstalledGame


class InstanceQueries(Protocol):
    """
    当前应用提供的只读游戏查询协议，不包含账户、配置或写接口。
    """

    def query_instance_names(self, root_path: Path, deadline: float) -> tuple[str, ...]:
        """
        有界枚举直接实例名称，不写入游戏目录。

        :param root_path: 已批准的根目录
        :param deadline: 当前调用截止时间
        :return: 本地实例名称
        """
        ...

    def query_instance_summary(
        self, root_path: Path, version_id: str, deadline: float, *, reader: InstanceReadBoundary | None = None
    ) -> InstanceSummary:
        """
        读取指定实例的公开摘要，不扫描其他实例资料。

        :param root_path: 已批准根目录
        :param version_id: 指定实例名
        :param deadline: 本次调用截止时间
        :param reader: 一次扫描共用的读取预算
        :return: 公开摘要
        """
        ...


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


@dataclass(frozen=True, slots=True)
class _Reference:
    resource: PluginResource
    grant_ref: str


class RuntimeResources:
    """
    资源引用只存在于本运行；每次读取和结果交付都检查授权及物理身份。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        catalog: PluginResourceCatalog,
        queries: InstanceQueries,
        refresh: Callable[[], int],
    ) -> None:
        """
        绑定宿主持有的授权、目录和只读查询，不从 Worker 请求构造主体。

        :param session: 当前运行授权
        :param catalog: 当前应用资源目录
        :param queries: 共享游戏查询接口
        :param refresh: 在授权锁外刷新资源并返回目录版本
        """
        self._session = session
        self._catalog = catalog
        self._queries = queries
        self._refresh = refresh
        self._references: dict[str, _Reference] = {}
        self._pages = ResourcePages()
        self._epoch = session.view().grant_epoch
        self._lock = RLock()

    def _prepare(self, capability: str = "instances.read") -> tuple[GrantLease, str]:
        """
        先刷新目录身份和授权代次，再取得该能力自身范围，不从读取推导写入。
        """
        revision = self._refresh()
        view = self._session.view()
        with self._lock:
            if self._epoch != view.grant_epoch:
                self.clear()
                self._epoch = view.grant_epoch
        permission = next((item for item in view.items if item.capability == capability), None)
        if permission is None and capability == "instances.read":
            permission = next((item for item in view.items if item.capability == "instances.manage"), None)
        lease = self._session.authorize(capability, permission.resource_refs if permission else ())
        return lease, f"{lease.grant_epoch}:{revision}"

    def permission_refs(self, capability: str = "instances.read") -> list[str]:
        """
        将获准游戏根转换为当前运行引用，不交付持久资源标识。

        :param capability: 查看或管理游戏
        :return: 获准根目录的运行引用
        """
        lease, _ = self._prepare(capability)
        return [self._reference(self._catalog.resolve(ref), ref) for ref in lease.resource_refs]

    def _reference(self, resource: PluginResource, grant_ref: str) -> str:
        target = (resource.root_path, resource.root_identity, resource.version_id, resource.identity)
        with self._lock:
            for key, reference in self._references.items():
                existing = reference.resource
                if (existing.root_path, existing.root_identity, existing.version_id, existing.identity) == target:
                    return key
            if len(self._references) >= 8192:
                raise ProtocolError("实例运行引用超过上限", "PLUGIN_RATE_LIMITED")
            key = uuid4().hex
            self._references[key] = _Reference(resource, grant_ref)
            return key

    def _resolve(self, key: str, *, is_root: bool) -> _Reference:
        """
        复用物理引用，但独立解析当前读取范围，管理能力可用于查看，引用本身不扩大目录范围。
        """
        from ECL.services.game.plugin_games import GameResourceTarget

        with self._lock:
            reference = self._references.get(key)
        if reference is None or (reference.resource.version_id is None) != is_root:
            raise ProtocolError("资源引用不属于当前运行或类别", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        resource = reference.resource
        read = self.read_game_target(
            GameResourceTarget(
                Path(resource.root_path),
                resource.root_identity,
                resource.version_id,
                resource.identity if resource.version_id is not None else None,
            ),
            monotonic() + 5,
        )
        return _Reference(resource, read.resource_refs[0])

    def _check(
        self, lease: GrantLease, resource: PluginResource | None = None, *, deadline: float | None = None
    ) -> None:
        self._refresh()
        self._session.check(lease)
        if resource is not None:
            guard = InstanceReadBoundary(Path(resource.root_path), monotonic() + 5)
            if guard.identity != resource.root_identity:
                raise InstanceReadError("游戏目录身份发生变化")
            if (
                resource.version_id is not None
                and guard.directory(Path("versions") / resource.version_id) != resource.identity
            ):
                raise InstanceReadError("实例身份发生变化")
        if deadline is not None and monotonic() >= deadline:
            raise ProtocolError("实例查询已超过等待时间", "PLUGIN_DEADLINE_EXCEEDED")

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        分派已实现的三个只读方法，结果返回前再次复核授权。

        :param method: 明确的只读方法名
        :param params: 待严格校验的 JSON 输入
        :param deadline: 当前调用截止时间
        :return: 白名单 DTO 或分页
        :raises ProtocolError: 越权、引用失效、分页或输入失败时抛出
        """
        if method == "resources.list":
            query = ResourceListRequest.model_validate(params)
            if query.capability not in {"instances.read", "instances.manage"}:
                raise ProtocolError("当前服务仅提供实例资源", "PLUGIN_SCHEMA_INVALID")
        elif method == "instances.get":
            query = InstanceGetRequest.model_validate(params)
        elif method == "instances.scan":
            query = InstanceScanRequest.model_validate(params)
        else:
            raise ProtocolError("此实例方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")
        capability = query.capability if isinstance(query, ResourceListRequest) else "instances.read"
        lease, generation = self._prepare(capability)
        if isinstance(query, ResourceListRequest):
            signature = query.model_dump_json(exclude={"cursor"})
            result = self._pages.page(signature, generation, query.cursor, query.limit, lambda: self._list(lease))
            self._check(lease, deadline=deadline)
            return result
        if isinstance(query, InstanceGetRequest):
            reference = self._resolve(query.instance_ref, is_root=False)
            self._check(lease, reference.resource, deadline=deadline)
            result = self._summary(reference, query.instance_ref, deadline)
            self._check(lease, reference.resource, deadline=deadline)
            return result
        reference = self._resolve(query.root_ref, is_root=True)
        self._check(lease, reference.resource, deadline=deadline)
        prefix = f"scan:{query.root_ref}:"
        signature = prefix + query.model_dump_json(exclude={"cursor"})
        if query.force and query.cursor is None:
            self._pages.discard(prefix)
        result = self._pages.page(
            signature, generation, query.cursor, query.limit, lambda: self._scan(reference, deadline)
        )
        self._check_scan_page(reference.resource, result, deadline)
        self._check(lease, reference.resource, deadline=deadline)
        return result

    def game_target(self, key: str, capability: str, deadline: float) -> tuple[GameResourceTarget, GrantLease]:
        """
        将当前运行引用约束到获准游戏根，并核对具体目标物理身份。

        :param key: 当前运行的根或实例引用
        :param capability: 查看或管理游戏
        :param deadline: 本次准入截止
        :return: 物理目标及原始准入
        :raises ProtocolError: 引用、权限、目录或截止失效
        """
        from ECL.services.game.plugin_games import GameResourceTarget

        self._prepare(capability)
        with self._lock:
            reference = self._references.get(key)
        if reference is None:
            raise ProtocolError("游戏引用不属于当前运行", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        resource = reference.resource
        target = GameResourceTarget(
            Path(resource.root_path),
            resource.root_identity,
            resource.version_id,
            resource.identity if resource.version_id is not None else None,
        )
        lease = self._target_lease(target, capability, deadline)
        return target, lease

    def _target_lease(self, target: GameResourceTarget, capability: str, deadline: float) -> GrantLease:
        """
        按同一根的物理身份选择当前业务准入，管理能力包含查看。
        """
        view = self._session.view()
        permissions = [
            item
            for item in view.items
            if item.decision == "allow"
            and (
                item.capability == capability
                or (capability == "instances.read" and item.capability == "instances.manage")
            )
        ]
        permissions.sort(key=lambda item: item.capability != "instances.manage")
        for permission in permissions:
            for key in permission.resource_refs:
                resource = self._catalog.resolve(key)
                if (
                    resource.version_id is None
                    and resource.root_path == str(target.root_path)
                    and resource.root_identity == target.root_identity
                ):
                    lease = self._session.authorize(permission.capability, (key,))
                    self.verify_game_target(target, lease, deadline)
                    return lease
        raise PluginGrantError("游戏目录未获准", "PLUGIN_RESOURCE_DENIED")

    def read_game_target(self, target: GameResourceTarget, deadline: float) -> GrantLease:
        """
        返回覆盖目标的原始查看或管理准入，不扩大根目录范围。

        :param target: 已绑定物理身份的目标
        :param deadline: 本次检查截止
        :return: 原始业务准入
        :raises PluginGrantError: 没有覆盖目标的有效许可
        """
        return self._target_lease(target, "instances.read", deadline)

    def verify_game_target(self, target: GameResourceTarget, lease: GrantLease, deadline: float) -> None:
        """
        执行和提交前复核原准入与同一物理对象，不为旧任务重新授予许可。

        :param target: 接纳时保存的目标
        :param lease: 原始执行或读取准入
        :param deadline: 本次检查截止
        :raises ProtocolError: 授权、目录、实例或截止已失效
        """
        self._refresh()
        self._session.check(lease)
        resource = self._catalog.resolve(lease.resource_refs[0])
        if resource.version_id is not None or resource.root_path != str(target.root_path):
            raise ProtocolError("游戏目标不匹配", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        reader = InstanceReadBoundary(target.root_path, deadline)
        if reader.identity != target.root_identity or (
            target.version_id is not None
            and reader.directory(Path("versions") / target.version_id) != target.instance_identity
        ):
            raise InstanceReadError("游戏资源物理身份已变化")
        reader.check()

    def installed_instance(self, result: InstalledGame, read: GrantLease, deadline: float) -> dict[str, JsonValue]:
        """
        为已安装结果复核原始管理根准入，只投影该新实例，不扫描其他实例。

        :param result: 执行方确认的内部安装结果
        :param read: 提交前取得的原始管理根准入
        :param deadline: 本次交付截止
        :return: 现行InstanceView，不含本机路径
        :raises ProtocolError: 原许可或结果物理身份失效
        """
        self._refresh()
        self._session.check(read)
        root = self._catalog.resolve(read.resource_refs[0])
        if root.version_id is not None or root.root_path != str(result.root_path):
            raise ProtocolError("安装结果不在原读取范围", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        reader = InstanceReadBoundary(result.root_path, deadline)
        if reader.identity != root.root_identity or (
            reader.directory(Path("versions") / result.version_name) != result.identity
        ):
            raise InstanceReadError("安装结果身份已经变化")
        resource = PluginResource(
            resource_id=root.resource_id,
            root_path=root.root_path,
            root_identity=root.root_identity,
            version_id=result.version_name,
            identity=result.identity,
            is_active=True,
        )
        key = self._reference(resource, root.resource_id)
        value = self._summary(_Reference(resource, root.resource_id), key, deadline, reader)
        self._session.check(read)
        reader.check()
        return value

    def profile_target(self, instance_ref: str, deadline: float) -> tuple[InstanceProfileTarget, GrantLease]:
        """
        通过管理根准入解析具体实例，不再叠加独立读写许可。

        :param instance_ref: 当前运行发现的实例引用
        :param deadline: 本次提交截止
        :return: 实例目标及原始管理准入
        :raises ProtocolError: 引用、管理范围或实际身份失效
        """
        from ECL.services.game.instance_profile_transactions import InstanceProfileTarget

        target, lease = self.game_target(instance_ref, "instances.manage", deadline)
        if target.version_id is None or target.instance_identity is None:
            raise ProtocolError("资料修改需要具体实例", "PLUGIN_RESOURCE_OUT_OF_SCOPE")
        return InstanceProfileTarget(
            target.root_path, target.version_id, target.root_identity, target.instance_identity
        ), lease

    def verify_profile_read(self, target: InstanceProfileTarget, read: GrantLease, deadline: float) -> None:
        """
        为已提交视图复核原始管理准入及物理实例，不为旧结果重新授权。

        :param target: 提交时确定的物理目标
        :param read: 当时的原始管理准入
        :param deadline: 本次结果核验截止
        :raises ProtocolError: 原始管理准入或实际对象已失效
        """
        self._refresh()
        self._session.check(read)
        reader = InstanceReadBoundary(target.root_path, deadline)
        if (
            reader.identity != target.root_identity
            or reader.directory(Path("versions") / target.version_id) != target.instance_identity
        ):
            raise InstanceReadError("实例物理身份已变化")

    def _check_scan_page(self, root: PluginResource, page: dict[str, JsonValue], deadline: float) -> None:
        """
        每页交付前复核派生实例的实际身份，分页快照不能延续同名重建前的摘要。

        仅检查当前页，复用一个根读取边界；最终授权检查仍由调用方统一执行。
        """
        reader = InstanceReadBoundary(Path(root.root_path), deadline)
        if reader.identity != root.identity:
            raise InstanceReadError("游戏目录身份发生变化")
        items = page["items"]
        assert isinstance(items, list)
        for item in items:
            assert isinstance(item, dict) and isinstance(item["instance_ref"], str)
            reference = self._resolve(item["instance_ref"], is_root=False)
            resource = reference.resource
            assert resource.version_id is not None
            if (
                resource.root_path != root.root_path
                or resource.root_identity != reader.identity
                or reader.directory(Path("versions") / resource.version_id) != resource.identity
            ):
                raise InstanceReadError("分页实例身份发生变化，请重新扫描")
        reader.check()

    def _list(self, lease: GrantLease) -> tuple[dict[str, JsonValue], ...]:
        items: list[dict[str, JsonValue]] = []
        for ref in lease.resource_refs:
            resource = self._catalog.resolve(ref)
            runtime_ref = self._reference(resource, ref)
            if resource.version_id is None:
                items.append({"kind": "instance_root", "root_ref": runtime_ref, "label": resource.label})
            else:
                items.append(
                    {
                        "kind": "instance",
                        "instance_ref": runtime_ref,
                        "version_id": resource.version_id,
                        "label": resource.label,
                    }
                )
        return tuple(items)

    def _summary(
        self, reference: _Reference, instance_ref: str, deadline: float, reader: InstanceReadBoundary | None = None
    ) -> dict[str, JsonValue]:
        resource = reference.resource
        assert resource.version_id is not None
        return self._queries.query_instance_summary(
            Path(resource.root_path), resource.version_id, deadline, reader=reader
        ).to_protocol(instance_ref)

    def _scan(self, reference: _Reference, deadline: float) -> tuple[dict[str, JsonValue], ...]:
        resource = reference.resource
        root_path = Path(resource.root_path)
        reader = InstanceReadBoundary(root_path, deadline)
        items = []
        for name in self._queries.query_instance_names(root_path, deadline):
            identity = reader.directory(Path("versions") / name)
            child = PluginResource(
                resource_id=resource.resource_id,
                root_path=resource.root_path,
                root_identity=resource.identity,
                version_id=name,
                identity=identity,
                is_active=True,
            )
            runtime_ref = self._reference(child, reference.grant_ref)
            items.append(self._summary(_Reference(child, reference.grant_ref), runtime_ref, deadline, reader))
        return tuple(items)

    def clear(self) -> None:
        """
        失效本运行引用与游标，不操作游戏资源。
        """
        with self._lock:
            self._references.clear()
            self._pages.clear()
