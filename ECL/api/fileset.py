# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：文件集 IPC 域处理器：列表、导入、改名、删除、定位与缩略图。
#
# 公开接口：
#   - class FileSetHandlers — 处理文件集相关的前端命令。
#       - fileset_list(body) — 返回全部条目。
#       - fileset_import(body) — 批量导入本地媒体文件。
#       - fileset_rename(body) — 修改显示名。
#       - fileset_delete(body) — 删除条目，被背景引用的条目拒绝删除。
#       - fileset_resolve(body) — 返回文件路径与可直接加载的协议地址。
#       - fileset_thumbnail(body) — 返回图片缩略图地址。
# ============================================================

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anyio import to_thread

from ECL.api.bridge import _FrontendState, _ipc_handler, _validate_body
from ECL.api.models import (
    FileSetDeleteRequest,
    FileSetImportRequest,
    FileSetItemRequest,
    FileSetRenameRequest,
    RequestModel,
)
from ECL.fileset.store import FileSetError

if TYPE_CHECKING:
    from ECL.api.image_protocol import ImageProtocolService
    from ECL.api.media_protocol import MediaProtocolService


class FileSetHandlers(_FrontendState):
    """
    处理文件集查询、导入、改名与删除请求。

    协议服务由协作式初始化中的 ``FileHandlers`` 创建，这里只声明依赖，
    不重复实例化，避免同一路径出现两套令牌表。
    """

    image_protocol: ImageProtocolService
    media_protocol: MediaProtocolService

    @_ipc_handler("FILESET_LIST_FAILED")
    async def fileset_list(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回文件集全部条目，按最近更新时间倒序。

        :param body: 空请求体
        :return: 条目字典列表
        """
        _request, invalid = _validate_body(RequestModel, body)
        if invalid is not None:
            return invalid
        items = await to_thread.run_sync(self.fileset.list_items)
        return {"success": True, "data": [item.to_dict() for item in items]}

    @_ipc_handler("FILESET_IMPORT_FAILED")
    async def fileset_import(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        批量导入本地图片与视频，单个文件失败不影响其余文件。

        :param body: 待导入的本地文件路径列表
        :return: 含 imported、duplicated 与 failed 三段的结果
        """
        request, invalid = _validate_body(FileSetImportRequest, body)
        if invalid is not None:
            return invalid
        result = await to_thread.run_sync(self.fileset.import_files, request.paths)
        return {"success": True, "data": result}

    @_ipc_handler("FILESET_RENAME_FAILED")
    async def fileset_rename(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        修改条目显示名，不触碰磁盘文件。

        :param body: 条目标识与新名称
        :return: 更新后的条目
        """
        request, invalid = _validate_body(FileSetRenameRequest, body)
        if invalid is not None:
            return invalid
        item = await to_thread.run_sync(self.fileset.rename_item, request.item_id, request.name)
        return {"success": True, "data": item.to_dict()}

    @_ipc_handler("FILESET_DELETE_FAILED")
    async def fileset_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        删除条目并清理文件；正被背景配置引用的条目返回在 inUse 中而不删除。

        :param body: 待删除的条目标识列表
        :return: 实际删除的标识与被拒的标识
        """
        request, invalid = _validate_body(FileSetDeleteRequest, body)
        if invalid is not None:
            return invalid
        in_use = await to_thread.run_sync(self._items_in_use, request.item_ids)
        deletable = [item_id for item_id in request.item_ids if item_id not in in_use]
        deleted = await to_thread.run_sync(self.fileset.delete_items, deletable)
        return {"success": True, "data": {"deleted": deleted, "inUse": sorted(in_use)}}

    @_ipc_handler("FILESET_RESOLVE_FAILED")
    async def fileset_resolve(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回条目的文件路径与可直接加载的协议地址。

        图片走 ``ecl-img``，视频走 ``ecl-media``；两者都不接受客户端传入路径。

        :param body: 条目标识
        :return: 路径、协议地址与媒体类型
        """
        request, invalid = _validate_body(FileSetItemRequest, body)
        if invalid is not None:
            return invalid
        path, kind = await to_thread.run_sync(self.fileset.resolve, request.item_id)
        if kind == "image":
            url = await to_thread.run_sync(self.image_protocol.register_local, path)
        else:
            url = await to_thread.run_sync(self.media_protocol.register_local, str(path))
        return {"success": True, "data": {"path": str(path), "url": url, "kind": kind}}

    @_ipc_handler("FILESET_THUMBNAIL_FAILED")
    async def fileset_thumbnail(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回图片条目的缩略图地址；视频条目由前端自行取首帧。

        :param body: 条目标识
        :return: 缩略图路径与协议地址；视频返回空值
        """
        request, invalid = _validate_body(FileSetItemRequest, body)
        if invalid is not None:
            return invalid
        thumbnail = await to_thread.run_sync(self.fileset.thumbnail_path, request.item_id)
        if thumbnail is None:
            return {"success": True, "data": {"path": None, "url": None}}
        url = await to_thread.run_sync(self.image_protocol.register_local, thumbnail)
        return {"success": True, "data": {"path": str(thumbnail), "url": url}}

    def _items_in_use(self, item_ids: list[str]) -> set[str]:
        """
        找出正被背景配置引用的条目。

        实例图标与封面在写入时已复制到实例目录，不构成引用，因此只检查背景。

        :param item_ids: 待检查的条目标识
        :return: 被引用的条目标识集合
        """
        referenced = self._background_paths()
        in_use: set[str] = set()
        for item_id in item_ids:
            try:
                path, _kind = self.fileset.resolve(item_id)
            except FileSetError:
                continue
            if self._path_key(str(path)) in referenced:
                in_use.add(item_id)
        return in_use

    def _background_paths(self) -> set[str]:
        """
        收集当前背景配置引用的本地路径。

        :return: 规范化后的路径集合
        """
        ui_config = self.config.get_config("ui")
        background = ui_config.get("background") if isinstance(ui_config, dict) else None
        if not isinstance(background, dict):
            return set()
        candidates: list[str] = []
        image = background.get("image")
        if isinstance(image, dict):
            candidates.append(str(image.get("path") or ""))
        video = background.get("video")
        if isinstance(video, dict):
            candidates.append(str(video.get("path") or ""))
            candidates.append(str(video.get("poster_path") or ""))
        # 旧版扁平字段仍可能保存路径，一并纳入引用检查。
        candidates.append(str(background.get("path") or ""))
        candidates.append(str(background.get("poster_path") or ""))
        return {self._path_key(value) for value in candidates if value.strip()}

    @staticmethod
    def _path_key(raw: str) -> str:
        # Windows 路径大小写不敏感，统一 casefold 后再比较。
        resolved = str(Path(raw).expanduser().resolve(strict=False))
        return resolved.casefold() if sys.platform == "win32" else resolved


__all__ = ["FileSetHandlers"]
