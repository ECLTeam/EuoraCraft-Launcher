# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：隔离单次 Worker 的 Java 引用、精确授权、分页及真实查询资源。
#
# 公开接口：
#   - class RuntimeJava — 仅向当前运行交付已授权 Java 摘要与官方包目录。
# ============================================================
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, RLock
from time import monotonic, time
from typing import TYPE_CHECKING
from uuid import uuid4

from ECL.java.models import JavaError, JavaInstallPlan, RuntimeRecord
from ECL.java.queries import JavaQueries
from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.grant_models import GrantLease
from ECL.plugins.grant_store import PluginGrantSession
from ECL.plugins.java_resources import PluginJavaResourceCatalog
from ECL.plugins.runtime.models import (
    JavaCatalogRequest,
    JavaInstallPlanRequest,
    JavaInstallRequest,
    JavaInventoryRequest,
    JavaValidateRequest,
    ResourceListRequest,
)
from ECL.plugins.runtime.resources import ResourcePages
from ECL.utils.operation_logging import private_transport_scope

if TYPE_CHECKING:
    from ECL.java.manager import JavaManager
    from ECL.plugins.runtime.operation_adapters import RuntimeOperations


@dataclass(frozen=True, slots=True)
class _Packages:
    recommended: int
    expires_at: float
    items: tuple[dict[str, JsonValue], ...]


class RuntimeJava:
    """
    当前运行独占引用与查询额度，原租约失效后不恢复旧结果。
    """

    def __init__(
        self,
        session: PluginGrantSession,
        queries: JavaQueries,
        catalog: PluginJavaResourceCatalog,
        refresh: Callable[[], int],
        manager: JavaManager | None = None,
    ) -> None:
        """
        注入唯一领域服务与宿主资源表，不创建后台执行器。

        :param session: 当前运行的授权会话
        :param queries: 应用现有 Java 查询
        :param catalog: 仅保存授权身份的宿主目录
        :param refresh: 在授权锁外刷新当前有效集合
        :param manager: 应用唯一 Java 管理器；未提供时拒绝安装
        """
        self._session = session
        self._queries = queries
        self._catalog = catalog
        self._refresh = refresh
        self._manager = manager
        self._plans: dict[str, tuple[JavaInstallPlan, GrantLease]] = {}
        self._installed: dict[str, tuple[str, GrantLease]] = {}
        self._lock = RLock()
        self._probe_lock = Lock()
        self._catalog_lock = Lock()
        self._closed = Event()
        self._epoch = -1
        self._refs: dict[str, str] = {}
        self._package_refs: dict[str, tuple[str, float]] = {}
        self._packages: dict[str, _Packages] = {}
        self._pages = ResourcePages()

    def _check(self, leases: tuple[GrantLease, ...], deadline: float) -> None:
        self._session.check_all(leases)
        if self._closed.is_set():
            raise ProtocolError("Java 查询所属运行已经结束", "PLUGIN_SESSION_EXPIRED")
        if monotonic() >= deadline:
            raise ProtocolError("Java 查询已超过截止", "PLUGIN_DEADLINE_EXCEEDED")

    def _reset_references(self, epoch: int) -> None:
        """
        授权代次变化后退休全部 Java 引用，重新批准不会复活原计划或安装结果。
        """
        with self._lock:
            if epoch < self._epoch:
                raise ProtocolError("Java 引用所属授权代次已经失效", "PLUGIN_GRANT_EXPIRED")
            if self._epoch != epoch:
                self._refs.clear()
                self._package_refs.clear()
                self._packages.clear()
                self._plans.clear()
                self._installed.clear()
                self._pages.clear()
                self._epoch = epoch

    def _prepare(self, capability: str) -> tuple[GrantLease, str]:
        revision = self._refresh() if capability != "java.catalog.read" else 0
        view = self._session.view()
        self._reset_references(view.grant_epoch)
        grant = next((item for item in view.items if item.capability == capability), None)
        lease = self._session.authorize(capability, grant.resource_refs if grant else ())
        return lease, f"{lease.grant_epoch}:{revision}"

    def _reference(self, resource_id: str, epoch: int) -> str:
        self._reset_references(epoch)
        with self._lock:
            reference = next((key for key, value in self._refs.items() if value == resource_id), None)
            if reference is None:
                if len(self._refs) >= 128:
                    raise ProtocolError("Java 引用数量超过上限", "PLUGIN_RATE_LIMITED")
                reference = uuid4().hex
                self._refs[reference] = resource_id
            return reference

    def permission_refs(self, capability: str) -> list[str]:
        """
        将原始持久选择转换为本 run 引用，不扩大读或校验范围。

        :param capability: java.read 或 java.use
        :return: 当前获准资源的运行引用
        """
        lease, _ = self._prepare(capability)
        result = [self._reference(key, lease.grant_epoch) for key in lease.resource_refs]
        self._session.check(lease)
        return result

    @staticmethod
    def _view(record: RuntimeRecord, reference: str) -> dict[str, JsonValue]:
        for value, limit in ((record.vendor, 120), (record.full_version, 120), (record.architecture, 32)):
            if len(value) > limit:
                raise ProtocolError("Java 摘要字段超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
        return {
            "runtime_ref": reference,
            "vendor": record.vendor,
            "major_version": record.major_version,
            "full_version": record.full_version,
            "runtime_kind": record.runtime_kind,
            "architecture": record.architecture,
            "origin": record.origin,
            "is_enabled": record.is_enabled,
            "validation_status": record.validation_status,
        }

    def handle(self, method: str, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        严格解析后按单一方法执行，实际调用结束才释放 run 额度。

        :param method: 已接线的 Java 方法或资源发现
        :param params: 不可信请求参数
        :param deadline: 包含等待及真实执行的原始截止
        :return: 不包含路径与宿主内部身份的白名单结果
        :raises ProtocolError: 授权、字段、资源或执行失败时抛出
        """
        if method not in {"java.inventory", "java.validate", "java.catalog", "java.install_plan", "resources.list"}:
            raise ProtocolError("此 Java 方法尚未开放", "PLUGIN_API_METHOD_NOT_FOUND")
        try:
            if method == "java.install_plan":
                return self._install_plan(params, deadline)
            if method == "java.validate":
                return self._validate(JavaValidateRequest.model_validate(params), deadline)
            if method == "java.catalog":
                return self._packages_page(JavaCatalogRequest.model_validate(params), deadline)
            query = (
                ResourceListRequest.model_validate(params)
                if method == "resources.list"
                else JavaInventoryRequest.model_validate(params)
            )
            if isinstance(query, ResourceListRequest) and query.capability != "java.read":
                raise ProtocolError("Java 资源发现能力无效", "PLUGIN_SCHEMA_INVALID")
            lease, generation = self._prepare("java.read")
            self._check((lease,), deadline)

            def project(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
                resource_id = str(item["resource_id"])
                record = self._catalog.resolve(resource_id)
                reference = self._reference(resource_id, lease.grant_epoch)
                if method == "resources.list":
                    return {
                        "kind": "java_runtime",
                        "runtime_ref": reference,
                        "label": f"{record.vendor} Java {record.full_version} {record.architecture}"[:120],
                    }
                return self._view(record, reference)

            result = self._pages.page(
                f"{method}:{query.limit}",
                generation,
                query.cursor,
                query.limit,
                lambda: tuple({"resource_id": key} for key in lease.resource_refs),
                project=project,
            )
            self._check((lease,), deadline)
            return result
        except JavaError as exc:
            code = exc.error_code
            if not code.startswith("PLUGIN_"):
                code = "PLUGIN_JAVA_CATALOG_FAILED" if method == "java.catalog" else "PLUGIN_JAVA_QUERY_FAILED"
            raise ProtocolError("Java 查询或校验未完成", code) from exc

    def _validate(self, request: JavaValidateRequest, deadline: float) -> dict[str, JsonValue]:
        if not self._probe_lock.acquire(blocking=False):
            raise ProtocolError("本运行已有 Java 校验", "PLUGIN_RATE_LIMITED")
        try:
            record, check = self.selection(request.runtime_ref, deadline)
            result = self._queries.validate(record, deadline, check)
            if request.required_major is not None and result.major_version < request.required_major:
                raise ProtocolError("所选 Java 不满足最低主版本", "PLUGIN_JAVA_VERSION_MISMATCH")
            check()
            return self._view(result, request.runtime_ref)
        finally:
            self._probe_lock.release()

    def _install_plan(self, params: dict[str, JsonValue], deadline: float) -> dict[str, JsonValue]:
        """
        从本运行官方目录引用生成短期计划，不接受下载地址或目标目录。
        """
        request = JavaInstallPlanRequest.model_validate(params)
        lease = self._session.authorize("java.install")
        catalog, _ = self._prepare("java.catalog.read")
        self._check((lease, catalog), deadline)
        with self._lock:
            package_id = next(
                (
                    key
                    for key, (ref, expiry) in self._package_refs.items()
                    if ref == request.package_ref and expiry > monotonic()
                ),
                None,
            )
        if self._manager is None:
            raise ProtocolError("当前宿主未提供 Java 安装", "PLUGIN_API_METHOD_NOT_FOUND")
        if package_id is None:
            raise ProtocolError("Java 包引用不存在或已过期", "PLUGIN_JAVA_PLAN_EXPIRED")
        plan = self._manager.install_plan(package_id)
        self._check((lease, catalog), deadline)
        with self._lock:
            self._plans = {ref: value for ref, value in self._plans.items() if value[0].expires_at > time()}
            if len(self._plans) >= 32:
                raise ProtocolError("Java 安装计划数量已达上限", "PLUGIN_RATE_LIMITED")
            reference = uuid4().hex
            self._plans[reference] = (plan, lease)
        return {
            "plan_ref": reference,
            "expires_at": plan.expires_at,
            "estimated_free_bytes": plan.estimated_free_bytes,
            "package_ref": request.package_ref,
        }

    def _installed_view(self, value: object, lease: GrantLease) -> JsonValue:
        """
        只把自身已完成安装登记为可选用引用，原始安装准入仍须有效。
        """
        if not isinstance(value, RuntimeRecord):
            return None
        self._session.check(lease)
        self._catalog.refresh(discover=True)
        resource = next(
            (item for item in self._catalog.resources() if item.is_active and item.runtime_id == value.runtime_id), None
        )
        if resource is None:
            raise ProtocolError("已安装 Java 的对象身份无法确认", "PLUGIN_RESOURCE_UNAVAILABLE")
        record = self._catalog.resolve(resource.resource_id)
        result: dict[str, JsonValue] = {}

        def register() -> None:
            reference = self._reference(resource.resource_id, lease.grant_epoch)
            with self._lock:
                self._installed[reference] = (resource.resource_id, lease)
            result.update(self._view(record, reference))

        self._session.accept(lease, register)
        return result

    def install(
        self, params: dict[str, JsonValue], operations: RuntimeOperations, deadline: float
    ) -> dict[str, JsonValue]:
        """
        在本运行任务中安装并允许选用该官方 Java，不借用其他来源回执。

        :param params: 当前运行持有的计划引用
        :param operations: 当前 Broker 的任务门面
        :param deadline: 提交截止
        :return: 本运行随机任务引用
        :raises ProtocolError: 计划、准入、来源或提交失败时抛出
        """
        from ECL.services.operations import OperationOrigin

        request = JavaInstallRequest.model_validate(params)
        self._session.authorize("java.install")
        with self._lock:
            prepared = self._plans.get(request.plan_ref)
        if prepared is None or prepared[0].expires_at <= time():
            raise ProtocolError("Java 安装计划已过期", "PLUGIN_JAVA_PLAN_EXPIRED")
        plan, lease = prepared
        self._check((lease,), deadline)
        manager = self._manager
        if manager is None:
            raise ProtocolError("当前宿主未提供 Java 安装", "PLUGIN_API_METHOD_NOT_FOUND")
        origin = OperationOrigin(lease.installation_id, lease.content_digest, lease.run_id)
        try:
            token = manager.reserve_plugin_install(plan, origin)
        except JavaError as error:
            raise ProtocolError("Java 安装目标当前不可用", error.error_code) from error
        try:
            reference = operations.submit(
                "plugin_java_install",
                lambda context: manager.install_for_plugin(plan, token, origin, context),
                lease,
                result_lease=lease,
                project_result=lambda value: self._installed_view(value, lease),
                cleanup=lambda: manager.release_plugin_install(plan, token, origin),
                deadline=monotonic() + 900,
                submission_deadline=deadline,
            )
        except BaseException:
            manager.release_plugin_install(plan, token, origin)
            raise
        return {"operation_ref": reference, "state": "queued"}

    def selection(self, reference: str, deadline: float) -> tuple[RuntimeRecord, Callable[[], None]]:
        """
        解析本运行明确获准或自行安装的 Java，返回原准入复核函数。

        :param reference: 本运行持有的 Java 引用
        :param deadline: 原始调用截止
        :return: 已核实登记记录及供实际启动提交使用的复核
        :raises ProtocolError: 跨运行、失权或对象变化时抛出
        """
        with self._lock:
            installed = self._installed.get(reference)
        if installed is not None:
            resource_id, lease = installed
            leases = (lease,)
        else:
            read, _ = self._prepare("java.read")
            with self._lock:
                resource_id = self._refs.get(reference)
            if resource_id is None or resource_id not in read.resource_refs:
                raise ProtocolError("Java 引用不属于当前获准范围", "PLUGIN_RESOURCE_DENIED")
            leases = (
                self._session.authorize("java.read", (resource_id,)),
                self._session.authorize("java.use", (resource_id,)),
            )

        def check() -> None:
            self._check(leases, deadline)
            if not self._catalog.resolve(resource_id).is_enabled:
                raise ProtocolError("所选 Java 已停用", "PLUGIN_JAVA_DISABLED")

        check()
        return self._catalog.resolve(resource_id), check

    def _packages_page(self, request: JavaCatalogRequest, deadline: float) -> dict[str, JsonValue]:
        lease, generation = self._prepare("java.catalog.read")
        signature = json.dumps([request.major_version, request.runtime_kind, request.limit])
        if not self._catalog_lock.acquire(blocking=False):
            raise ProtocolError("本运行已有 Java 目录查询", "PLUGIN_RATE_LIMITED")
        try:

            def check() -> None:
                self._check((lease,), deadline)

            check()
            now = monotonic()
            with self._lock:
                self._packages = {key: value for key, value in self._packages.items() if value.expires_at > now}
                self._package_refs = {key: value for key, value in self._package_refs.items() if value[1] > now}
                snapshot = self._packages.get(signature)
            if request.cursor is None:
                with private_transport_scope():
                    recommended, packages, expiry = self._queries.packages(
                        request.major_version, request.runtime_kind, deadline, check
                    )
                expiry = min(expiry, monotonic() + 120)
                items: list[dict[str, JsonValue]] = []
                with self._lock:
                    for package in packages:
                        if package.package_id not in self._package_refs:
                            if len(self._package_refs) >= 256:
                                raise ProtocolError("Java 包引用数量超过上限", "PLUGIN_RATE_LIMITED")
                            self._package_refs[package.package_id] = (uuid4().hex, expiry)
                        reference, ref_expiry = self._package_refs[package.package_id]
                        expiry = min(expiry, ref_expiry)
                        if len(package.release_name) > 120 or len(package.architecture) > 32:
                            raise ProtocolError("Java 包字段超过上限", "PLUGIN_MESSAGE_TOO_LARGE")
                        items.append(
                            {
                                "package_ref": reference,
                                "vendor": "Temurin",
                                "major_version": package.major_version,
                                "full_version": package.release_name,
                                "runtime_kind": package.runtime_kind,
                                "platform": package.platform,
                                "architecture": package.architecture,
                                "download_bytes": package.download_bytes,
                                "checksum": package.checksum,
                            }
                        )
                    if signature not in self._packages and len(self._packages) >= 16:
                        raise ProtocolError("Java 目录快照数量超过上限", "PLUGIN_RATE_LIMITED")
                    snapshot = _Packages(recommended, expiry, tuple(items))
                    self._packages[signature] = snapshot
            if snapshot is None or snapshot.expires_at <= monotonic():
                raise ProtocolError("Java 目录游标已过期", "PLUGIN_CURSOR_INVALID")
            result = self._pages.page(
                "java.catalog:" + signature,
                generation + str(snapshot.expires_at),
                request.cursor,
                request.limit,
                lambda: snapshot.items,
            )
            check()
            return {"recommended_major": snapshot.recommended, **result}
        finally:
            self._catalog_lock.release()

    def close(self) -> None:
        """
        拒绝新工作；正在执行的调用自行确认进程及网络实际结束。
        """
        self._closed.set()
        self._pages.clear()
