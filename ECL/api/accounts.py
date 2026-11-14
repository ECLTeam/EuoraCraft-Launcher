# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：账户领域 IPC 处理器：登录、账户列表、皮肤与衣橱等操作转发到账户服务。
#
# 公开接口：
#   - class AccountHandlers — 处理前端发来的账户、微软登录、皮肤和本地衣柜请求。
#       - accounts_list(body) -> dict[str, Any] — 获取账户列表。
#       - accounts_current(body) -> dict[str, Any] — 获取当前账户。
#       - accounts_auth_providers(body) -> dict[str, Any] — 获取插件注册的全部认证提供方定义。
#       - accounts_add_plugin(body) -> dict[str, Any] — 通过插件认证提供方新增账户。
#       - accounts_add_offline(body) -> dict[str, Any] — 添加离线账户。
#       - accounts_preview_offline_uuid(body) -> dict[str, Any] — 预演离线账户将使用的 UUID，不写入账户状态。
#       - accounts_default_skins(body) -> dict[str, Any] — 获取可供离线账户选择的默认皮肤列表。
#       - accounts_set_offline_skin(body) -> dict[str, Any] — 设置离线账户的默认皮肤。
#       - accounts_add_authlib(body) -> dict[str, Any] — 添加外置登录账户。
#       - accounts_select_authlib_profile(body) -> dict[str, Any] — 为多角色外置账户选择本次登录使用的单个角色。
#       - authlib_resolve_server(body) -> dict[str, Any] — 解析外置登录网站实际使用的 API 地址。
#       - accounts_start_microsoft_login(body) -> dict[str, Any] — 开始微软登录。
#       - accounts_microsoft_login_config(body) -> dict[str, Any] — 获取微软登录配置。
#       - accounts_littleskin_management(body) — 读取当前角色和管理授权状态。
#       - accounts_littleskin_closet(body) — 读取云端衣柜分页。
#       - accounts_littleskin_set_texture(body) — 穿戴已授权的云端材质。
#       - accounts_littleskin_reset_texture(body) — 清除一项穿戴。
#       - wardrobe_import_littleskin_texture(body) — 下载云端材质到本地衣柜。
#       - accounts_littleskin_login_config(body) — 获取 LittleSkin 两种方式的配置可用性。
#       - accounts_start_littleskin_login(body) — 创建 LittleSkin 登录会话。
#       - accounts_poll_littleskin_login(body) — 查询无凭据的 LittleSkin 登录进度。
#       - accounts_select_littleskin_profile(body) — 选择已授权角色。
#       - accounts_complete_littleskin_login(body) — 保存准备完成的 LittleSkin 账户。
#       - accounts_cancel_littleskin_login(body) — 等待实际取消 LittleSkin 登录。
#       - accounts_authlib_login_config(body) -> dict[str, Any] — 获取外置登录可用性配置。
#       - accounts_poll_microsoft_login(body) -> dict[str, Any] — 获取微软登录状态。
#       - accounts_cancel_microsoft_login(body) -> dict[str, Any] — 取消微软登录。
#       - accounts_complete_microsoft_login(body) -> dict[str, Any] — 完成微软登录。
#       - accounts_switch(body) -> dict[str, Any] — 切换账户。
#       - accounts_remove(body) -> dict[str, Any] — 删除账户。
#       - accounts_set_favorite(body) -> dict[str, Any] — 设置账户是否收藏。
#       - accounts_set_pinned(body) -> dict[str, Any] — 设置账户是否置顶。
#       - accounts_refresh_profile(body) -> dict[str, Any] — 刷新账户信息。
#       - accounts_texture_urls(body) -> dict[str, Any] — 返回账户完整皮肤与当前披风地址，图片裁切和渲染由前端完成。
#       - wardrobe_list(body) -> dict[str, Any] — 返回本地衣柜元数据，不包含纹理字节或本地绝对路径。
#       - wardrobe_sync_account_texture(body) -> dict[str, Any] — 将账户当前穿戴的远程皮肤下载到本地衣柜，重复纹理沿用已有条目。
#       - wardrobe_import(body) -> dict[str, Any] — 将用户选择的 PNG 复制到启动器衣柜，并返回去重结果。
#       - wardrobe_update(body) -> dict[str, Any] — 修改衣柜条目的名称或皮肤模型，不转换原始图片。
#       - wardrobe_delete(body) -> dict[str, Any] — 删除本地收藏；已经上传到外部账户的皮肤不受影响。
#       - wardrobe_texture(body) -> dict[str, Any] — 将衣柜中的小型 PNG 原样编码为 Data URL 供 WebView 渲染。
#       - wardrobe_export(body) -> dict[str, Any] — 通过原生保存对话框导出衣柜中的原始 PNG，不经过前端 Base64 往返传输。
#       - wardrobe_apply_skin(body) -> dict[str, Any] — 将衣柜中的标准 64×64 皮肤上传到指定 Microsoft 账户。
#       - microsoft_reset_skin(body) -> dict[str, Any] — 将正版账户皮肤重置为默认。
#       - microsoft_set_cape(body) -> dict[str, Any] — 为正版账户选择已解锁的披风。
#       - microsoft_reset_cape(body) -> dict[str, Any] — 取消正版账户当前佩戴的披风。
#       - authlib_servers(body) -> dict[str, Any] — 获取外置登录服务器。
# ============================================================

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from anyio import to_thread
from pytaurix.plugins.dialog import DialogExt

from ECL.api.models import (
    AccountTextureRequest,
    LittleSkinClosetRequest,
    LittleSkinProfileRequest,
    LittleSkinSessionRequest,
    LittleSkinStartRequest,
    LittleSkinTextureRequest,
    MicrosoftCapeRequest,
    WardrobeApplySkinRequest,
    WardrobeImportRequest,
    WardrobeItemRequest,
    WardrobeSyncTextureRequest,
    WardrobeUpdateRequest,
)
from ECL.minecraft.services.account.wardrobe import WardrobeError, WardrobeStore
from ECL.utils import atomic_write_bytes

from .bridge import _FrontendState, _ipc_handler, _normalize_image_url, _validate_body


class AccountHandlers(_FrontendState):
    """
    处理前端发来的账户、微软登录、皮肤和本地衣柜请求。
    """

    texture_download_chunk_bytes = 64 * 1024
    safe_filename_max_chars = 80
    skin_dimensions = (64, 64)

    async def accounts_list(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取账户列表。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.accounts.list_accounts()}

    async def accounts_current(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取当前账户。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.accounts.current_account()}

    async def accounts_auth_providers(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取插件注册的全部认证提供方定义。

        :param body: 空 IPC 请求数据
        """
        return {"success": True, "data": self.accounts.list_auth_providers()}

    @_ipc_handler("AUTH_PROVIDER_LOGIN_FAILED")
    async def accounts_add_plugin(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        通过插件认证提供方新增账户。

        :param body: 包含提供方标识与登录字段值的 IPC 请求数据
        """
        values = body.get("values")
        account = await to_thread.run_sync(
            self.accounts.add_plugin_account,
            body.get("provider_id"),
            values if isinstance(values, dict) else {},
        )
        return {"success": True, "data": account}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_add_offline(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        添加离线账户。

        :param body: 前端传入的 IPC 请求参数
        """
        account_data = self.accounts.add_offline(body.get("username"), body.get("uuid"), body.get("skin"))
        return {"success": True, "data": account_data}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_preview_offline_uuid(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        预演离线账户将使用的 UUID，不写入任何账户状态。

        供前端在用户输入用户名时即时展示最终 UUID；只读取账户服务的派生规则，
        不新增账户、不切换当前账户，也不触发账户变更事件。

        :param body: 包含待预览 username 的 IPC 请求数据
        """
        preview = self.accounts.preview_offline_uuid(body.get("username"))
        return {"success": True, "data": preview}

    async def accounts_default_skins(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取可供离线账户选择的默认皮肤列表。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.accounts.default_skins()}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_set_offline_skin(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        设置离线账户的默认皮肤。

        :param body: 前端传入的 IPC 请求参数
        """
        accounts = self.accounts.set_offline_skin(body.get("account_id"), body.get("skin"))
        return {"success": True, "data": accounts}

    @_ipc_handler("AUTHLIB_LOGIN_FAILED")
    async def accounts_add_authlib(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        添加外置登录账户。

        :param body: 前端传入的 IPC 请求参数
        """
        server_url = self._normalize_authlib_server_url(body.get("server_url"))
        if server_url is None:
            return {"success": False, "message": "外置登录服务器地址无效", "errorCode": "INVALID_AUTHLIB_SERVER"}
        email = body.get("email")
        if not isinstance(email, str) or not email.strip():
            return {"success": False, "message": "外置登录邮箱不能为空", "errorCode": "INVALID_AUTHLIB_USERNAME"}
        email = email.strip()
        account = await to_thread.run_sync(
            self.accounts.add_authlib,
            server_url,
            email,
            body.get("password"),
        )
        self._remember_authlib_login(account.get("auth_server") or server_url, email)
        return {"success": True, "data": account}

    @_ipc_handler("AUTHLIB_PROFILE_SELECT_FAILED")
    async def accounts_select_authlib_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        为多角色外置账户选择本次登录使用的单个角色。

        :param body: 前端传入的 IPC 请求参数
        """
        account = await to_thread.run_sync(
            self.accounts.select_authlib_profile,
            body.get("account_id"),
            body.get("profile_id"),
        )
        return {"success": True, "data": account}

    @_ipc_handler("AUTHLIB_SERVER_RESOLVE_FAILED")
    async def authlib_resolve_server(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        解析外置登录网站实际使用的 API 地址。

        :param body: 前端传入的 IPC 请求参数
        """
        server_url = self._normalize_authlib_server_url(body.get("server_url"))
        if server_url is None:
            return {"success": False, "message": "外置登录服务器地址无效", "errorCode": "INVALID_AUTHLIB_SERVER"}
        resolved_url = await to_thread.run_sync(self.accounts.resolve_authlib_server, server_url)
        return {"success": True, "data": resolved_url}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_start_microsoft_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        开始微软登录。

        :param body: 前端传入的 IPC 请求参数
        """
        login_data = await self.accounts.start_microsoft_login()
        return {"success": True, "data": login_data}

    async def accounts_microsoft_login_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取微软登录配置。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.accounts.microsoft_login_config()}

    async def accounts_littleskin_login_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        读取 LittleSkin 授权方式的可用性，应用配置不会交给界面。

        :param body: 空 IPC 请求数据
        :return: 两种授权方式及应用 ID 是否已配置
        """
        return {"success": True, "data": self.accounts.littleskin.config()}

    @_ipc_handler("LITTLESKIN_LOGIN_FAILED")
    async def accounts_start_littleskin_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        校验授权方式后创建可轮询和取消的登录会话。

        :param body: 用户选择的授权方式
        :return: 初始无凭据进度
        """
        request, invalid = _validate_body(LittleSkinStartRequest, body)
        if invalid is not None:
            return invalid
        return {"success": True, "data": await self.accounts.start_littleskin_login(request.method, request.account_id)}

    @_ipc_handler("LITTLESKIN_MANAGEMENT_FAILED", timeout=120)
    async def accounts_littleskin_management(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取当前角色穿戴与管理授权状态，不向前端返回凭据。

        :param body: 本地账户标识
        :return: 当前角色与授权状态
        """
        request, invalid = _validate_body(AccountTextureRequest, body)
        if invalid is not None:
            return invalid
        return {
            "success": True,
            "data": await to_thread.run_sync(self.accounts.littleskin_management, request.account_id),
        }

    @_ipc_handler("LITTLESKIN_CLOSET_FAILED", timeout=120)
    async def accounts_littleskin_closet(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        读取已授权用户指定类别的云端衣柜分页。

        :param body: 账户、类别和页码
        :return: 已校验的分页
        """
        request, invalid = _validate_body(LittleSkinClosetRequest, body)
        if invalid is not None:
            return invalid
        result = await to_thread.run_sync(
            self.accounts.littleskin_closet, request.account_id, request.kind, request.page
        )
        return {"success": True, "data": result}

    @_ipc_handler("LITTLESKIN_TEXTURE_FAILED", timeout=120)
    async def accounts_littleskin_set_texture(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        核对当前角色与衣柜后穿戴一项材质，不自动重放写请求。

        :param body: 账户、类别和材质编号
        :return: 已修改的角色
        """
        request, invalid = _validate_body(LittleSkinTextureRequest, body)
        if invalid is not None:
            return invalid
        result = await to_thread.run_sync(
            self.accounts.littleskin_set_texture, request.account_id, request.kind, request.tid
        )
        return {"success": True, "data": result}

    @_ipc_handler("LITTLESKIN_TEXTURE_FAILED", timeout=120)
    async def accounts_littleskin_reset_texture(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        仅清除用户指定的皮肤或披风，保留另一项穿戴。

        :param body: 账户和类别
        :return: 已修改的角色
        """
        request, invalid = _validate_body(WardrobeSyncTextureRequest, body)
        if invalid is not None:
            return invalid
        result = await to_thread.run_sync(self.accounts.littleskin_set_texture, request.account_id, request.kind, None)
        return {"success": True, "data": result}

    @_ipc_handler("WARDROBE_IMPORT_FAILED", timeout=120)
    async def wardrobe_import_littleskin_texture(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        从已核对的云端衣柜下载原始 PNG 并导入本地，凭据不会发送给材质地址。

        :param body: 账户、类别和材质编号
        :return: 本地条目及去重结果
        """
        request, invalid = _validate_body(LittleSkinTextureRequest, body)
        if invalid is not None:
            return invalid
        texture = await to_thread.run_sync(
            self.accounts.authlib_manager.manage_littleskin,
            request.account_id,
            lambda client, credentials, profile_id: client.closet_texture(credentials, request.kind, request.tid),
        )
        contents = await to_thread.run_sync(self._download_account_texture, texture.url)
        item, deduplicated = await to_thread.run_sync(
            self.wardrobe.import_bytes,
            contents,
            request.kind,
            texture.name,
            ("slim" if texture.type == "alex" else "classic") if request.kind == "skin" else None,
        )
        return {"success": True, "data": {"item": item, "deduplicated": deduplicated}}

    @_ipc_handler("LITTLESKIN_LOGIN_FAILED")
    async def accounts_poll_littleskin_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        读取当前会话进度，旧会话不能读取新登录的结果。

        :param body: 本次登录的会话标识
        :return: 无凭据进度
        """
        request, invalid = _validate_body(LittleSkinSessionRequest, body)
        if invalid is not None:
            return invalid
        return {"success": True, "data": self.accounts.littleskin.snapshot(request.session_id)}

    @_ipc_handler("LITTLESKIN_LOGIN_FAILED")
    async def accounts_select_littleskin_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        为已授权的角色创建游戏令牌，尚不保存本地账户。

        :param body: 会话标识和用户选择的角色 UUID
        :return: 角色兑换任务的初始进度
        """
        request, invalid = _validate_body(LittleSkinProfileRequest, body)
        if invalid is not None:
            return invalid
        return {"success": True, "data": await self.accounts.littleskin.select(request.session_id, request.profile_id)}

    @_ipc_handler("LITTLESKIN_SAVE_FAILED")
    async def accounts_complete_littleskin_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        等待准备好的角色账户实际落盘后返回公开信息。

        :param body: 本次登录的会话标识
        :return: 保存后的账户展示资料
        """
        request, invalid = _validate_body(LittleSkinSessionRequest, body)
        if invalid is not None:
            return invalid
        return {"success": True, "data": await self.accounts.littleskin.complete(request.session_id)}

    @_ipc_handler("LITTLESKIN_LOGIN_FAILED", timeout=30)
    async def accounts_cancel_littleskin_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        等待实际网络请求和回调监听结束后确认取消。

        :param body: 需要取消的会话标识
        :return: 实际终止后的无凭据进度
        """
        request, invalid = _validate_body(LittleSkinSessionRequest, body)
        if invalid is not None:
            return invalid
        return {"success": True, "data": await self.accounts.littleskin.cancel(request.session_id)}

    async def accounts_authlib_login_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取外置登录可用性配置。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.game.authlib_login_config()}

    async def accounts_poll_microsoft_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取微软登录状态。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": self.accounts.poll_microsoft_login()}

    async def accounts_cancel_microsoft_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        取消微软登录。

        :param body: 前端传入的 IPC 请求参数
        """
        return {"success": True, "data": {"cancelled": self.accounts.cancel_microsoft_login()}}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_complete_microsoft_login(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        完成微软登录。

        :param body: 前端传入的 IPC 请求参数
        """
        login_result = self.accounts.complete_microsoft_login()
        return {"success": True, "data": login_result}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_switch(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        切换账户。

        :param body: 前端传入的 IPC 请求参数
        """
        self.accounts.switch_account(body.get("account_id"))
        return {"success": True}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_remove(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        删除账户。

        :param body: 前端传入的 IPC 请求参数
        """
        self.accounts.remove_account(body.get("account_id"))
        return {"success": True}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_set_favorite(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        设置账户是否收藏。

        :param body: 包含账户标识与收藏标记的 IPC 请求数据
        :return: 刷新后的完整账户列表
        """
        result = await to_thread.run_sync(
            self.accounts.set_favorite,
            body.get("account_id"),
            bool(body.get("favorite")),
        )
        return {"success": True, "data": result}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_set_pinned(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        设置账户是否置顶。

        :param body: 包含账户标识与置顶标记的 IPC 请求数据
        :return: 刷新后的完整账户列表
        """
        result = await to_thread.run_sync(
            self.accounts.set_pinned,
            body.get("account_id"),
            bool(body.get("pinned")),
        )
        return {"success": True, "data": result}

    @_ipc_handler("ACCOUNT_OPERATION_FAILED")
    async def accounts_refresh_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        刷新账户信息。

        :param body: 前端传入的 IPC 请求参数
        """
        refresh_result = await self.accounts.refresh_account(body.get("account_id"))
        return {"success": True, "data": refresh_result}

    @_ipc_handler("ACCOUNT_TEXTURE_FAILED")
    async def accounts_texture_urls(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回账户完整皮肤与当前披风地址，图片裁切和渲染由前端完成。

        :param body: 包含账户稳定标识的 IPC 请求数据
        :return: 可用的皮肤和披风 URL
        """
        request, invalid = _validate_body(AccountTextureRequest, body)
        if invalid is not None:
            return invalid
        textures = await to_thread.run_sync(self.accounts.texture_urls, request.account_id)
        return {"success": True, "data": textures}

    async def wardrobe_list(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        返回本地衣柜元数据，不包含纹理字节或本地绝对路径。

        :param body: 空 IPC 请求数据
        :return: 按最近更新时间排序的衣柜条目
        """
        return {"success": True, "data": self.wardrobe.list_items()}

    @_ipc_handler("WARDROBE_SYNC_FAILED")
    async def wardrobe_sync_account_texture(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        将账户指定种类的当前材质下载到本地衣柜，重复纹理沿用已有条目。

        :param body: 包含账户稳定标识的 IPC 请求数据
        :return: 本地衣柜条目与哈希去重标记
        """
        request, invalid = _validate_body(WardrobeSyncTextureRequest, body)
        if invalid is not None:
            return invalid
        account_data = await to_thread.run_sync(self.accounts.list_accounts)
        account = next(
            (candidate for candidate in account_data["accounts"] if candidate["id"] == request.account_id),
            None,
        )
        if account is None or account.get("type") == "offline":
            raise WardrobeError("该账户没有可下载的此类材质", "WARDROBE_TEXTURE_UNAVAILABLE")
        textures = await to_thread.run_sync(self.accounts.texture_urls, request.account_id)
        skin_url = _normalize_image_url(textures.get("skinUrl" if request.kind == "skin" else "capeUrl"))
        if skin_url is None:
            raise WardrobeError("该账户没有可下载的此类材质", "WARDROBE_TEXTURE_UNAVAILABLE")
        if account.get("auth_provider") == "littleskin":
            parsed = urlsplit(skin_url)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.netloc != "littleskin.cn"
                or re.fullmatch(r"/textures/[0-9a-f]{64}", parsed.path) is None
                or parsed.query
                or parsed.fragment
            ):
                raise WardrobeError("LittleSkin 材质来源无效", "WARDROBE_TEXTURE_SOURCE_REJECTED")
            skin_url = f"https://littleskin.cn{parsed.path}"
        texture = await to_thread.run_sync(self._download_account_texture, skin_url)
        item, deduplicated = await to_thread.run_sync(
            self.wardrobe.import_bytes,
            texture,
            request.kind,
            f"{account.get('alias') or 'Minecraft'} 当前{'皮肤' if request.kind == 'skin' else '披风'}",
            (textures.get("skinModel") or "classic") if request.kind == "skin" else None,
        )
        self.logger.info(
            "账户当前材质已同步到衣柜：账户标识：%s；纹理标识：%s；已去重：%s",
            request.account_id,
            item["id"],
            ("是" if deduplicated else "否"),
        )
        return {"success": True, "data": {"item": item, "deduplicated": deduplicated}}

    def _download_account_texture(self, url: str) -> bytes:
        # 使用应用共享客户端流式下载账户材质，在写入衣柜前限制响应大小。
        data = bytearray()
        with self.http.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                raise WardrobeError("材质下载返回了未允许的重定向", "WARDROBE_REDIRECT_REJECTED")
            response.raise_for_status()
            for chunk in response.iter_bytes(self.texture_download_chunk_bytes):
                data.extend(chunk)
                if len(data) > WardrobeStore.max_texture_bytes:
                    raise WardrobeError("账户材质超过 5 MiB", "WARDROBE_FILE_TOO_LARGE")
        return bytes(data)

    @_ipc_handler("WARDROBE_IMPORT_FAILED")
    async def wardrobe_import(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        将用户选择的 PNG 复制到启动器衣柜，并返回去重结果。

        :param body: 包含源路径、素材类型和可选模型的 IPC 请求数据
        :return: 导入后的条目与去重标记
        """
        request, invalid = _validate_body(WardrobeImportRequest, body)
        if invalid is not None:
            return invalid
        item, deduplicated = await to_thread.run_sync(
            self.wardrobe.import_file,
            request.path,
            request.kind.value,
            request.name,
            request.model.value if request.model else None,
        )
        return {"success": True, "data": {"item": item, "deduplicated": deduplicated}}

    @_ipc_handler("WARDROBE_UPDATE_FAILED")
    async def wardrobe_update(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        修改衣柜条目的名称或皮肤模型，不转换原始图片。

        :param body: 包含条目标识及待修改字段的 IPC 请求数据
        :return: 更新后的衣柜条目
        """
        request, invalid = _validate_body(WardrobeUpdateRequest, body)
        if invalid is not None:
            return invalid
        item = await to_thread.run_sync(
            self.wardrobe.update_item,
            request.item_id,
            request.name,
            request.model.value if request.model else None,
            request.favorite,
        )
        return {"success": True, "data": item}

    @_ipc_handler("WARDROBE_DELETE_FAILED")
    async def wardrobe_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        删除本地收藏；已经上传到外部账户的皮肤不受影响。

        :param body: 包含衣柜条目标识的 IPC 请求数据
        """
        request, invalid = _validate_body(WardrobeItemRequest, body)
        if invalid is not None:
            return invalid
        await to_thread.run_sync(self.wardrobe.delete_item, request.item_id)
        return {"success": True}

    @_ipc_handler("WARDROBE_TEXTURE_FAILED")
    async def wardrobe_texture(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        将衣柜中的小型 PNG 原样编码为 Data URL 供 WebView 渲染。

        :param body: 包含衣柜条目标识的 IPC 请求数据
        :return: PNG Data URL 和 MIME 类型
        """
        request, invalid = _validate_body(WardrobeItemRequest, body)
        if invalid is not None:
            return invalid
        _, texture = await to_thread.run_sync(self.wardrobe.read_texture, request.item_id)
        encoded = base64.b64encode(texture).decode("ascii")
        return {"success": True, "data": {"dataUrl": f"data:image/png;base64,{encoded}", "mime": "image/png"}}

    @_ipc_handler("WARDROBE_EXPORT_FAILED")
    async def wardrobe_export(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        通过原生保存对话框导出衣柜中的原始 PNG，不经过前端 Base64 往返传输。

        :param body: 包含衣柜条目标识的 IPC 请求数据
        :return: 用户选择的保存路径；取消时路径为空
        """
        request, invalid = _validate_body(WardrobeItemRequest, body)
        if invalid is not None:
            return invalid
        if self._webview is None:
            raise WardrobeError("窗口尚未就绪", "WEBVIEW_NOT_READY")
        item, texture = await to_thread.run_sync(self.wardrobe.read_texture, request.item_id)
        safe_name = (
            re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", item["name"]).strip(" .")[: self.safe_filename_max_chars] or "skin"
        )
        title = "另存为皮肤" if item["kind"] == "skin" else "另存为披风"
        picked = await self._run_native_dialog(
            title,
            lambda: DialogExt.file(self._webview).blocking_save_file(
                add_filter=("PNG 图片", ["png"]),
                set_file_name=f"{safe_name}.png",
                set_title=title,
            ),
        )
        if not picked:
            return {"success": True, "data": {"path": None}}
        target = Path(str(picked))
        await to_thread.run_sync(atomic_write_bytes, target, texture)
        self.logger.info("衣柜纹理已导出：纹理标识：%s；类型：%s", item["id"], item["kind"])
        return {"success": True, "data": {"path": str(target)}}

    @_ipc_handler("SKIN_UPDATE_FAILED", timeout=15)
    async def wardrobe_apply_skin(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        将衣柜中的标准 64×64 皮肤上传到指定 Microsoft 账户。

        :param body: 包含衣柜条目和目标账户标识的 IPC 请求数据
        :return: 上传后刷新得到的账户资料
        """
        request, invalid = _validate_body(WardrobeApplySkinRequest, body)
        if invalid is not None:
            return invalid
        item, texture = await to_thread.run_sync(self.wardrobe.read_texture, request.item_id)
        if item["kind"] != "skin" or (item["width"], item["height"]) != self.skin_dimensions:
            return {
                "success": False,
                "message": "只有标准 64×64 皮肤可以上传到 Microsoft",
                "errorCode": "WARDROBE_UNSUPPORTED_UPLOAD",
            }
        account = await self.accounts.upload_skin(
            request.account_id,
            item["model"] or "classic",
            texture,
        )
        self.logger.info("衣柜皮肤已上传：纹理标识：%s；模型：%s", item["id"], item["model"])
        return {"success": True, "data": account}

    @_ipc_handler("SKIN_UPDATE_FAILED", timeout=15)
    async def microsoft_reset_skin(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        将正版账户皮肤重置为默认。

        :param body: 前端传入的 IPC 请求参数
        """
        request, invalid = _validate_body(AccountTextureRequest, body)
        if invalid is not None:
            return invalid
        account = await self.accounts.reset_skin(request.account_id)
        return {"success": True, "data": account}

    @_ipc_handler("SKIN_UPDATE_FAILED", timeout=15)
    async def microsoft_set_cape(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        为正版账户选择已解锁的披风。

        :param body: 前端传入的 IPC 请求参数
        """
        request, invalid = _validate_body(MicrosoftCapeRequest, body)
        if invalid is not None:
            return invalid
        account = await self.accounts.set_cape(request.account_id, request.cape_id)
        return {"success": True, "data": account}

    @_ipc_handler("SKIN_UPDATE_FAILED", timeout=15)
    async def microsoft_reset_cape(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        取消正版账户当前佩戴的披风。

        :param body: 前端传入的 IPC 请求参数
        """
        request, invalid = _validate_body(AccountTextureRequest, body)
        if invalid is not None:
            return invalid
        account = await self.accounts.reset_cape(request.account_id)
        return {"success": True, "data": account}

    async def authlib_servers(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        获取外置登录服务器。

        :param body: 前端传入的 IPC 请求参数
        """
        authlib_server_list = []
        for server in self._get_authlib_servers():
            server_url = server["url"]
            hostname = urlsplit(server_url).hostname or server_url
            authlib_server_list.append(
                {"name": hostname, "url": server_url, "email": server["email"], "description": server_url}
            )
        return {"success": True, "data": authlib_server_list}
