# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：读取唯一 Java 登记快照，执行单目标受控校验及固定来源目录查询。
#
# 公开接口：
#   - class JavaQueries — 不触发系统扫描或修改业务登记的查询服务。
# ============================================================
from __future__ import annotations

import os
from collections.abc import Callable
from threading import BoundedSemaphore
from time import monotonic
from typing import TYPE_CHECKING

from ECL.game import JavaProbeError, JavaScanner

from .models import JavaError, JavaPackage, JavaPolicy, RuntimeRecord

if TYPE_CHECKING:
    from .manager import JavaManager


class JavaQueries:
    """
    复用应用已有 Java 管理服务，仅由明确授权的调用执行探测。
    """

    def __init__(self, manager: JavaManager) -> None:
        """
        绑定唯一服务及宿主级实际调用额度，不另建执行器。

        :param manager: 应用拥有的 Java 管理器
        """
        self._manager = manager
        self._probe_slots = BoundedSemaphore(2)
        self._catalog_slots = BoundedSemaphore(2)

    def records(self) -> tuple[RuntimeRecord, ...]:
        """
        只读已登记快照，不探测、扫描、初始化登记或更新引用。

        :return: 领域唯一登记中的不可变记录
        :raises JavaError: 登记损坏、超限或服务关闭时抛出
        """
        return self._manager.registered_snapshot()

    def validate(self, record: RuntimeRecord, deadline: float, check: Callable[[], None]) -> RuntimeRecord:
        """
        在锁外校验指定 Java，所有进程实际结束后才释放额度。

        :param record: 已通过授权和对象身份核对的登记记录
        :param deadline: 原调用共同绝对截止
        :param check: 原始授权及对象状态的复核，不允许换取新租约
        :return: 本次探测摘要，不写回登记或配置
        :raises JavaError: 停用、无效、超时或额度耗尽时抛出
        """
        if not self._probe_slots.acquire(blocking=False):
            raise JavaError("Java 校验正在处理其他请求", "PLUGIN_RATE_LIMITED")
        try:
            check()
            if not record.is_enabled:
                raise JavaError("所选 Java 已停用", "PLUGIN_JAVA_DISABLED")
            environment = dict(os.environ)
            for key in tuple(environment):
                if key.upper() in {"JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}:
                    environment.pop(key)
            try:
                runtime = JavaScanner.probe(
                    record.executable_path, deadline=deadline, environment=environment, check=check
                )
            except JavaProbeError as exc:
                code = "PLUGIN_DEADLINE_EXCEEDED" if exc.code == "deadline" else "PLUGIN_JAVA_INVALID"
                raise JavaError("Java 校验未完成", code) from exc
            if runtime is None:
                raise JavaError("所选 Java 无法运行", "PLUGIN_JAVA_INVALID")
            refreshed = JavaPolicy.record(runtime)
            check()
            return record.model_copy(
                update={
                    "vendor": refreshed.vendor,
                    "full_version": refreshed.full_version,
                    "major_version": refreshed.major_version,
                    "runtime_kind": refreshed.runtime_kind,
                    "architecture": refreshed.architecture,
                    "validation_status": "valid",
                }
            )
        finally:
            self._probe_slots.release()

    def packages(
        self, major: int | None, kind: str, deadline: float, check: Callable[[], None]
    ) -> tuple[int, tuple[JavaPackage, ...], float]:
        """
        查询固定来源的一个主版本，缓存与两次请求共用截止。

        :param major: 指定主版本；None 表示同次来源快照推荐版本
        :param kind: 已校验的 JRE 或 JDK
        :param deadline: 包含 releases 和 packages 的共同截止
        :param check: 逐次请求前后检查原始授权
        :return: 推荐主版本、包快照及其实际缓存到期
        :raises JavaError: 来源、预算或额度不满足时抛出
        """
        if not self._catalog_slots.acquire(blocking=False):
            raise JavaError("Java 目录正在处理其他请求", "PLUGIN_RATE_LIMITED")
        try:
            catalog = self._manager.catalog
            recommended, releases = catalog.release_snapshot(deadline, check)
            if recommended not in releases or type(recommended) is not int:
                raise JavaError("Java 推荐版本无效", "PLUGIN_JAVA_CATALOG_FAILED")
            packages, expiry = catalog.package_snapshot(major or recommended, kind, deadline, check)
            check()
            if monotonic() >= deadline:
                raise JavaError("Java 目录查询已超时", "PLUGIN_DEADLINE_EXCEEDED")
            return recommended, packages, expiry
        finally:
            self._catalog_slots.release()
