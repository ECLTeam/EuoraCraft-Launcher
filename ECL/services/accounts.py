# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：账户领域服务：微软/离线/外置登录、凭据刷新与皮肤同步。
#
# 公开接口：
#   - class LauncherMicrosoftAccountManager — 将底层 Microsoft 认证流程适配为启动器账户管理流程。
#       - add_microsoft_account() -> str — 完成 Microsoft 登录并保存可用于启动游戏的账户。
#   - class AccountManager — 聚合离线、Microsoft 与 Authlib 账户，并维护当前账户选择。
#       - microsoft_login_config() -> dict[str, bool] — 返回微软设备代码登录是否具备有效客户端配置。
#       - default_skins() -> list[dict[str, Any]] — 返回可供离线账户选择的默认皮肤列表。
#       - list_auth_providers() -> list[dict[str, Any]] — 返回插件注册的全部认证提供方定义，供前端动态渲染登录表单。
#       - add_plugin_account(provider_id, values) -> dict[str, Any] — 通过插件认证提供方新增账户并切换为当前账户。
#       - set_favorite(account_id, favorite) -> dict[str, Any] — 设置账户是否收藏。
#       - set_pinned(account_id, pinned) -> dict[str, Any] — 设置账户是否置顶。
#       - list_accounts() -> dict[str, Any] — 返回所有账户以及当前账户标识。
#       - current_account() -> dict[str, Any] | None — 获取当前账户。
#       - get_launch_credentials() -> dict[str, str] — 返回启动游戏所需的当前账户凭据。
#       - add_offline(username, custom_uuid=…, skin=…) -> dict[str, Any] — 添加离线账户。
#       - set_offline_skin(account_id, skin) -> list[dict[str, Any]] — 设置离线账户的默认皮肤并持久化，返回刷新后的账户列表。
#       - add_authlib(server_url, username, password) -> dict[str, Any] — 添加外置登录账户。
#       - select_authlib_profile(account_id, profile_id) -> dict[str, Any] — 完成多角色外置登录，只保存用户选中的一个角色。
#       - resolve_authlib_server(server_url) -> str — 返回外置登录地址对应的 Yggdrasil API 地址。
#       - switch_account(account_id) -> None — 切换当前账户；账户不存在时抛出 ``AccountError``。
#       - remove_account(account_id) -> None — 移除账户并自动选择剩余账户。
#       - refresh_account(account_id) -> dict[str, Any] — 刷新账户信息。
#       - texture_urls(account_id) -> dict[str, str] — 返回账户完整皮肤与当前披风的远程纹理地址，供前端统一渲染。
#       - upload_skin(account_id, variant, image) -> dict[str, Any] — 上传皮肤到 Mojang 服务器并刷新账户信息。
#       - reset_skin(account_id) -> dict[str, Any] — 将正版账户皮肤重置为默认。
#       - set_cape(account_id, cape_id) -> dict[str, Any] — 为正版账户选择已解锁的披风。
#       - reset_cape(account_id) -> dict[str, Any] — 取消正版账户当前佩戴的披风。
#       - start_microsoft_login() -> dict[str, Any] — 开始微软登录。
#       - poll_microsoft_login() -> dict[str, Any] — 获取微软登录状态。
#       - complete_microsoft_login() -> dict[str, Any] — 完成已授权的微软登录并返回保存后的账户。
#       - cancel_microsoft_login() -> bool — 取消登录流程；返回本次调用是否实际取消了任务。
#       - close() -> None — 取消仍在运行的认证任务并释放认证客户端。
# ============================================================

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import UUID

import httpx

from ECL.foundation import BuildEnvironment, EventBus
from ECL.game import MicrosoftAuthManager, name_to_uuid
from ECL.plugins.auth_providers import AuthProviderRegistry
from ECL.services.authlib import AuthlibAccountManager, AuthlibError
from ECL.utils import AccountError, atomic_write_text, get_logger


def _load_default_skins(resource_path: Path | None) -> dict[str, tuple[str, str]]:
    # 读取默认皮肤资源，返回 ``skin_id -> (显示名, 贴图 data URL)``。
    if resource_path is None:
        return {}
    skins_dir = resource_path / "resources" / "Skins"
    if not skins_dir.is_dir():
        return {}
    skins: dict[str, tuple[str, str]] = {}
    for entry in sorted(skins_dir.glob("*.png")):
        encoded = base64.b64encode(entry.read_bytes()).decode("ascii")
        skins[entry.stem.lower()] = (entry.stem, f"data:image/png;base64,{encoded}")
    return skins


def _login_progress_result(status: str, state: dict[str, Any], *, force_pending: bool = False) -> dict[str, Any]:
    # 将等待中的登录状态映射为前端可轮询的进度响应。
    result = {
        "status": "pending" if force_pending or status != "progress" else "progress",
        "retry_after": state.get("interval", AccountManager.default_login_poll_interval_seconds),
    }
    if state.get("stage"):
        result["stage"] = state["stage"]
    return result


class _ProgressMinecraftClient:
    """
    为 Minecraft 账户客户端补充登录阶段进度通知。

    :param client: 实际执行令牌和档案请求的 Minecraft 客户端
    :param on_progress: 登录阶段发生变化时调用的回调函数
    """

    def __init__(self, client: Any, on_progress: Callable[[str], None]) -> None:
        self.client = client
        self.on_progress = on_progress
        # 只有设备代码登录期间才发送阶段事件，日常皮肤操作不应污染登录状态。
        self.login_active = False

    async def get_minecraft_token(self, token: str):
        """
        获取 Minecraft 访问令牌并报告授权阶段。

        :param token: Xbox XSTS 令牌
        :return: 底层客户端返回的 Minecraft 令牌响应
        """
        if self.login_active:
            self.on_progress("authorization_confirmed")
        result = await self.client.get_minecraft_token(token)
        if self.login_active:
            self.on_progress("minecraft_token")
        return result

    async def get_profile(self, token: str):
        """
        获取 Minecraft 档案并报告保存阶段。

        :param token: Minecraft 访问令牌
        :return: 底层客户端返回的玩家档案
        """
        if self.login_active:
            self.on_progress("profile")
        result = await self.client.get_profile(token)
        if self.login_active:
            self.on_progress("saving")
        return result

    async def upload_skin(self, token: str, variant: str, image: bytes):
        return await self.client.upload_skin(token, variant, image)

    async def reset_skin(self, token: str):
        return await self.client.reset_skin(token)

    async def set_cape(self, token: str, cape_id: str):
        return await self.client.set_cape(token, cape_id)

    async def reset_cape(self, token: str):
        return await self.client.reset_cape(token)

    async def set_profile_name(self, token: str, name: str):
        return await self.client.set_profile_name(token, name)


class LauncherMicrosoftAccountManager(MicrosoftAuthManager):
    """
    将底层 Microsoft 认证流程适配为启动器账户管理流程。

    :param client_id: Microsoft OAuth 应用客户端 ID
    :param on_device_code: 收到设备代码后调用的回调函数
    :param on_progress: 登录阶段发生变化时调用的回调函数
    :param cache_path: Microsoft 令牌缓存目录
    """

    def __init__(
        self,
        client_id: str,
        on_device_code: Callable[[dict[str, str]], None],
        on_progress: Callable[[str], None],
        cache_path: Path | str | None = None,
        verify: bool = True,
    ) -> None:
        super().__init__(client_id, cache_path, on_device_code, verify=verify)
        self._progress_client = _ProgressMinecraftClient(self.minecraft_client, on_progress)
        self.minecraft_client = self._progress_client

    async def add_microsoft_account(self) -> str:
        """
        完成 Microsoft 登录并保存可用于启动游戏的账户。

        :return: 新增账户的稳定标识
        """
        self._progress_client.login_active = True
        try:
            return await super().add_microsoft_account()
        finally:
            self._progress_client.login_active = False


class AccountManager:
    """
    聚合离线、Microsoft 与 Authlib 账户，并维护当前账户选择。

    Microsoft 设备码登录的任务状态也由本类统一拥有，确保同一时间只有一个登录流程。
    Authlib 协议和头像渲染保留独立实现文件，因为它们具有独立的协议与图像处理边界。

    :param data_path: 启动器数据目录
    :param microsoft_manager: 测试或定制环境提供的 Microsoft 认证管理器
    :param microsoft_client_id: Microsoft OAuth 客户端 ID
    :param authlib_manager: 测试或定制环境提供的 Authlib 管理器
    :param event_bus: 当前应用上下文拥有的事件总线
    """

    microsoft_login_poll_interval_seconds = 2
    default_login_poll_interval_seconds = 5
    login_start_wait_seconds = 15
    login_waiting_statuses = frozenset({"starting", "pending", "progress"})
    login_status_messages = {
        "error": "Microsoft 登录失败",
        "cancelled": "Microsoft 登录已取消",
    }

    def __init__(
        self,
        data_path: Path | str,
        microsoft_manager: MicrosoftAuthManager | None = None,
        microsoft_client_id: str | None = None,
        authlib_manager: AuthlibAccountManager | None = None,
        event_bus: EventBus | None = None,
        disable_ssl_verify: bool = False,
        resource_path: Path | str | None = None,
        state_dir: Path | str | None = None,
    ):
        """
        加载持久化账户，并连接两种在线认证提供者。

        :param data_path: 启动器数据目录
        :param microsoft_manager: 可选的 Microsoft 认证管理器
        :param microsoft_client_id: Microsoft OAuth 客户端 ID
        :param authlib_manager: 可选的 Authlib 账户管理器
        :param event_bus: 当前应用上下文拥有的事件总线
        :param disable_ssl_verify: 是否关闭 Microsoft 登录服务器的 SSL 证书校验
        :param resource_path: 只读资源目录，用于定位 ``resources/Skins`` 默认皮肤
        :param state_dir: 账户聚合状态目录；生产环境统一使用 ``~/.ECL/accounts``，
            缺省时保持数据目录内的历史位置（测试兼容）
        """
        self.logger = get_logger("AccountManager")
        self.events = event_bus or EventBus()
        self.data_path = Path(state_dir) if state_dir is not None else Path(data_path) / "accounts"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_path / "accounts.json"
        # 插件认证提供方由插件框架共享；插件账户持久化于聚合状态文件中。
        self.plugin_auth_providers = AuthProviderRegistry()
        self._plugin_accounts: dict[str, dict[str, Any]] = {}
        # 持久化状态和登录任务会被 IPC 轮询并发访问，统一使用一把可重入锁保护。
        self._lock = RLock()
        # 登录事件表示设备码流程已经推进；取消事件用于请求登录任务尽快终止。
        self._login_event: asyncio.Event = asyncio.Event()
        self._login_cancel_event: asyncio.Event = asyncio.Event()
        self._login_task: asyncio.Task | None = None
        # 登录流只在需要向前端展示设备码时存在，状态字典则始终可供轮询。
        self._login_flow: dict[str, Any] | None = None
        self._login_state: dict[str, Any] = {"status": "idle"}
        # 离线账户和当前选择由本类持久化；在线账户数据由对应提供者保存。
        self._offline_accounts: dict[str, dict[str, Any]] = {}
        self._current_account_id: str | None = None
        # 账户偏好（收藏/置顶）独立存储，覆盖离线、微软与外置账户。
        self._account_prefs: dict[str, dict[str, bool]] = {}
        self.resource_path = Path(resource_path).resolve() if resource_path else None
        self._default_skins: dict[str, tuple[str, str]] | None = None
        self._load_state()
        self.logger.debug("账户聚合状态读取完成")

        manager_was_provided = microsoft_manager is not None
        effective_client_id = (microsoft_client_id or BuildEnvironment.microsoft_client_id).strip()
        self._microsoft_login_available = manager_was_provided or bool(effective_client_id)
        if microsoft_manager is None:
            self.logger.debug("正在创建 Microsoft 认证管理器")
            microsoft_manager = LauncherMicrosoftAccountManager(
                client_id=effective_client_id,
                on_device_code=self._on_device_code,
                on_progress=self._on_microsoft_progress,
                verify=not disable_ssl_verify,
            )
            self.logger.debug("Microsoft 认证管理器已创建")
        else:
            microsoft_manager.on_device_code = self._on_device_code
        self.microsoft_manager = microsoft_manager
        if authlib_manager is None:
            self.logger.debug("正在创建 Authlib 账户管理器")
            authlib_manager = AuthlibAccountManager()
            self.logger.debug("Authlib 账户管理器已创建")
        self.authlib_manager = authlib_manager
        self._deduplicate_microsoft_accounts()
        self._ensure_current_account()
        self.logger.debug(
            "账户服务已加载：离线账户数：%d；微软账户数：%d；外置账户数：%d；已选择当前账户：%s",
            len(self._offline_accounts),
            len(self.microsoft_manager.get_microsoft_accounts()),
            len(self.authlib_manager.list_accounts()),
            ("是" if self._current_account_id is not None else "否"),
        )

    def microsoft_login_config(self) -> dict[str, bool]:
        """
        返回微软设备代码登录是否具备有效客户端配置。
        """
        return {
            "available": self._microsoft_login_available,
            "needs_client_id": not self._microsoft_login_available,
        }

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            offline_accounts = state.get("offline_accounts", {})
            if isinstance(offline_accounts, dict):
                self._offline_accounts = offline_accounts
            current_account_id = state.get("current_account_id")
            if isinstance(current_account_id, str) and current_account_id:
                self._current_account_id = current_account_id
            account_prefs = state.get("account_prefs", {})
            if isinstance(account_prefs, dict):
                self._account_prefs = {
                    str(account_id): flags
                    for account_id, flags in account_prefs.items()
                    if isinstance(flags, dict) and isinstance(account_id, (str, int))
                }
            plugin_accounts = state.get("plugin_accounts", {})
            if isinstance(plugin_accounts, dict):
                self._plugin_accounts = {
                    str(account_id): info for account_id, info in plugin_accounts.items() if isinstance(info, dict)
                }
            self.logger.debug(
                "已读取账户聚合状态：离线账户数：%d；插件：%d；已选择当前账户：%s",
                len(self._offline_accounts),
                len(self._plugin_accounts),
                ("是" if self._current_account_id is not None else "否"),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.logger.warning("读取账号状态失败，将使用空账号列表: %s", exc)

    def _save_state(self) -> None:
        state = {
            "offline_accounts": self._offline_accounts,
            "plugin_accounts": self._plugin_accounts,
            "current_account_id": self._current_account_id,
            "account_prefs": self._account_prefs,
        }
        try:
            atomic_write_text(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))
        except (OSError, TypeError, ValueError) as exc:
            raise AccountError("保存账号数据失败", "ACCOUNT_SAVE_FAILED") from exc

    @staticmethod
    def _validate_username(username: Any) -> str:
        if not isinstance(username, str):
            raise AccountError("离线用户名不能为空", "INVALID_OFFLINE_USERNAME")
        normalized = username.strip()
        if not normalized:
            raise AccountError("离线用户名不能为空", "INVALID_OFFLINE_USERNAME")
        if len(normalized) > 16 or any(character.isspace() for character in normalized):
            raise AccountError("离线用户名必须为 1 到 16 个不含空格的字符", "INVALID_OFFLINE_USERNAME")
        return normalized

    def _microsoft_account(self, account_id: str, info: dict[str, Any]) -> dict[str, Any]:
        profile = info.get("Profile") or {}
        account = {
            "id": account_id,
            "alias": profile.get("name") or info.get("Email") or account_id,
            "type": "microsoft",
            "email": info.get("Email") or "",
            "uuid": profile.get("id") or "",
            "isCurrent": account_id == self._current_account_id,
        }

        skins = profile.get("skins") or []
        if skins and isinstance(skins[0], dict):
            skin_url = skins[0].get("url")
            if isinstance(skin_url, str) and skin_url:
                account["skinUrl"] = skin_url

        capes = profile.get("capes") or []
        if isinstance(capes, list):
            valid_capes = []
            for cape in capes:
                if isinstance(cape, dict) and cape.get("id"):
                    valid_capes.append(
                        {
                            "id": str(cape.get("id")),
                            "name": str(cape.get("alias") or cape.get("name") or cape.get("id")),
                            "state": str(cape.get("state") or ""),
                            "url": str(cape.get("url") or ""),
                        }
                    )
            if valid_capes:
                account["capes"] = valid_capes

        return account

    @staticmethod
    def _microsoft_identity(info: dict[str, Any]) -> str | None:
        profile = info.get("Profile") or {}
        account_uuid = profile.get("id")
        if isinstance(account_uuid, str) and account_uuid.strip():
            return f"uuid:{account_uuid.replace('-', '').strip().lower()}"

        email = info.get("Email")
        if isinstance(email, str) and email.strip():
            return f"email:{email.strip().casefold()}"
        return None

    def _authlib_account(self, account_id: str, info: dict[str, Any]) -> dict[str, Any]:
        profiles = info.get("Profiles") or {}
        profile = profiles.get("selectedProfile") or {}
        account = {
            "id": account_id,
            "alias": profile.get("name") or "请选择角色",
            "type": "authlib",
            "email": info.get("Username") or "",
            "uuid": profile.get("id") or "",
            "auth_server": info.get("YggdrasilAPI") or "",
            "isCurrent": account_id == self._current_account_id,
        }
        available_profiles = profiles.get("availableProfiles") or []
        if not profile and isinstance(available_profiles, list):
            account["profile_selection_required"] = True
            account["available_profiles"] = deepcopy(available_profiles)
        return account

    def _deduplicate_microsoft_accounts(self, preferred_id: str | None = None) -> str | None:
        accounts = self.microsoft_manager.get_microsoft_accounts()
        canonical_by_identity: dict[str, str] = {}
        replacements: dict[str, str] = {}

        for account_id, info in accounts.items():
            identity = self._microsoft_identity(info)
            if identity is None:
                continue

            existing_id = canonical_by_identity.get(identity)
            if existing_id is None:
                canonical_by_identity[identity] = account_id
                continue

            candidate_ids = {existing_id, account_id}
            if preferred_id in candidate_ids:
                canonical_id = preferred_id
            elif self._current_account_id in candidate_ids:
                canonical_id = self._current_account_id
            else:
                canonical_id = account_id

            duplicate_id = existing_id if canonical_id == account_id else account_id
            canonical_by_identity[identity] = canonical_id
            replacements[duplicate_id] = canonical_id

        for duplicate_id in replacements:
            self.microsoft_manager.del_microsoft_account(duplicate_id)

        def resolve_account_id(account_id: str | None) -> str | None:
            while account_id in replacements:
                account_id = replacements[account_id]
            return account_id

        current_account_id = resolve_account_id(self._current_account_id)
        if current_account_id != self._current_account_id:
            self._current_account_id = current_account_id
            if self.state_path.exists():
                self._save_state()

        if replacements:
            self.logger.info("已合并 %s 个重复 Microsoft 账户", len(replacements))
        return resolve_account_id(preferred_id)

    def default_skins(self) -> list[dict[str, Any]]:
        """
        返回可供离线账户选择的默认皮肤列表。
        """
        return [
            {"id": skin_id, "name": name, "skinUrl": url} for skin_id, (name, url) in self._default_skin_map().items()
        ]

    def _default_skin_map(self) -> dict[str, tuple[str, str]]:
        if self._default_skins is None:
            self._default_skins = _load_default_skins(self.resource_path)
        return self._default_skins

    def _default_skin_url(self, skin_id: Any) -> str | None:
        info = self._default_skin_map().get(str(skin_id or "").lower())
        return info[1] if info else None

    def _normalize_offline_skin(self, skin: Any) -> str | None:
        if skin is None or not str(skin).strip():
            return None
        skin_id = str(skin).strip().lower()
        if skin_id not in self._default_skin_map():
            raise AccountError("离线账户皮肤无效", "INVALID_OFFLINE_SKIN")
        return skin_id

    def _offline_account(self, account: dict[str, Any]) -> dict[str, Any]:
        copied = deepcopy(account)
        skin_url = self._default_skin_url(copied.get("skin"))
        if skin_url is not None:
            copied["skinUrl"] = skin_url
        return copied

    def _plugin_account(self, account_id: str, info: dict[str, Any]) -> dict[str, Any]:
        # 将插件账户原始信息转换为公开账户字典。
        provider = self.plugin_auth_providers.get(info.get("provider") or "")
        account = {
            "id": account_id,
            "alias": info.get("alias") or info.get("player_name") or "插件账户",
            "type": "plugin",
            "provider": info.get("provider") or "",
            "providerTitle": provider.title if provider is not None else (info.get("provider_title") or ""),
            "uuid": info.get("uuid") or "",
            "isCurrent": account_id == self._current_account_id,
        }
        if info.get("email"):
            account["email"] = info["email"]
        if info.get("skinUrl"):
            account["skinUrl"] = info["skinUrl"]
        return account

    def list_auth_providers(self) -> list[dict[str, Any]]:
        """
        返回插件注册的全部认证提供方定义，供前端动态渲染登录表单。
        """
        return [provider.to_dict() for provider in self.plugin_auth_providers.list_providers()]

    def add_plugin_account(self, provider_id: Any, values: Any) -> dict[str, Any]:
        """
        通过插件认证提供方新增账户并切换为当前账户。

        :param provider_id: 插件注册的提供方标识
        :param values: 登录表单字段值字典
        :return: 新增账户的公开字典
        """
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise AccountError("认证提供方不能为空", "AUTH_PROVIDER_NOT_FOUND")
        if not isinstance(values, dict):
            raise AccountError("登录表单数据无效", "INVALID_AUTH_FIELDS")
        provider = self.plugin_auth_providers.get(provider_id)
        if provider is None:
            raise AccountError("认证提供方不存在或已卸载", "AUTH_PROVIDER_NOT_FOUND")
        try:
            info = provider.authenticate(values)
        except AccountError:
            raise
        except Exception as exc:
            raise AccountError(f"认证失败: {exc}", "AUTH_PROVIDER_LOGIN_FAILED") from exc
        if not isinstance(info, dict) or not info.get("id"):
            raise AccountError("认证提供方返回数据无效", "AUTH_PROVIDER_LOGIN_FAILED")
        account_id = f"plugin:{provider_id}:{info['id']}"
        with self._lock:
            self._plugin_accounts[account_id] = {
                "provider": provider_id,
                "alias": str(info.get("alias") or info.get("player_name") or "").strip(),
                "uuid": str(info.get("uuid") or "").strip(),
                "email": str(info.get("email") or "").strip() or None,
                "skinUrl": str(info.get("skinUrl") or "").strip() or None,
                "data": info.get("data"),
            }
            self._current_account_id = account_id
            self._save_state()
        self.logger.info("当前账户已切换并保存")
        self._emit_changed()
        return self._plugin_account(account_id, self._plugin_accounts[account_id])

    def _all_accounts(self) -> list[dict[str, Any]]:
        offline_accounts = [self._offline_account(account) for account in self._offline_accounts.values()]
        microsoft_accounts = [
            self._microsoft_account(account_id, info)
            for account_id, info in self.microsoft_manager.get_microsoft_accounts().items()
        ]
        authlib_accounts = [
            self._authlib_account(account_id, info) for account_id, info in self.authlib_manager.list_accounts().items()
        ]
        plugin_accounts = [self._plugin_account(account_id, info) for account_id, info in self._plugin_accounts.items()]
        accounts = offline_accounts + microsoft_accounts + authlib_accounts + plugin_accounts
        for account in accounts:
            account["isCurrent"] = account["id"] == self._current_account_id
            self._apply_account_prefs(account)
        # 置顶账户优先，其次是收藏账户，组内保持原有顺序。
        accounts.sort(key=lambda account: (not account.get("pinned", False), not account.get("favorite", False)))
        return accounts

    def _ensure_current_account(self) -> None:
        with self._lock:
            accounts = self._all_accounts()
            account_ids = {account["id"] for account in accounts}
            if self._current_account_id in account_ids:
                return
            self._current_account_id = accounts[0]["id"] if accounts else None
            if accounts or self.state_path.exists():
                self._save_state()

    def _apply_account_prefs(self, account: dict[str, Any]) -> None:
        # 在账户数据中注入收藏/置顶标记。
        prefs = self._account_prefs.get(account["id"], {})
        account["favorite"] = bool(prefs.get("favorite"))
        account["pinned"] = bool(prefs.get("pinned"))

    def set_favorite(self, account_id: Any, favorite: bool) -> dict[str, Any]:
        """
        设置账户是否收藏。

        :param account_id: 账户的稳定标识
        :param favorite: 是否收藏
        :returns: 刷新后的完整账户列表
        """
        return self._set_account_flag(account_id, "favorite", favorite)

    def set_pinned(self, account_id: Any, pinned: bool) -> dict[str, Any]:
        """
        设置账户是否置顶。

        :param account_id: 账户的稳定标识
        :param pinned: 是否置顶
        :returns: 刷新后的完整账户列表
        """
        return self._set_account_flag(account_id, "pinned", pinned)

    def _set_account_flag(self, account_id: Any, flag: str, value: bool) -> dict[str, Any]:
        # 设置账户的布尔标记（收藏/置顶）并持久化。
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        self._validate_account_prefs(account_id)
        with self._lock:
            prefs = self._account_prefs.setdefault(account_id, {})
            prefs[flag] = bool(value)
            self._save_state()
        self._emit_changed()
        return self.list_accounts()

    def _validate_account_prefs(self, account_id: str) -> None:
        # 验证账户是否存在，不存在时抛出 ``AccountError``。
        with self._lock:
            account_ids = {account["id"] for account in self._all_accounts()}
        if account_id not in account_ids:
            raise AccountError("账号不存在，无法设置偏好", "ACCOUNT_NOT_FOUND")

    def list_accounts(self) -> dict[str, Any]:
        """
        返回所有账户以及当前账户标识。
        """
        with self._lock:
            accounts = self._all_accounts()
            current = next((account for account in accounts if account["isCurrent"]), None)
            return {"accounts": accounts, "current": current}

    def current_account(self) -> dict[str, Any] | None:
        """
        获取当前账户。
        """
        return self.list_accounts()["current"]

    async def get_launch_credentials(self) -> dict[str, str]:
        """
        返回启动游戏所需的当前账户凭据。
        """
        with self._lock:
            current = next(
                (account for account in self._all_accounts() if account["id"] == self._current_account_id),
                None,
            )
        if current is None:
            raise AccountError("请先选择一个游戏账户", "ACCOUNT_REQUIRED")

        account_uuid = str(current.get("uuid") or "").replace("-", "")
        player_name = str(current.get("alias") or "").strip()
        if not account_uuid or not player_name:
            raise AccountError("当前账户资料不完整", "ACCOUNT_PROFILE_INVALID")
        if current.get("type") == "offline":
            return {
                "player_name": player_name,
                "uuid": account_uuid,
                "user_type": "legacy",
                "access_token": "None",
            }
        if current.get("type") == "microsoft":
            token = await self.microsoft_manager.get_minecraft_token(current["id"])
            return {
                "player_name": player_name,
                "uuid": account_uuid,
                "user_type": "msa",
                "access_token": token,
            }
        if current.get("type") == "authlib":
            try:
                token = self.authlib_manager.get_token(current["id"])
            except (AuthlibError, httpx.HTTPError, OSError, KeyError, TypeError, ValueError) as exc:
                raise AccountError(f"刷新外置登录令牌失败: {exc}", "AUTHLIB_TOKEN_FAILED") from exc
            return {
                "player_name": player_name,
                "uuid": account_uuid,
                "user_type": "yggdrasil",
                "access_token": token["AccessToken"],
                "auth_server": token["YggdrasilAPI"],
            }
        if current.get("type") == "plugin":
            return self._plugin_launch_credentials(current)
        raise AccountError("当前账户类型暂不支持启动游戏", "ACCOUNT_TYPE_UNSUPPORTED")

    def _plugin_launch_credentials(self, current: dict[str, Any]) -> dict[str, str]:
        # 通过插件认证提供方解析启动凭据。
        provider = self.plugin_auth_providers.get(current.get("provider") or "")
        if provider is None:
            raise AccountError("认证提供方已卸载，无法启动游戏", "AUTH_PROVIDER_UNAVAILABLE")
        try:
            credentials = provider.resolve_credentials(current)
        except Exception as exc:
            raise AccountError(f"解析插件账户凭据失败: {exc}", "AUTH_PROVIDER_CREDENTIAL_FAILED") from exc
        if not isinstance(credentials, dict):
            raise AccountError("认证提供方返回的凭据无效", "AUTH_PROVIDER_CREDENTIAL_FAILED")
        user_type = str(credentials.get("user_type") or "").lower()
        if user_type not in {"msa", "yggdrasil", "legacy"}:
            raise AccountError(f"认证提供方返回了不支持的凭据类型: {user_type}", "AUTH_PROVIDER_CREDENTIAL_FAILED")
        player_name = str(credentials.get("player_name") or current.get("alias") or "").strip()
        uuid = str(credentials.get("uuid") or current.get("uuid") or "").replace("-", "")
        if not player_name or not uuid:
            raise AccountError("认证提供方返回的凭据不完整", "AUTH_PROVIDER_CREDENTIAL_FAILED")
        resolved = {
            "player_name": player_name,
            "uuid": uuid,
            "user_type": user_type,
            "access_token": str(credentials.get("access_token") or ("None" if user_type == "legacy" else "")),
        }
        if credentials.get("auth_server"):
            resolved["auth_server"] = str(credentials["auth_server"])
        return resolved

    @staticmethod
    def _resolve_offline_uuid(username: str, custom_uuid: Any = None) -> str:
        if custom_uuid is None or custom_uuid == "":
            return str(name_to_uuid(username))
        if not isinstance(custom_uuid, str):
            raise AccountError("自定义 UUID 必须是字符串", "INVALID_OFFLINE_UUID")

        value = custom_uuid.strip()
        compact_value = value.replace("-", "")
        if len(compact_value) != 32 or any(character not in "0123456789abcdefABCDEF" for character in compact_value):
            raise AccountError("自定义 UUID 格式无效", "INVALID_OFFLINE_UUID")
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise AccountError("自定义 UUID 格式无效", "INVALID_OFFLINE_UUID") from exc

    def add_offline(self, username: Any, custom_uuid: Any = None, skin: Any = None) -> dict[str, Any]:
        """
        添加离线账户。

        :param username: 登录或离线账户用户名
        :param custom_uuid: 可选的离线账户 UUID
        :param skin: 可选的默认皮肤标识
        """
        normalized = self._validate_username(username)
        account_uuid = self._resolve_offline_uuid(normalized, custom_uuid)
        account_id = f"offline:{account_uuid}"
        skin_id = self._normalize_offline_skin(skin)
        with self._lock:
            account = {
                "id": account_id,
                "alias": normalized,
                "type": "offline",
                "uuid": account_uuid,
                "isCurrent": True,
            }
            if skin_id is not None:
                account["skin"] = skin_id
            self._offline_accounts[account_id] = account
            self._current_account_id = account_id
            self._save_state()
        self._emit_changed()
        return deepcopy(account)

    def set_offline_skin(self, account_id: Any, skin: Any) -> list[dict[str, Any]]:
        """
        设置离线账户的默认皮肤并持久化，返回刷新后的账户列表。

        :param account_id: 离线账户的稳定标识
        :param skin: 默认皮肤标识，空值表示不设置
        """
        if not isinstance(account_id, str) or account_id not in self._offline_accounts:
            raise AccountError("账号不存在或不是离线账户", "ACCOUNT_NOT_FOUND")
        skin_id = self._normalize_offline_skin(skin)
        with self._lock:
            account = self._offline_accounts[account_id]
            if skin_id is None:
                account.pop("skin", None)
            else:
                account["skin"] = skin_id
            self._save_state()
        self._emit_changed()
        with self._lock:
            return self._all_accounts()

    def add_authlib(self, server_url: Any, username: Any, password: Any) -> dict[str, Any]:
        """
        添加外置登录账户。

        :param server_url: Authlib 认证服务器地址
        :param username: 登录或离线账户用户名
        :param password: 仅用于本次认证且不会写入日志的密码
        """
        if not isinstance(server_url, str) or not server_url.strip():
            raise AccountError("外置登录服务器不能为空", "INVALID_AUTHLIB_SERVER")
        if not isinstance(username, str) or not username.strip():
            raise AccountError("外置登录用户名不能为空", "INVALID_AUTHLIB_USERNAME")
        if not isinstance(password, str) or not password:
            raise AccountError("外置登录密码不能为空", "INVALID_AUTHLIB_PASSWORD")
        try:
            account_id, info = self.authlib_manager.add_account(
                server_url.strip(),
                username.strip(),
                password,
            )
        except httpx.HTTPStatusError as exc:
            try:
                error = exc.response.json()
            except json.JSONDecodeError:
                error = {}

            message = None
            if isinstance(error, dict):
                message = error.get("errorMessage") or error.get("error")
            if not isinstance(message, str) or not message.strip():
                message = f"认证服务器返回 {exc.response.status_code} {exc.response.reason_phrase}"
            raise AccountError(f"外置登录失败: {message.strip()}", "AUTHLIB_LOGIN_FAILED") from exc
        except (AuthlibError, httpx.RequestError, OSError, KeyError, TypeError, ValueError) as exc:
            raise AccountError(f"外置登录失败: {exc}", "AUTHLIB_LOGIN_FAILED") from exc
        with self._lock:
            selection_required = not bool((info.get("Profiles") or {}).get("selectedProfile"))
            if not selection_required:
                self._current_account_id = account_id
                self._save_state()
            account = self._authlib_account(account_id, info)
        if not account.get("profile_selection_required"):
            self._emit_changed()
        return account

    def select_authlib_profile(self, account_id: Any, profile_id: Any) -> dict[str, Any]:
        """
        完成多角色外置登录，只保存用户选中的一个角色。

        :param account_id: 账户的稳定标识
        :param profile_id: Authlib 玩家档案标识
        """
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        if not isinstance(profile_id, str) or not profile_id:
            raise AccountError("请选择要登录的角色", "INVALID_AUTHLIB_PROFILE")
        try:
            selected_account_id, info = self.authlib_manager.select_profile(account_id, profile_id)
        except httpx.HTTPStatusError as exc:
            try:
                error = exc.response.json()
            except json.JSONDecodeError:
                error = {}
            message = error.get("errorMessage") if isinstance(error, dict) else None
            if not isinstance(message, str) or not message.strip():
                message = f"认证服务器返回 {exc.response.status_code} {exc.response.reason_phrase}"
            raise AccountError(f"选择外置登录角色失败: {message.strip()}", "AUTHLIB_PROFILE_SELECT_FAILED") from exc
        except (AuthlibError, httpx.RequestError, OSError, KeyError, TypeError, ValueError) as exc:
            raise AccountError(f"选择外置登录角色失败: {exc}", "AUTHLIB_PROFILE_SELECT_FAILED") from exc
        with self._lock:
            self._current_account_id = selected_account_id
            self._save_state()
            account = self._authlib_account(selected_account_id, info)
        self._emit_changed()
        return account

    def resolve_authlib_server(self, server_url: Any) -> str:
        """
        返回外置登录地址对应的 Yggdrasil API 地址。

        :param server_url: Authlib 认证服务器地址
        """
        if not isinstance(server_url, str) or not server_url.strip():
            raise AccountError("外置登录服务器不能为空", "INVALID_AUTHLIB_SERVER")
        try:
            return self.authlib_manager.resolve_server(server_url.strip())
        except httpx.HTTPError as exc:
            raise AccountError(f"无法识别外置登录服务器: {exc}", "AUTHLIB_SERVER_RESOLVE_FAILED") from exc

    def switch_account(self, account_id: Any) -> None:
        """
        切换当前账户；账户不存在时抛出 ``AccountError``。

        :param account_id: 账户的稳定标识
        """
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        with self._lock:
            account_ids = {account["id"] for account in self._all_accounts()}
            if account_id not in account_ids:
                raise AccountError("账号不存在", "ACCOUNT_NOT_FOUND")
            self._current_account_id = account_id
            self._save_state()
        self._emit_changed()

    def remove_account(self, account_id: Any) -> None:
        """
        移除账户并自动选择剩余账户。

        :param account_id: 账户的稳定标识
        """
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        with self._lock:
            if account_id in self._offline_accounts:
                self._offline_accounts.pop(account_id)
            elif account_id in self._plugin_accounts:
                self._plugin_accounts.pop(account_id)
            elif account_id in self.microsoft_manager.get_microsoft_accounts():
                self.microsoft_manager.del_microsoft_account(account_id)
            elif account_id in self.authlib_manager.list_accounts():
                self.authlib_manager.delete_account(account_id)
            else:
                raise AccountError("账号不存在", "ACCOUNT_NOT_FOUND")

            if self._current_account_id == account_id:
                remaining_accounts = self._all_accounts()
                self._current_account_id = remaining_accounts[0]["id"] if remaining_accounts else None
            self._account_prefs.pop(account_id, None)
            self._save_state()
        self.logger.info("账户已移除，当前账户选择已保存")
        self._emit_changed()

    async def refresh_account(self, account_id: Any) -> dict[str, Any]:
        """
        刷新账户信息。

        :param account_id: 账户的稳定标识
        """
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        is_microsoft = False
        is_authlib = False
        self.logger.info("开始刷新账户资料")
        with self._lock:
            if account_id in self._offline_accounts:
                account = deepcopy(self._offline_accounts[account_id])
            elif account_id in self._plugin_accounts:
                self.logger.info("账户资料刷新完成，使用插件提供的资料")
                return self._plugin_account(account_id, deepcopy(self._plugin_accounts[account_id]))
            elif account_id in self.microsoft_manager.get_microsoft_accounts():
                is_microsoft = True
            elif account_id in self.authlib_manager.list_accounts():
                is_authlib = True
            else:
                raise AccountError("账号不存在", "ACCOUNT_NOT_FOUND")
        # 网络刷新在锁外进行，避免持锁期间阻塞事件循环上的内存读取。
        if is_microsoft:
            await self.microsoft_manager.refresh_profile(account_id)
            with self._lock:
                info = self.microsoft_manager.get_microsoft_accounts()[account_id]
                account = self._microsoft_account(account_id, info)
        elif is_authlib:
            info = self.authlib_manager.refresh_account(account_id)
            with self._lock:
                account = self._authlib_account(account_id, info)
        self._emit_changed()
        self.logger.info("账户资料刷新完成")
        return account

    def texture_urls(self, account_id: str) -> dict[str, str]:
        """
        返回账户完整皮肤与当前披风的远程纹理地址，供前端统一渲染。

        离线账户没有远程材质；Microsoft 直接使用缓存档案，Authlib 只解析会话服务器
        返回的材质元数据，不在后端下载图片。

        :param account_id: 账户的稳定标识
        :return: 可用的 ``skinUrl``、``skinModel`` 与 ``capeUrl``；离线或无材质时返回空字典
        """
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        with self._lock:
            microsoft = self.microsoft_manager.get_microsoft_accounts().get(account_id)
            if microsoft is not None:
                profile = microsoft.get("Profile") or {}
                result: dict[str, str] = {}
                skins = profile.get("skins") or []
                if skins and isinstance(skins[0], dict) and isinstance(skins[0].get("url"), str):
                    result["skinUrl"] = skins[0]["url"]
                    variant = str(skins[0].get("variant") or "classic").lower()
                    result["skinModel"] = "slim" if variant == "slim" else "classic"
                active_cape = next(
                    (
                        cape
                        for cape in profile.get("capes") or []
                        if isinstance(cape, dict) and str(cape.get("state") or "").upper() == "ACTIVE"
                    ),
                    None,
                )
                if active_cape and isinstance(active_cape.get("url"), str):
                    result["capeUrl"] = active_cape["url"]
                return result
            if account_id in self._offline_accounts:
                skin_url = self._default_skin_url(self._offline_accounts[account_id].get("skin"))
                if skin_url:
                    # 离线账户没有模型元数据，默认按纤细手臂渲染
                    return {"skinUrl": skin_url, "skinModel": "slim"}
                return {}
            if account_id in self._plugin_accounts:
                skin_url = self._plugin_accounts[account_id].get("skinUrl")
                if skin_url:
                    return {"skinUrl": skin_url, "skinModel": "classic"}
                return {}
            is_authlib = account_id in self.authlib_manager.list_accounts()
        if is_authlib:
            return self.authlib_manager.get_texture_urls(account_id)
        raise AccountError("账号不存在", "ACCOUNT_NOT_FOUND")

    def _require_microsoft_account(self, account_id: Any) -> None:
        # 校验 account_id 是否为正版(Microsoft)账户。
        if not isinstance(account_id, str) or not account_id:
            raise AccountError("账号 ID 不能为空", "INVALID_ACCOUNT_ID")
        if account_id not in self.microsoft_manager.get_microsoft_accounts():
            raise AccountError("账号不存在或不是正版账户", "ACCOUNT_NOT_FOUND")

    async def upload_skin(self, account_id: Any, variant: Any, image: bytes) -> dict[str, Any]:
        """
        上传皮肤到 Mojang 服务器并刷新账户信息。

        :param account_id: 账户的稳定标识
        :param variant: Minecraft 皮肤模型类型
        :param image: 需要上传或保存的图像数据
        """
        self._require_microsoft_account(account_id)
        normalized_variant = "slim" if str(variant or "").lower() == "slim" else "classic"
        await self.microsoft_manager.upload_skin(account_id, normalized_variant, image)
        return await self.refresh_account(account_id)

    async def reset_skin(self, account_id: Any) -> dict[str, Any]:
        """
        将正版账户皮肤重置为默认。

        :param account_id: 账户的稳定标识
        """
        self._require_microsoft_account(account_id)
        await self.microsoft_manager.reset_skin(account_id)
        return await self.refresh_account(account_id)

    async def set_cape(self, account_id: Any, cape_id: Any) -> dict[str, Any]:
        """
        为正版账户选择已解锁的披风。

        :param account_id: 账户的稳定标识
        :param cape_id: 需要启用的披风标识
        """
        self._require_microsoft_account(account_id)
        if not isinstance(cape_id, str) or not cape_id.strip():
            raise AccountError("披风 ID 不能为空", "INVALID_CAPE_ID")
        await self.microsoft_manager.set_cape(account_id, cape_id.strip())
        return await self.refresh_account(account_id)

    async def reset_cape(self, account_id: Any) -> dict[str, Any]:
        """
        取消正版账户当前佩戴的披风。

        :param account_id: 账户的稳定标识
        """
        self._require_microsoft_account(account_id)
        await self.microsoft_manager.reset_cape(account_id)
        return await self.refresh_account(account_id)

    def _on_device_code(self, flow: dict[str, Any]) -> None:
        with self._lock:
            self._login_flow = flow
            if self._login_cancel_event.is_set():
                flow["expires_at"] = 0
                self._login_state = {"status": "cancelled"}
                self._login_event.set()
                return
            flow["interval"] = self.microsoft_login_poll_interval_seconds
            self._login_state = {
                "status": "pending",
                "userCode": flow.get("user_code", ""),
                "verificationUri": flow.get("verification_uri", ""),
                "message": flow.get("message", ""),
                "interval": self.microsoft_login_poll_interval_seconds,
            }
            self._login_event.set()

    def _on_microsoft_progress(self, stage: str) -> None:
        self.logger.debug("Microsoft 登录阶段已更新: %s", stage)
        with self._lock:
            self._login_state = {"status": "progress", "stage": stage}
        self.events.emit(
            "accounts:microsoft_login_status",
            {
                "status": "progress",
                "stage": stage,
                "focus": stage == "authorization_confirmed",
            },
        )

    async def _run_microsoft_login(self) -> None:
        frontend_event: dict[str, Any] | None = None
        try:
            account_id = await self.microsoft_manager.add_microsoft_account()
            with self._lock:
                account_id = self._deduplicate_microsoft_accounts(preferred_id=account_id) or account_id
                self._current_account_id = account_id
                self._save_state()
                self._login_state = {"status": "ready", "account_id": account_id}
                frontend_event = {"status": "ready"}
        except asyncio.CancelledError:
            with self._lock:
                self._login_state = {"status": "cancelled"}
            raise
        except Exception as exc:
            with self._lock:
                if self._login_cancel_event.is_set():
                    self.logger.info("Microsoft 登录已取消")
                    self._login_state = {"status": "cancelled"}
                else:
                    self.logger.exception("Microsoft 登录失败")
                    self._login_state = {"status": "error", "message": str(exc)}
                    frontend_event = {"status": "error", "message": str(exc)}
        finally:
            with self._lock:
                self._login_flow = None
                self._login_event.set()
        if frontend_event is not None:
            self.events.emit("accounts:microsoft_login_status", frontend_event)

    async def start_microsoft_login(self) -> dict[str, Any]:
        """
        开始微软登录。
        """
        if not self._microsoft_login_available:
            raise AccountError(
                "需要配置 MICROSOFT_CLIENT_ID 后才能使用正版登录",
                "MICROSOFT_CLIENT_ID_REQUIRED",
            )
        with self._lock:
            task = self._login_task
            if task is not None and not task.done():
                if self._login_cancel_event.is_set():
                    raise AccountError("Microsoft 登录正在取消，请稍后重试", "MICROSOFT_LOGIN_CANCELLING")
                state = deepcopy(self._login_state)
            elif self._login_state.get("status") == "ready":
                self._login_state = {"status": "idle"}
                self._login_task = None
                return {"status": "completed"}
            else:
                self._login_state = {"status": "starting"}
                self._login_flow = None
                self._login_cancel_event = asyncio.Event()
                self._login_event = asyncio.Event()
                self._login_task = asyncio.get_running_loop().create_task(self._run_microsoft_login())
                self.logger.debug("Microsoft 设备码登录任务已启动")
                state = deepcopy(self._login_state)

        if state.get("status") == "starting":
            with suppress(TimeoutError):
                await asyncio.wait_for(self._login_event.wait(), timeout=self.login_start_wait_seconds)
            with self._lock:
                state = deepcopy(self._login_state)

        if state.get("status") == "error":
            raise AccountError(state.get("message") or "Microsoft 登录启动失败", "MICROSOFT_LOGIN_FAILED")
        if state.get("status") == "cancelled":
            raise AccountError("Microsoft 登录已取消", "MICROSOFT_LOGIN_CANCELLED")
        if state.get("status") == "starting":
            self.cancel_microsoft_login()
            raise AccountError("获取 Microsoft 设备代码超时，请重试", "MICROSOFT_LOGIN_TIMEOUT")
        if state.get("status") == "ready":
            return {"status": "completed"}
        if state.get("status") == "progress":
            return {"status": "progress", "stage": state.get("stage")}
        return {
            "status": "pending",
            "userCode": state.get("userCode", ""),
            "verificationUri": state.get("verificationUri", ""),
            "message": state.get("message", ""),
            "interval": state.get("interval", self.default_login_poll_interval_seconds),
        }

    def poll_microsoft_login(self) -> dict[str, Any]:
        """
        获取微软登录状态。
        """
        with self._lock:
            state = deepcopy(self._login_state)
        status = state.get("status")
        if status == "ready":
            return {"status": "ready"}
        if status in self.login_waiting_statuses:
            return _login_progress_result(status, state)
        message = state.get("message") or self.login_status_messages.get(status, "当前没有进行中的 Microsoft 登录")
        return {"status": "error", "message": message}

    def complete_microsoft_login(self) -> dict[str, Any]:
        """
        完成已授权的微软登录并返回保存后的账户。
        """
        with self._lock:
            state = deepcopy(self._login_state)
            status = state.get("status")
            if status == "error":
                raise AccountError(state.get("message") or "Microsoft 登录失败", "MICROSOFT_LOGIN_FAILED")
            if status == "cancelled":
                raise AccountError("Microsoft 登录已取消", "MICROSOFT_LOGIN_CANCELLED")
            if status in self.login_waiting_statuses:
                return _login_progress_result(status, state, force_pending=True)
            if status != "ready":
                raise AccountError("当前没有可完成的 Microsoft 登录", "MICROSOFT_LOGIN_NOT_STARTED")

            account_id = state["account_id"]
            info = self.microsoft_manager.get_microsoft_accounts().get(account_id)
            if info is None:
                raise AccountError("Microsoft 账号数据不存在", "ACCOUNT_NOT_FOUND")
            account = self._microsoft_account(account_id, info)
            self._login_state = {"status": "idle"}
            self._login_task = None
        self._emit_changed()
        return {"status": "completed", "account": account}

    def cancel_microsoft_login(self) -> bool:
        """
        取消登录流程；返回本次调用是否实际取消了任务。
        """
        with self._lock:
            task = self._login_task
            is_active = bool(task is not None and not task.done())
            if not is_active and self._login_state.get("status") not in {"starting", "pending"}:
                return False

            self._login_cancel_event.set()
            if self._login_flow is not None:
                self._login_flow["expires_at"] = 0
            self._login_state = {"status": "cancelled"}
            self._login_event.set()
            if task is not None and not task.done():
                task.cancel()
        self.events.emit("accounts:microsoft_login_status", {"status": "cancelled"})
        return True

    def _emit_changed(self) -> None:
        self.events.emit("accounts:changed", self.list_accounts())

    async def close(self) -> None:
        """
        取消仍在运行的认证任务并释放认证客户端。
        """
        cancelled = self.cancel_microsoft_login()
        self.logger.debug("正在关闭账户服务：已请求取消登录：%s", ("是" if cancelled else "否"))
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._login_task
            self._login_task = None
        await self.microsoft_manager.aclose()
        self.authlib_manager.close()
