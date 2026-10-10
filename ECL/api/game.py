# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：游戏领域 IPC 处理器：版本目录、扫描安装、启动、崩溃分析等入口。
#
# 公开接口：
#   - class GameHandlers — 处理游戏目录、版本安装和运行实例相关的前端请求。
#       - game_versions(body) -> ApiResponse — 查询 Minecraft 版本列表或分类目录。
#       - game_loader_versions(body) -> ApiResponse — 查询指定 Minecraft 版本可用的模组加载器版本。
#       - game_fabric_api_versions(body) -> ApiResponse — 查询指定 Minecraft 版本可用的 Fabric API 版本。
#       - game_scan(body) -> ApiResponse — 扫描一个或多个 Minecraft 根目录中的本地实例。
#       - game_install(body) -> ApiResponse — 创建受 Game Service 管理的版本安装任务。
#       - game_uninstall(body) -> ApiResponse — 从指定 Minecraft 根目录卸载一个实例。
#       - game_config_get(body) -> ApiResponse — 读取 Minecraft 根目录中的 ``ecl.json``。
#       - game_config_set(body) -> ApiResponse — 原子替换 Minecraft 根目录中的 ``ecl.json``。
#       - game_config_patch(body) -> ApiResponse — 合并更新 Minecraft 根目录中的 ``ecl.json``。
#       - game_instances(body) -> ApiResponse — 返回由启动器管理的运行中 Minecraft 实例。
#       - game_version_stats(body) -> ApiResponse — 返回指定 Minecraft 版本的持久化运行统计。
#       - game_version_settings_get(body) -> ApiResponse — 读取版本目录中的独立启动设置。
#       - game_version_settings_set(body) -> ApiResponse — 原子写入版本目录中的独立启动设置。
#       - game_instance_profile_get(body) -> ApiResponse — 读取单个实例的 ECL 原始覆盖资料。
#       - game_instance_profile_patch(body) -> ApiResponse — 合并保存实例资料覆盖字段。
#       - game_instance_profile_reset(body) -> ApiResponse — 删除指定实例覆盖字段，使其恢复自动解析。
#       - game_instance_icon_set(body) -> ApiResponse — 设置实例自动、内置、加载器或本地图片图标。
#       - game_instance_pin_order_set(body) -> ApiResponse — 保存全部置顶实例的拖拽顺序。
#       - game_instance_categories_get(body) -> ApiResponse — 返回内置与用户自定义实例分类。
#       - game_instance_categories_upsert(body) -> ApiResponse — 新建或更新用户自定义实例分类。
#       - game_instance_categories_delete(body) -> ApiResponse — 删除用户自定义实例分类。
#       - game_launch(body) -> ApiResponse — 校验启动参数、补全文件并创建 Minecraft 进程。
#       - game_launch_cancel(body) -> ApiResponse — 取消当前仍处于准备或文件补全阶段的启动任务。
#       - game_instance_stop(body) -> ApiResponse — 通知指定的运行中 Minecraft 实例退出，超时后才强制结束。
#       - game_crash_list(body) -> ApiResponse — 列出指定实例文件夹内可分析的候选日志文件。
#       - game_crash_analyze(body) -> ApiResponse — 分析用户为指定游戏版本选择的 Minecraft 日志或 ZIP。
#       - game_crash_output(body) -> ApiResponse — 按需读取当前会话报告中的脱敏游戏输出。
#       - game_crash_export(body) -> ApiResponse — 将当前会话报告导出为经过脱敏的 ZIP。
#       - game_version_settings_effective(body) -> ApiResponse — 解析当前实例的有效启动设置。
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import Any

from anyio import to_thread

from ECL.api.contracts import ApiResponse, failure, success
from ECL.api.models import (
    CrashAnalyzeRequest,
    CrashExportRequest,
    CrashReportRequest,
    GameCatalogRequest,
    GameConfigPatch,
    GameConfigUpdate,
    GameInstanceRequest,
    GamePathRequest,
    GameScanRequest,
    GameUninstallRequest,
    GameVersionRequest,
    GameVersionSettingsUpdate,
    InstallRequest,
    InstanceCategoryDeleteRequest,
    InstanceCategoryUpsertRequest,
    InstanceIconRequest,
    InstancePinOrderRequest,
    InstanceProfilePatchRequest,
    InstanceProfileResetRequest,
    LaunchRequest,
    LoaderCatalogRequest,
)
from ECL.services.game.launch_settings import InstanceLaunchOverrides, LaunchSettingsResolver

from .bridge import _FrontendState, _ipc_handler, _validate_body


class GameHandlers(_FrontendState):
    """
    处理游戏目录、版本安装和运行实例相关的前端请求。
    """

    async def _read_launch_settings(self, game_path: str | Path, version_id: str) -> dict[str, Any]:
        """
        优先读取实例文件，文件为空时保留旧全局实例覆盖的启动语义。

        此回退与旧前端的键规范一致，只读取而不迁移落盘；用户保存或重置时
        由既有设置接口写入实例文件并清理旧入口。
        """
        settings = await to_thread.run_sync(self.game.read_version_settings, game_path, version_id)
        if settings:
            return settings
        legacy = self._get_effective_config().get("version_settings")
        if not isinstance(legacy, dict):
            return {}
        key = await to_thread.run_sync(self._legacy_launch_settings_key, game_path, version_id)
        value = legacy.get(key) if key is not None else None
        return dict(value) if isinstance(value, dict) else {}

    def _legacy_launch_settings_key(self, game_path: str | Path, version_id: str) -> str | None:
        """
        仅在旧小写路径键唯一对应一个实际根目录时恢复或清理实例设置。

        旧键有歧义时保留原配置，不让一个实例读入或删除另一个实例的覆盖。
        在工作线程中读取目录，避免磁盘操作阻塞异步 IPC 请求。

        :param game_path: 请求指定的游戏根目录
        :param version_id: 磁盘实例目录名
        :return: 唯一对应的历史键；存在歧义时为 None
        """
        normalized_path = str(game_path).strip().replace("\\", "/").rstrip("/").lower()
        roots = {str(Path(game_path).expanduser().resolve(strict=False))}
        game_config = self.config.get_config("game")
        entries = game_config.get("minecraft_paths", []) if isinstance(game_config, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            configured_path = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(configured_path, str):
                continue
            if configured_path.strip().replace("\\", "/").rstrip("/").lower() == normalized_path:
                roots.add(str(Path(configured_path).expanduser().resolve(strict=False)))
        if len(roots) > 1:
            return None
        return f"{normalized_path}::{version_id.strip()}"

    def _download_source(self, requested_source: str | None) -> str:
        # 选择请求显式指定的下载源，缺失时使用启动器配置。
        if requested_source:
            return requested_source
        return str((self._get_effective_config().get("download") or {}).get("mirror_source") or "official")

    @_ipc_handler("VERSION_CATALOG_FAILED")
    async def game_versions(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询 Minecraft 版本列表或分类目录。

        :param body: 符合 ``GameCatalogRequest`` 的请求数据
        :return: 版本列表或按版本类型分类的目录
        """
        request, invalid = _validate_body(GameCatalogRequest, body)
        if invalid is not None:
            return invalid
        source = self._download_source(request.source.value if request.source else None)
        if request.classified:
            catalog = await to_thread.run_sync(self.game.minecraft_versions_classified, source)
            return success(catalog)
        versions = await to_thread.run_sync(self.game.minecraft_versions, request.filter_type, source)
        return success(versions)

    @_ipc_handler("LOADER_VERSIONS_FAILED")
    async def game_loader_versions(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询指定 Minecraft 版本可用的模组加载器版本。

        :param body: 符合 ``LoaderCatalogRequest`` 的请求数据
        :return: 加载器版本列表
        """
        request, invalid = _validate_body(LoaderCatalogRequest, body)
        if invalid is not None:
            return invalid
        source = self._download_source(request.source.value if request.source else None)
        versions = await to_thread.run_sync(
            self.game.loader_versions,
            request.loader.value,
            request.game_version,
            source,
        )
        return success(versions)

    @_ipc_handler("LOADER_VERSIONS_FAILED")
    async def game_fabric_api_versions(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询指定 Minecraft 版本可用的 Fabric API 版本。

        :param body: 符合 ``LoaderCatalogRequest`` 的请求数据
        :return: Fabric API 版本列表
        """
        request, invalid = _validate_body(LoaderCatalogRequest, body)
        if invalid is not None:
            return invalid
        versions = await to_thread.run_sync(self.game.fabric_api_versions, request.game_version)
        return success(versions)

    @_ipc_handler("VERSION_SCAN_FAILED")
    async def game_scan(self, body: dict[str, Any]) -> ApiResponse:
        """
        扫描一个或多个 Minecraft 根目录中的本地实例。

        :param body: 符合 ``GameScanRequest`` 的请求数据
        :return: 扫描到的实例列表
        """
        request, invalid = _validate_body(GameScanRequest, body)
        if invalid is not None:
            return invalid
        paths = request.paths
        if paths is None:
            game_config = self._get_effective_config().get("game") or {}
            configured_paths = game_config.get("minecraft_paths") or []
            paths = [item.get("path") if isinstance(item, dict) else item for item in configured_paths]
        else:
            game_config = self._get_effective_config().get("game") or {}
        scan_paths = [str(path) for path in paths if path]
        qomicex_path = game_config.get("qomicex_instances_path")
        compatibility_options = {
            "qomicex": {"instances_path": qomicex_path},
        }
        versions = await to_thread.run_sync(
            lambda: self.game.scan_versions(
                scan_paths,
                force=request.force,
                compatibility_options=compatibility_options,
            )
        )
        return success(versions)

    @_ipc_handler("VERSION_INSTALL_FAILED")
    async def game_install(self, body: dict[str, Any]) -> ApiResponse:
        """
        创建受 Game Service 管理的版本安装任务。

        :param body: 符合 ``InstallRequest`` 的请求数据
        :return: 安装任务标识与最终实例名称
        """
        request, invalid = _validate_body(InstallRequest, body)
        if invalid is not None:
            return invalid
        payload = request.model_dump(mode="json", exclude={"game_path", "java_path", "source"}, exclude_none=True)
        if request.loader_type is not None and request.loader_version is not None:
            payload[f"{request.loader_type.value}_version"] = request.loader_version
        result = self.game.install_version(
            payload,
            game_path=request.game_path,
            java_path=str(request.java_path) if request.java_path else None,
            source=self._download_source(request.source.value if request.source else None),
        )
        return success(result)

    @_ipc_handler("VERSION_UNINSTALL_FAILED")
    async def game_uninstall(self, body: dict[str, Any]) -> ApiResponse:
        """
        从指定 Minecraft 根目录卸载一个实例。

        :param body: 符合 ``GameUninstallRequest`` 的请求数据
        :return: 空的成功响应
        """
        request, invalid = _validate_body(GameUninstallRequest, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self.game.delete_instance, request.game_path, request.version_id)
        return success()

    @_ipc_handler("GAME_CONFIG_FAILED")
    async def game_config_get(self, body: dict[str, Any]) -> ApiResponse:
        """
        读取 Minecraft 根目录中的 ``ecl.json``。

        :param body: 符合 ``GamePathRequest`` 的请求数据
        :return: 完整游戏目录配置
        """
        request, invalid = _validate_body(GamePathRequest, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self.game.read_ecl_config, request.game_path))

    @_ipc_handler("GAME_CONFIG_FAILED")
    async def game_config_set(self, body: dict[str, Any]) -> ApiResponse:
        """
        原子替换 Minecraft 根目录中的 ``ecl.json``。

        :param body: 符合 ``GameConfigUpdate`` 的请求数据
        :return: 空的成功响应
        """
        request, invalid = _validate_body(GameConfigUpdate, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self.game.write_ecl_config, request.game_path, request.data)
        return success()

    @_ipc_handler("GAME_CONFIG_FAILED")
    async def game_config_patch(self, body: dict[str, Any]) -> ApiResponse:
        """
        合并更新 Minecraft 根目录中的 ``ecl.json``。

        :param body: 符合 ``GameConfigPatch`` 的请求数据
        :return: 更新后的完整游戏目录配置
        """
        request, invalid = _validate_body(GameConfigPatch, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self.game.patch_ecl_config, request.game_path, request.patch))

    async def game_instances(self, body: dict[str, Any]) -> ApiResponse:
        """
        返回由启动器管理的运行中 Minecraft 实例。

        :param body: 必须为空的请求对象
        :return: 运行中实例列表
        """
        if body:
            return failure("game_instances 不接受请求参数", "INVALID_REQUEST")
        return success(self.game.list_instances())

    @_ipc_handler("VERSION_STATS_FAILED")
    async def game_version_stats(self, body: dict[str, Any]) -> ApiResponse:
        """
        返回指定 Minecraft 版本的持久化运行统计。

        :param body: 符合 ``GameVersionRequest`` 的请求数据
        :return: 启动次数、上次运行秒数和总运行秒数
        """
        request, invalid = _validate_body(GameVersionRequest, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self.game.get_version_stats, request.game_path, request.version_id))

    @_ipc_handler("GAME_CONFIG_FAILED")
    async def game_version_settings_get(self, body: dict[str, Any]) -> ApiResponse:
        """
        读取版本目录中的独立启动设置。

        :param body: 符合 ``GameVersionRequest`` 的请求数据
        :return: 该版本的启动设置字典
        """
        request, invalid = _validate_body(GameVersionRequest, body)
        if invalid is not None:
            return invalid
        return success(await self._read_launch_settings(request.game_path, request.version_id))

    @_ipc_handler("GAME_CONFIG_FAILED")
    async def game_version_settings_effective(self, body: dict[str, Any]) -> ApiResponse:
        """
        查询与实际启动相同的实例有效设置，供界面展示继承结果。

        :param body: 实例目标
        :return: 解析后的启动设置
        """
        request, invalid = _validate_body(GameVersionRequest, body)
        if invalid is not None:
            return invalid
        settings = await self._read_launch_settings(request.game_path, request.version_id)
        effective = await to_thread.run_sync(
            LaunchSettingsResolver.resolve,
            self._get_effective_config().get("game") or {},
            InstanceLaunchOverrides.model_validate(settings),
        )
        values = effective.model_dump()
        values["version_isolation"] = await to_thread.run_sync(
            self.game.resolve_version_isolation, request.game_path, request.version_id, effective.version_isolation
        )
        return success(values)

    @_ipc_handler("GAME_CONFIG_FAILED")
    async def game_version_settings_set(self, body: dict[str, Any]) -> ApiResponse:
        """
        原子写入版本目录中的独立启动设置。

        :param body: 符合 ``GameVersionSettingsUpdate`` 的请求数据
        :return: 写入后的完整版本设置
        """
        request, invalid = _validate_body(GameVersionSettingsUpdate, body)
        if invalid is not None:
            return invalid
        saved = await to_thread.run_sync(
            self.game.write_version_settings,
            request.game_path,
            request.version_id,
            request.data,
        )
        legacy_key = await to_thread.run_sync(self._legacy_launch_settings_key, request.game_path, request.version_id)
        if legacy_key is not None:
            await to_thread.run_sync(self.config.remove_legacy_instance_settings, legacy_key)
        return success(saved)

    @_ipc_handler("INSTANCE_PROFILE_FAILED")
    async def game_instance_profile_get(self, body: dict[str, Any]) -> ApiResponse:
        """
        读取单个实例的 ECL 原始覆盖资料。

        :param body: 符合 ``GameVersionRequest`` 的实例目标
        :return: 未自动补齐字段的原始实例资料
        """
        request, invalid = _validate_body(GameVersionRequest, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self.game.get_instance_profile, request.game_path, request.version_id))

    @_ipc_handler("INSTANCE_PROFILE_FAILED")
    async def game_instance_profile_patch(self, body: dict[str, Any]) -> ApiResponse:
        """
        合并保存实例资料覆盖字段。

        :param body: 符合 ``InstanceProfilePatchRequest`` 的增量资料
        :return: 保存后的原始实例资料
        """
        request, invalid = _validate_body(InstanceProfilePatchRequest, body)
        if invalid is not None:
            return invalid
        patch = request.patch.model_dump(mode="json", exclude_unset=True, by_alias=True)
        return success(
            await to_thread.run_sync(
                self.game.patch_instance_profile,
                request.game_path,
                request.version_id,
                patch,
            )
        )

    @_ipc_handler("INSTANCE_PROFILE_FAILED")
    async def game_instance_profile_reset(self, body: dict[str, Any]) -> ApiResponse:
        """
        删除指定实例覆盖字段，使其恢复自动解析。

        :param body: 符合 ``InstanceProfileResetRequest`` 的字段列表
        :return: 保存后的原始实例资料
        """
        request, invalid = _validate_body(InstanceProfileResetRequest, body)
        if invalid is not None:
            return invalid
        return success(
            await to_thread.run_sync(
                self.game.reset_instance_profile,
                request.game_path,
                request.version_id,
                request.fields,
            )
        )

    @_ipc_handler("INSTANCE_ICON_FAILED")
    async def game_instance_icon_set(self, body: dict[str, Any]) -> ApiResponse:
        """
        设置实例自动、内置、加载器或本地图片图标。

        :param body: 符合 ``InstanceIconRequest`` 的图标选择
        :return: 保存后的原始实例资料
        """
        request, invalid = _validate_body(InstanceIconRequest, body)
        if invalid is not None:
            return invalid
        return success(
            await to_thread.run_sync(
                self.game.set_instance_icon,
                request.game_path,
                request.version_id,
                request.icon_type.value,
                request.value,
                request.source_path,
            )
        )

    @_ipc_handler("INSTANCE_PROFILE_FAILED")
    async def game_instance_pin_order_set(self, body: dict[str, Any]) -> ApiResponse:
        """
        保存全部置顶实例的拖拽顺序。

        :param body: 符合 ``InstancePinOrderRequest`` 的有序实例列表
        :return: 空的成功响应
        """
        request, invalid = _validate_body(InstancePinOrderRequest, body)
        if invalid is not None:
            return invalid
        entries = [entry.model_dump(mode="json") for entry in request.entries]
        await to_thread.run_sync(self.game.set_instance_pin_order, entries)
        return success()

    async def game_instance_categories_get(self, body: dict[str, Any]) -> ApiResponse:
        """
        返回内置与用户自定义实例分类。
        """
        if body:
            return failure("game_instance_categories_get 不接受请求参数", "INVALID_REQUEST")
        return success(await to_thread.run_sync(self.game.get_instance_categories))

    @_ipc_handler("INSTANCE_CATEGORY_FAILED")
    async def game_instance_categories_upsert(self, body: dict[str, Any]) -> ApiResponse:
        """
        新建或更新用户自定义实例分类。

        :param body: 符合 ``InstanceCategoryUpsertRequest`` 的分类数据
        :return: 保存后的分类
        """
        request, invalid = _validate_body(InstanceCategoryUpsertRequest, body)
        if invalid is not None:
            return invalid
        return success(
            await to_thread.run_sync(
                self.game.upsert_instance_category,
                request.category_id,
                request.name,
                request.color,
                request.order,
            )
        )

    @_ipc_handler("INSTANCE_CATEGORY_FAILED")
    async def game_instance_categories_delete(self, body: dict[str, Any]) -> ApiResponse:
        """
        删除用户自定义实例分类。

        :param body: 符合 ``InstanceCategoryDeleteRequest`` 的分类 ID
        :return: 空的成功响应
        """
        request, invalid = _validate_body(InstanceCategoryDeleteRequest, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self.game.delete_instance_category, request.category_id)
        return success()

    @_ipc_handler("GAME_LAUNCH_FAILED")
    async def game_launch(self, body: dict[str, Any]) -> ApiResponse:
        """
        校验启动参数、补全文件并创建 Minecraft 进程。

        :param body: 符合 ``LaunchRequest`` 的请求数据
        :return: 新游戏实例的标识、版本和目录
        """
        request, invalid = _validate_body(LaunchRequest, body)
        if invalid is not None:
            return invalid
        version_id = request.version_id
        game_path = request.game_path
        source = request.source
        quick_target = request.quick_target.model_dump() if request.quick_target else None
        settings = await self._read_launch_settings(game_path, version_id)
        effective = await to_thread.run_sync(
            LaunchSettingsResolver.resolve,
            self._get_effective_config().get("game") or {},
            InstanceLaunchOverrides.model_validate(settings),
            request.model_dump(exclude_unset=True),
        )
        values = effective.model_dump()
        java_path = values.pop("java_path")
        values["version_isolation"] = await to_thread.run_sync(
            self.game.resolve_version_isolation,
            game_path,
            version_id,
            values.get("version_isolation"),
        )
        if quick_target:
            quick_arguments = await to_thread.run_sync(
                self.game.quick_launch_arguments,
                game_path,
                version_id,
                quick_target,
                values.get("version_isolation", False),
            )
            values["game_args"] = [
                *values.get("game_args", []),
                *quick_arguments,
            ]
        result = await self.game.launch_instance(
            {"version_id": version_id},
            game_path=game_path,
            source=self._download_source(source.value if source else None),
            java_path=str(java_path) if java_path else None,
            **values,
        )
        return success(result)

    async def game_launch_cancel(self, body: dict[str, Any]) -> ApiResponse:
        """
        取消当前仍处于准备或文件补全阶段的启动任务。

        :param body: 必须为空的请求对象
        :return: 空的成功响应
        """
        if body:
            return failure("game_launch_cancel 不接受请求参数", "INVALID_REQUEST")
        if not self.game.cancel_launch():
            return failure("当前没有可取消的启动任务", "NO_ACTIVE_LAUNCH")
        return success()

    @_ipc_handler("INSTANCE_STOP_FAILED")
    async def game_instance_stop(self, body: dict[str, Any]) -> ApiResponse:
        """
        通知指定的运行中 Minecraft 实例退出，超时后才强制结束。

        :param body: 符合 ``GameInstanceRequest`` 的请求数据
        :return: 空的成功响应
        """
        request, invalid = _validate_body(GameInstanceRequest, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self.game.stop_instance, request.instance_id)
        return success()

    @_ipc_handler("CRASH_LIST_FAILED")
    async def game_crash_list(self, body: dict[str, Any]) -> ApiResponse:
        """
        列出指定实例文件夹内可分析的候选日志文件。

        :param body: 符合 ``GameVersionRequest`` 的游戏路径与版本
        :return: 候选日志描述列表（path/name/size/mtime）
        """
        request, invalid = _validate_body(GameVersionRequest, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self.game.list_crash_candidates, request.game_path, request.version_id))

    @_ipc_handler("CRASH_ANALYSIS_FAILED")
    async def game_crash_analyze(self, body: dict[str, Any]) -> ApiResponse:
        """
        分析用户为指定游戏版本选择的 Minecraft 日志或 ZIP。

        :param body: 包含所选文件和游戏版本的 ``CrashAnalyzeRequest`` 参数
        :return: 会话报告编号、原因、证据和可用输出信息
        """
        request, invalid = _validate_body(CrashAnalyzeRequest, body)
        if invalid is not None:
            return invalid
        result = await to_thread.run_sync(
            self.game.analyze_crash_file,
            request.file_path,
            request.game_path,
            request.version_id,
        )
        return success(result)

    @_ipc_handler("CRASH_OUTPUT_FAILED")
    async def game_crash_output(self, body: dict[str, Any]) -> ApiResponse:
        """
        按需读取当前会话报告中的脱敏游戏输出。

        :param body: 符合 ``CrashReportRequest`` 的报告编号
        :return: 输出文件名和最多五百行进程输出
        """
        request, invalid = _validate_body(CrashReportRequest, body)
        if invalid is not None:
            return invalid
        return success(await to_thread.run_sync(self.game.get_crash_output, request.report_id))

    @_ipc_handler("CRASH_EXPORT_FAILED")
    async def game_crash_export(self, body: dict[str, Any]) -> ApiResponse:
        """
        将当前会话报告导出为经过脱敏的 ZIP。

        :param body: 报告编号和可选的目标 ZIP 路径
        :return: 实际写入的导出路径
        """
        request, invalid = _validate_body(CrashExportRequest, body)
        if invalid is not None:
            return invalid
        result = await to_thread.run_sync(
            self.game.export_crash_report,
            request.report_id,
            request.output_path,
        )
        return success(result)


__all__ = ["GameHandlers"]
