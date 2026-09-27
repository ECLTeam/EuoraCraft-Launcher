# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：游戏领域 IPC 处理器：版本目录、扫描安装、启动、崩溃分析等入口。
#
# 公开接口：
#   - class GameHandlers — 提供游戏目录、安装任务和运行实例的正式 IPC 边界。
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
#       - game_crash_analyze(body) -> ApiResponse — 在指定版本上下文中分析用户选择的 Minecraft 日志或 ZIP。
#       - game_crash_output(body) -> ApiResponse — 按需读取当前会话报告中的脱敏游戏输出。
#       - game_crash_export(body) -> ApiResponse — 将当前会话报告导出为经过脱敏的 ZIP。
# ============================================================

from __future__ import annotations

import shlex
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
from ECL.utils.config import default_config

from .bridge import _FrontendState, _ipc_handler, _validate_body


class GameHandlers(_FrontendState):
    """
    提供游戏目录、安装任务和运行实例的正式 IPC 边界。
    """

    def _download_source(self, requested_source: str | None) -> str:
        # 选择请求显式指定的下载源，缺失时使用启动器配置。
        if requested_source:
            return requested_source
        return str((self._get_effective_config().get("download") or {}).get("mirror_source") or "official")

    @staticmethod
    def _parse_game_args_tail(value: Any) -> list[str]:
        """
        将全局游戏参数文本解析为稳定的参数数组。

        使用 shell 兼容的引号规则拆分文本，但不执行命令；引号不匹配时转换为
        可由 IPC 边界统一处理的 ``ValueError``。
        """
        if not isinstance(value, str) or not value.strip():
            return []
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise ValueError("游戏参数尾部的引号格式无效") from exc

    @staticmethod
    def _global_jvm_args(value: Any) -> list[str]:
        """
        校验并复制全局 JVM 参数，避免不可信配置污染启动请求。

        仅接受字符串列表，并在返回前丢弃空白参数，避免配置文件中的异常值直接
        进入 Java 子进程参数。
        """
        if value in (None, ""):
            return []
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("全局 JVM 参数配置格式无效")
        return [item.strip() for item in value if item.strip()]

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
        return success(await to_thread.run_sync(self.game.read_version_settings, request.game_path, request.version_id))

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
        return success(
            await to_thread.run_sync(
                self.game.write_version_settings,
                request.game_path,
                request.version_id,
                request.data,
            )
        )

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
        values = request.model_dump()
        version_id = values.pop("version_id")
        game_path = values.pop("game_path")
        source = values.pop("source")
        java_path = values.pop("java_path")
        quick_target = values.pop("quick_target", None)
        game_config = self._get_effective_config().get("game") or {}
        if "width" not in request.model_fields_set:
            values["width"] = game_config.get("game_width", default_config["game"]["game_width"])
        if "height" not in request.model_fields_set:
            values["height"] = game_config.get("game_height", default_config["game"]["game_height"])
        if "fullscreen" not in request.model_fields_set:
            values["fullscreen"] = bool(game_config.get("fullscreen", default_config["game"]["fullscreen"]))
        # 启动高级选项：前端未显式提供（实例覆盖为空）时回退到全局游戏设置。
        if "launcher_visibility" not in request.model_fields_set:
            values["launcher_visibility"] = str(game_config.get("launcher_visibility") or "none")
        for advanced_key in ("wrapper_command", "post_exit_command", "env_vars", "window_title"):
            if advanced_key not in request.model_fields_set:
                values[advanced_key] = str(game_config.get(advanced_key) or "")
        values["jvm_args"] = [
            *self._global_jvm_args(game_config.get("jvm_args")),
            *values.get("jvm_args", []),
        ]
        values["game_args"] = [
            *self._parse_game_args_tail(game_config.get("game_args_tail")),
            *values.get("game_args", []),
        ]
        values["version_isolation"] = await to_thread.run_sync(
            self.game.resolve_version_isolation,
            game_path,
            version_id,
            values.get("version_isolation"),
        )
        if quick_target:
            values["game_args"] = [
                *values.get("game_args", []),
                *self.game.quick_launch_arguments(
                    game_path,
                    version_id,
                    quick_target,
                    values.get("version_isolation", False),
                ),
            ]
        result = await self.game.launch_instance(
            {"version_id": version_id},
            game_path=game_path,
            source=self._download_source(source.value if source else None),
            java_path=str(java_path) if java_path else None,
            pre_launch_command=game_config.get("pre_launch_command"),
            renderer=game_config.get("renderer"),
            prefer_high_performance_gpu=bool(game_config.get("prefer_high_performance_gpu")),
            use_java_exe=bool(game_config.get("use_java_exe")),
            disable_crash_analysis=bool(game_config.get("disable_crash_analysis")),
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
        在指定版本上下文中分析用户选择的 Minecraft 日志或 ZIP。

        :param body: 符合 ``CrashAnalyzeRequest`` 的文件和版本上下文
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
