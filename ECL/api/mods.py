# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：模组领域 IPC 处理器：模组列表、在线搜索与详情（Modrinth/CurseForge/mcmod）。
#
# 公开接口：
#   - class ModHandlers — 提供本地模组文件管理 IPC；在线目录尚未纳入当前 Game API。
#       - get_mods(body) -> ApiResponse — 列出指定 Minecraft 根目录中的本地模组。
#       - toggle_mod(body) -> ApiResponse — 切换指定本地模组的启用状态。
#       - add_mod(body) -> ApiResponse — 将用户选择的 Jar 文件复制到目标 ``mods`` 目录。
#       - remove_mod(body) -> ApiResponse — 删除目标 ``mods`` 目录中的一个文件。
#       - open_mods_folder(body) -> ApiResponse — 创建并使用系统文件管理器打开目标 ``mods`` 目录。
#       - search_mods(body) -> ApiResponse — 搜索在线模组（Modrinth/CurseForge），映射为前端在线模组卡片结构。
#       - mod_source_config(body) -> ApiResponse — 返回在线资源来源的可用性配置，供前端禁用未配置的来源选项。
#       - get_mod_info(body) -> ApiResponse — 获取在线模组项目详情。
#       - get_mod_versions(body) -> ApiResponse — 获取在线模组兼容版本列表。
#       - download_mod(body) -> ApiResponse — 下载在线模组到目标实例的 ``mods`` 目录。
#       - download_mod_to_path(body) -> ApiResponse — 下载在线模组文件到用户指定的保存路径，不安装到任何实例。
# ============================================================

from __future__ import annotations

from typing import Any

from anyio import to_thread

from ECL.api.contracts import ApiResponse, success
from ECL.services.game.resource_search import SearchCriteria, SearchResult

from .bridge import _FrontendState, _ipc_handler, _open_folder, _validate_body
from .models import OnlineResourceSearchRequest


class ModHandlers(_FrontendState):
    """
    提供本地模组文件管理 IPC；在线目录尚未纳入当前 Game API。
    """

    @_ipc_handler("MOD_LIST_FAILED")
    async def get_mods(self, body: dict[str, Any]) -> ApiResponse:
        """
        列出指定 Minecraft 根目录中的本地模组。

        :param body: 包含 ``game_path`` 的请求数据
        :return: 本地模组列表
        """
        return success(await to_thread.run_sync(self.game.list_mods, body.get("game_path")))

    @_ipc_handler("MOD_TOGGLE_FAILED")
    async def toggle_mod(self, body: dict[str, Any]) -> ApiResponse:
        """
        切换指定本地模组的启用状态。

        :param body: 包含 ``game_path`` 和 ``filename`` 的请求数据
        :return: 切换后的启用状态
        """
        enabled = await to_thread.run_sync(self.game.toggle_mod, body.get("game_path"), body.get("filename"))
        return success({"enabled": enabled})

    @_ipc_handler("MOD_ADD_FAILED")
    async def add_mod(self, body: dict[str, Any]) -> ApiResponse:
        """
        将用户选择的 Jar 文件复制到目标 ``mods`` 目录。

        :param body: 包含 ``game_path`` 和 ``source_path`` 的请求数据
        :return: 安装后的模组文件名
        """
        filename = await to_thread.run_sync(self.game.add_mod, body.get("game_path"), body.get("source_path"))
        return success({"filename": filename})

    @_ipc_handler("MOD_REMOVE_FAILED")
    async def remove_mod(self, body: dict[str, Any]) -> ApiResponse:
        """
        删除目标 ``mods`` 目录中的一个文件。

        :param body: 包含 ``game_path`` 和 ``filename`` 的请求数据
        :return: 空的成功响应
        """
        await to_thread.run_sync(self.game.remove_mod, body.get("game_path"), body.get("filename"))
        return success()

    @_ipc_handler("MOD_FOLDER_FAILED")
    async def open_mods_folder(self, body: dict[str, Any]) -> ApiResponse:
        """
        创建并使用系统文件管理器打开目标 ``mods`` 目录。

        :param body: 包含 ``game_path`` 的请求数据
        :return: 模组目录路径
        """
        path = await to_thread.run_sync(self.game.mods_path, body.get("game_path"))
        await to_thread.run_sync(_open_folder, str(path))
        return success({"path": str(path)})

    @_ipc_handler("MOD_SEARCH_FAILED")
    async def search_mods(self, body: dict[str, Any]) -> ApiResponse:
        """
        搜索在线模组（Modrinth/CurseForge），映射为前端在线模组卡片结构。

        :param body: 在线搜索条件
        :return: 在线模组搜索结果
        """
        request, error = _validate_body(OnlineResourceSearchRequest, body)
        if error:
            return error
        if request.source == "all":

            def search_catalog() -> SearchResult:
                return self.game.search_resource_catalog(
                    SearchCriteria(
                        request.query, request.game_version, request.loader_type, request.resource_type, request.sort
                    ),
                    session_id=request.session_id,
                    page=request.page,
                    refresh=request.refresh,
                )

            catalog = await to_thread.run_sync(search_catalog)
            return success(catalog.model_dump(by_alias=True))
        result = await to_thread.run_sync(
            self.game.search_online_resources,
            request.query,
            request.game_version,
            request.loader_type,
            request.source,
            None,
            request.limit,
            request.resource_type,
            request.offset,
            request.sort,
        )
        source = str(result.get("source") or "modrinth")
        resource_type = str(result.get("resource_type") or "mod")
        items = self.game.map_search_hits(source, result.get("items") or [], resource_type)
        return success(
            {
                "items": items,
                "sources": {source: {"available": True, "error": "", "total": len(items)}},
                "total": result.get("total") or len(items),
                "query": body.get("query", ""),
            }
        )

    @_ipc_handler("MOD_SOURCE_CONFIG_FAILED")
    async def mod_source_config(self, body: dict[str, Any]) -> ApiResponse:
        """
        返回在线资源来源的可用性配置，供前端禁用未配置的来源选项。

        :param body: 空 IPC 请求数据
        :return: 各来源的可用状态
        """
        return success({"curseforge": {"available": self.game.curseforge_available()}})

    @_ipc_handler("MOD_INFO_FAILED")
    async def get_mod_info(self, body: dict[str, Any]) -> ApiResponse:
        """
        获取在线模组项目详情。

        :param body: 在线模组标识和来源
        :return: 模组项目详情
        """
        info = await to_thread.run_sync(
            self.game.fetch_project_info,
            body.get("source", "modrinth"),
            body.get("mod_id"),
            body.get("resource_type", "mod"),
        )
        return success(info)

    @_ipc_handler("MOD_VERSIONS_FAILED")
    async def get_mod_versions(self, body: dict[str, Any]) -> ApiResponse:
        """
        获取在线模组兼容版本列表。

        :param body: 在线模组标识、来源和筛选条件
        :return: 兼容版本列表
        """
        resource_type = str(body.get("resource_type") or "mod")
        # 非 mod 类型不按加载器过滤版本（资源包/光影/数据包无 loader 概念）
        loader = body.get("loader_type", "") if resource_type == "mod" else ""
        versions = await to_thread.run_sync(
            self.game.fetch_project_versions,
            body.get("source", "modrinth"),
            body.get("mod_id"),
            body.get("game_version", ""),
            loader,
        )
        return success(versions)

    @_ipc_handler("MOD_DOWNLOAD_FAILED")
    async def download_mod(self, body: dict[str, Any]) -> ApiResponse:
        """
        下载在线模组到目标实例的 ``mods`` 目录。

        :param body: 在线模组版本和安装目标
        :return: 安装结果
        """
        result = await to_thread.run_sync(
            self.game.install_online_resource,
            body.get("game_path"),
            body.get("instance_id"),
            body.get("resource_type", "mod"),
            body.get("source", "modrinth"),
            body.get("mod_id"),
            body.get("file_id"),
            False,
            body.get("task_id"),
            body.get("world_id"),
        )
        return success(
            {
                "installed": [result],
                "modsPath": str(self.game.mods_path(body.get("game_path"))),
            }
        )

    @_ipc_handler("MOD_DOWNLOAD_TO_PATH_FAILED")
    async def download_mod_to_path(self, body: dict[str, Any]) -> ApiResponse:
        """
        下载在线模组文件到用户指定的保存路径，不安装到任何实例。

        :param body: 在线模组版本和用户选择的保存路径
        :return: 保存结果（文件名）
        """
        result = await to_thread.run_sync(
            self.game.download_resource_to_path,
            body.get("source", "modrinth"),
            body.get("mod_id"),
            body.get("file_id"),
            body.get("save_path"),
            body.get("task_id"),
        )
        return success({"filename": result["filename"]})


__all__ = ["ModHandlers"]
