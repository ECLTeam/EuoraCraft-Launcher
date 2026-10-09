# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验前端的 Java 管理请求，并在工作线程中调用 Java 服务。
#
# 公开接口：
#   - class JavaHandlers — 提供清单、登记、可用状态、计划、安装和受保护移除接口。
# ============================================================
from __future__ import annotations

from pathlib import Path
from typing import Any

from anyio import to_thread

from ECL.api.contracts import ApiResponse, success
from ECL.api.models import (
    JavaCatalogRequest,
    JavaEnabledRequest,
    JavaInventoryRequest,
    JavaPackageRequest,
    JavaPlanRequest,
    JavaRegisterRequest,
    JavaRuntimeRequest,
    JavaSelectionRequest,
)
from ECL.java import JavaError, JavaManager, JavaPolicy

from .bridge import _FrontendState, _ipc_handler, _validate_body


class JavaHandlers(_FrontendState):
    """
    将短请求转换为明确模型，安装任务由共享应用执行器拥有。
    """

    def _java(self) -> JavaManager:
        manager = self.context.java
        if manager is None:
            raise JavaError("Java 管理服务不可用", "JAVA_MANAGER_UNAVAILABLE")
        return manager

    @_ipc_handler("JAVA_CLEANUP_FAILED")
    async def game_java_cleanup(self, body: dict[str, Any]) -> ApiResponse:
        """
        提交遗留文件重试清理，工作线程保留无法确认拥有权的内容。

        :param body: 可选查询参数
        :return: 应用清理任务回执
        """
        _, invalid = _validate_body(JavaInventoryRequest, body)
        if invalid is not None:
            return invalid
        return success(self._java().cleanup())

    @_ipc_handler("JAVA_INVENTORY_FAILED")
    async def game_java_inventory(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询完整管理清单，失效或停用条目不被丢弃。

        :param body: force 指示是否重新扫描
        :return: 清单、平台与安装根目录
        """
        request, invalid = _validate_body(JavaInventoryRequest, body)
        if invalid is not None:
            return invalid
        inventory = await to_thread.run_sync(self._java().inventory, request.force)
        return success(
            inventory.model_dump(mode="json", by_alias=True, exclude={"runtimes": {"__all__": {"installed_files"}}})
        )

    @_ipc_handler("JAVA_REGISTER_FAILED")
    async def game_java_register(self, body: dict[str, Any]) -> ApiResponse:
        """
        验证并长期登记一个用户选择的现有 Java。

        :param body: 要探测的可执行路径
        :return: 已保存的运行时身份与信息
        """
        request, invalid = _validate_body(JavaRegisterRequest, body)
        if invalid is not None:
            return invalid
        record = await to_thread.run_sync(self._java().register, Path(request.path))
        return success(record.model_dump(mode="json", by_alias=True, exclude={"installed_files"}))

    @_ipc_handler("JAVA_STATE_FAILED")
    async def game_java_set_enabled(self, body: dict[str, Any]) -> ApiResponse:
        """
        启用或停用后续使用，不终止当前游戏。

        :param body: 稳定运行时身份和启用状态
        :return: 已确认落盘的记录
        """
        request, invalid = _validate_body(JavaEnabledRequest, body)
        if invalid is not None:
            return invalid
        record = await to_thread.run_sync(self._java().set_enabled, request.runtime_id, request.is_enabled)
        return success(record.model_dump(mode="json", by_alias=True, exclude={"installed_files"}))

    @_ipc_handler("JAVA_FORGET_FAILED")
    async def game_java_forget(self, body: dict[str, Any]) -> ApiResponse:
        """
        移除手动登记，保留外部安装文件。

        :param body: 手动登记的运行时身份
        :return: 是否移除成功
        """
        request, invalid = _validate_body(JavaRuntimeRequest, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self._java().forget, request.runtime_id)
        return success({"removed": True})

    @_ipc_handler("JAVA_SELECTION_FAILED")
    async def game_java_select(self, body: dict[str, Any]) -> ApiResponse:
        """
        为设置回填验证当前运行时，服务本身不修改全局或实例设置。

        :param body: 要使用的运行时和可选版本要求
        :return: 重新验证的运行时
        """
        request, invalid = _validate_body(JavaSelectionRequest, body)
        if invalid is not None:
            return invalid
        required_major = request.required_major
        if request.game_path is not None and request.version_id is not None:
            required_major = await to_thread.run_sync(
                self.game.java_requirement, str(request.game_path), request.version_id
            )
        record = await to_thread.run_sync(self._java().validate_selection, request.runtime_id, required_major)
        return success(record.model_dump(mode="json", by_alias=True, exclude={"installed_files"}))

    @_ipc_handler("JAVA_CATALOG_FAILED")
    async def game_java_catalog(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询当前主机实际发布包，空列表不冒充可下载。

        :param body: 主版本、JRE/JDK 与缓存刷新选项
        :return: 来源提供的版本目录及实际包
        """
        request, invalid = _validate_body(JavaCatalogRequest, body)
        if invalid is not None:
            return invalid
        catalog = self._java().catalog
        releases = await to_thread.run_sync(catalog.releases, request.force)
        major = request.major_version or catalog.recommended_major
        packages = await to_thread.run_sync(lambda: catalog.packages(major, request.runtime_kind, force=request.force))
        platform, architecture = JavaPolicy.host()
        return success(
            {
                "availableMajorVersions": list(releases),
                "recommendedMajorVersion": catalog.recommended_major,
                "majorVersion": major,
                "platform": platform,
                "architecture": architecture,
                "packages": [package.model_dump(mode="json", by_alias=True) for package in packages],
            }
        )

    @_ipc_handler("JAVA_PLAN_FAILED")
    async def game_java_install_plan(self, body: dict[str, Any]) -> ApiResponse:
        """
        生成不可由前端改写下载 URL 和目标目录的安装计划。

        :param body: 官方候选的稳定包身份
        :return: 可审阅的安装计划与有效期
        """
        request, invalid = _validate_body(JavaPackageRequest, body)
        if invalid is not None:
            return invalid
        plan = await to_thread.run_sync(self._java().install_plan, request.package_id)
        return success(plan.model_dump(mode="json", by_alias=True))

    @_ipc_handler("JAVA_INSTALL_FAILED")
    async def game_java_install(self, body: dict[str, Any]) -> ApiResponse:
        """
        提交或复用已确认安装计划，执行进度由应用任务报告。

        :param body: 后端安装计划身份
        :return: 应用任务回执
        """
        request, invalid = _validate_body(JavaPlanRequest, body)
        if invalid is not None:
            return invalid
        return success(self._java().install(request.plan_id))

    @_ipc_handler("JAVA_UPDATE_CHECK_FAILED")
    async def game_java_check_updates(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询同主版本托管更新，不修改运行时或任何绑定。

        :param body: 可选查询选项
        :return: 当前确有新包的托管运行时及候选
        """
        _, invalid = _validate_body(JavaInventoryRequest, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self._java().check_updates))

    @_ipc_handler("JAVA_REMOVE_FAILED")
    async def game_java_remove(self, body: dict[str, Any]) -> ApiResponse:
        """
        提交托管移除任务，执行方重新核对实际引用和进程占用。

        :param body: 启动器安装的运行时身份
        :return: 应用任务回执
        """
        request, invalid = _validate_body(JavaRuntimeRequest, body)
        if invalid is not None:
            return invalid
        return success(self._java().remove(request.runtime_id))
