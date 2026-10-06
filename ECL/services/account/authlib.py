# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：外置登录服务：authlib-injector 下载维护与外置账户管理。
#
# 公开接口：
#   - class AuthlibInjector — 管理 authlib-injector 组件的版本、下载与校验。
#       - needs_download() -> bool — 本地 jar 缺失或校验失配时需要重新下载。
#       - ensure() -> Path — 返回可用的 authlib-injector；本地缺失时下载最新版。
#       - close() -> None — 关闭下载客户端。
#   - class AuthlibAccountManager — 持久化并维护外置登录（Yggdrasil）账户与访问令牌。
#       - list_accounts() -> dict[str, dict] — 返回全部外置登录账户。
#       - resolve_server(server_url) -> str — 通过 ALI 返回实际的 Yggdrasil API 地址。
#       - add_account(server_url, username, password) -> tuple[str, dict] — 登录外置账户并保存令牌与角色资料。
#       - select_profile(account_id, profile_id) -> tuple[str, dict] — 为一次待完成的多角色登录绑定单个角色并保存账户。
#       - delete_account(account_id) -> None — 删除本地外置账户。
#       - refresh_account(account_id) -> dict — 刷新外置账户的令牌与角色资料。
#       - get_token(account_id) -> dict[str, str] — 返回可启动游戏的有效外置登录令牌。
#       - get_texture_urls(account_id) -> dict[str, str] — 从外置登录会话档案中解析完整皮肤与披风 URL。
#       - close() -> None — 关闭认证客户端。
# ============================================================

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from threading import RLock
from uuid import uuid4

import httpx

from ECL.game import YggdrasilClient
from ECL.utils import AuthlibError, AuthlibProfileSelectionRequired, atomic_write_bytes, atomic_write_text


class AuthlibInjector:
    """
    管理 authlib-injector 组件的版本、下载与校验。

    组件以 SHA-256 摘要核对缓存 jar；本地文件缺失或失配时才重新拉取最新
    元数据，保证启动前始终能返回一个可用组件。

    :param data_path: 启动器数据目录
    :param http_client: 可注入的 HTTP 客户端
    """

    metadata_url = "https://authlib-injector.yushi.moe/artifact/latest.json"

    def __init__(self, data_path: Path | str, http_client: httpx.Client | None = None) -> None:
        self.path = Path(data_path) / "dependencies" / "authlib-injector"
        self.path.mkdir(parents=True, exist_ok=True)
        self.jar_path = self.path / "authlib-injector.jar"
        self.checksum_path = self.path / "authlib-injector.sha256"
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(15, connect=10),
            follow_redirects=True,
            headers={"User-Agent": "EuoraCraft-Launcher"},
        )

    @staticmethod
    def _checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def needs_download(self) -> bool:
        """
        本地 jar 缺失或校验失配时需要重新下载。
        """
        if self.jar_path.is_file() and self.checksum_path.is_file():
            expected = self.checksum_path.read_text(encoding="ascii").strip()
            return self._checksum(self.jar_path.read_bytes()) != expected
        return True

    def ensure(self) -> Path:
        """
        返回可用的 authlib-injector；本地缺失时下载最新版。
        """
        if not self.needs_download():
            return self.jar_path

        metadata = self.http.get(self.metadata_url)
        metadata.raise_for_status()
        artifact = metadata.json()
        download_url = artifact["download_url"]
        expected = artifact["checksums"]["sha256"]
        response = self.http.get(download_url)
        response.raise_for_status()
        if self._checksum(response.content) != expected:
            raise AuthlibError("authlib-injector 文件校验失败")

        atomic_write_bytes(self.jar_path, response.content)
        atomic_write_text(self.checksum_path, expected, encoding="ascii")
        return self.jar_path

    def close(self) -> None:
        """
        关闭下载客户端。
        """
        self.http.close()


class AuthlibAccountManager:
    """
    持久化并维护外置登录（Yggdrasil）账户与访问令牌。

    账户元数据与令牌分别落盘于 ``accounts`` 目录，登录后的多角色选择以
    ``pending_accounts`` 暂存，直到用户绑定单个角色；令牌失效时自动刷新。
    本类只负责账户状态，不介入启动流程。

    :param data_path: 数据根目录；缺失时使用默认的 ``~/.ECL``
    :param client: 可注入的 Yggdrasil 客户端
    :param http_client: 可注入的 HTTP 客户端
    """

    def __init__(
        self,
        data_path: Path | str | None = None,
        client: YggdrasilClient | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        root_path = Path(data_path) if data_path is not None else Path.home() / ".ECL"
        self.account_path = root_path / "accounts"
        self.account_path.mkdir(parents=True, exist_ok=True)
        self.account_file = self.account_path / "yggdrasil_accounts_list.json"
        self.token_path = self.account_path / "yggdrasil_accounts"
        self.token_path.mkdir(parents=True, exist_ok=True)
        self.client = client or YggdrasilClient()
        # 启动器通道客户端固定不读代理环境变量，账户登录等请求不受下载代理影响
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(15, connect=10),
            follow_redirects=True,
            headers={"User-Agent": "EuoraCraft-Launcher"},
            trust_env=False,
        )
        self.accounts: dict[str, dict] = {}
        self.tokens: dict[str, dict[str, str]] = {}
        self.pending_accounts: dict[str, dict] = {}
        self._lock = RLock()
        self._load()

    def _load(self) -> None:
        if not self.account_file.is_file():
            return
        accounts = json.loads(self.account_file.read_text(encoding="utf-8"))
        if not isinstance(accounts, dict):
            raise AuthlibError("外置登录账户文件格式无效")
        for account_id, account in accounts.items():
            token_file = self.token_path / f"{account_id}.json"
            if not token_file.is_file():
                continue
            token = json.loads(token_file.read_text(encoding="utf-8"))
            self.accounts[account_id] = account
            self.tokens[account_id] = token

    def _save_accounts(self) -> None:
        atomic_write_text(self.account_file, json.dumps(self.accounts, ensure_ascii=False, indent=2))

    def _save_token(self, account_id: str) -> None:
        atomic_write_text(
            self.token_path / f"{account_id}.json",
            json.dumps(self.tokens[account_id], ensure_ascii=False, indent=2),
        )

    @staticmethod
    def _token_profile_id(response: dict) -> str | None:
        access_token = response.get("accessToken")
        if isinstance(access_token, str):
            try:
                payload_part = access_token.split(".", 2)[1]
                padding = "=" * (-len(payload_part) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_part + padding))
                token_profile_id = payload.get("selectedProfile")
                if isinstance(token_profile_id, str) and token_profile_id:
                    return token_profile_id
            except (IndexError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                pass
        return None

    @classmethod
    def _selected_profile(cls, response: dict, username: str | None = None) -> dict:
        # 返回皮肤站指定的默认角色，兼容只在访问令牌中声明角色的实现。
        selected_profile = response.get("selectedProfile")
        if isinstance(selected_profile, dict) and selected_profile.get("id") and selected_profile.get("name"):
            return deepcopy(selected_profile)

        available_profiles = [
            profile
            for profile in response.get("availableProfiles") or []
            if isinstance(profile, dict) and profile.get("id") and profile.get("name")
        ]
        selected_profile_id = cls._token_profile_id(response)

        if selected_profile_id:
            profile = next(
                (item for item in available_profiles if str(item.get("id")) == selected_profile_id),
                None,
            )
            if profile is not None:
                return deepcopy(profile)
        if isinstance(username, str) and username:
            normalized_username = username.casefold()
            profile = next(
                (item for item in available_profiles if str(item.get("name") or "").casefold() == normalized_username),
                None,
            )
            if profile is not None:
                return deepcopy(profile)
        if len(available_profiles) == 1:
            return deepcopy(available_profiles[0])
        if len(available_profiles) > 1:
            raise AuthlibProfileSelectionRequired(available_profiles)
        raise AuthlibError("该账户没有可用的游戏角色")

    def _existing_profile_accounts(self, server_url: str, username: str) -> dict[str, str]:
        normalized_username = username.casefold()
        result: dict[str, str] = {}
        for account_id, account in self.accounts.items():
            if str(account.get("YggdrasilAPI") or "").rstrip("/") != server_url.rstrip("/"):
                continue
            if str(account.get("Username") or "").casefold() != normalized_username:
                continue
            profile = (account.get("Profiles") or {}).get("selectedProfile")
            profile_id = str(profile.get("id") or "") if isinstance(profile, dict) else ""
            if profile_id:
                result[profile_id] = account_id
        return result

    def _refresh_with_profile(
        self,
        account_id: str,
        server_url: str,
        response: dict,
        profile: dict,
        username: str,
    ) -> dict:
        refreshed = self.client.refresh(
            server_url,
            response["accessToken"],
            response["clientToken"],
            follow_ali=False,
            selected_profile=profile,
        )
        return self._store_response(account_id, server_url, refreshed, username)

    @staticmethod
    def _pending_info(
        account_id: str,
        server_url: str,
        username: str,
        profiles: list[dict],
        existing_accounts: dict[str, str],
    ) -> dict:
        choices = []
        for profile in profiles:
            choice = deepcopy(profile)
            choice["logged_in"] = str(profile.get("id") or "") in existing_accounts
            choices.append(choice)
        return {
            "AccountId": account_id,
            "YggdrasilAPI": server_url,
            "Username": username,
            "Profiles": {"availableProfiles": choices},
        }

    def _create_pending_login(
        self,
        account_id: str,
        server_url: str,
        username: str,
        password: str,
        response: dict,
        profiles: list[dict],
        existing_accounts: dict[str, str],
    ) -> tuple[str, dict]:
        with self._lock:
            self.pending_accounts[account_id] = {
                "YggdrasilAPI": server_url,
                "Username": username,
                "Password": password,
                "Response": deepcopy(response),
                "Profiles": deepcopy(profiles),
                "ExistingAccounts": deepcopy(existing_accounts),
            }
        return account_id, self._pending_info(account_id, server_url, username, profiles, existing_accounts)

    def _read_account_token(self, account_id: str) -> tuple[dict, dict]:
        """
        在锁内读取账户与令牌快照。

        :param account_id: 账户稳定标识
        :returns: 账户与令牌数据；账户不存在时抛出 KeyError
        """
        with self._lock:
            account = self.accounts.get(account_id)
            token = self.tokens.get(account_id)
        if account is None or token is None:
            raise KeyError(f"账户 '{account_id}' 不存在")
        return account, token

    def _store_response(
        self,
        account_id: str,
        server_url: str,
        response: dict,
        username: str | None = None,
    ) -> dict:
        profiles = {"selectedProfile": self._selected_profile(response, username)}
        user = response.get("user")
        if isinstance(user, dict):
            profiles["user"] = deepcopy(user)

        account = {
            "AccountId": account_id,
            "YggdrasilAPI": server_url,
            "Profiles": profiles,
        }
        # 网络请求在锁外完成后才进入本方法，这里只短暂持锁完成状态写入与持久化
        with self._lock:
            self.tokens[account_id] = {
                "AccessToken": response["accessToken"],
                "ClientToken": response["clientToken"],
            }
            saved_username = username or (self.accounts.get(account_id) or {}).get("Username")
            if saved_username:
                account["Username"] = saved_username
            self.accounts[account_id] = account
            self._save_token(account_id)
            self._save_accounts()
        return deepcopy(account)

    def list_accounts(self) -> dict[str, dict]:
        """
        返回全部外置登录账户。
        """
        with self._lock:
            return deepcopy(self.accounts)

    def resolve_server(self, server_url: str) -> str:
        """
        通过 ALI 返回实际的 Yggdrasil API 地址。

        :param server_url: Authlib 认证服务器地址
        """
        return self.client.follow_ali(server_url).rstrip("/")

    def add_account(self, server_url: str, username: str, password: str) -> tuple[str, dict]:
        """
        登录外置账户并保存令牌与角色资料。

        ALI 解析与认证请求在锁外执行，仅状态写入短暂持锁，
        避免登录耗时阻塞账户列表等 IPC 读取。

        :param server_url: Authlib 认证服务器地址
        :param username: 登录或离线账户用户名
        :param password: 仅用于本次认证且不会写入日志的密码
        """
        root_url = self.resolve_server(server_url)
        account_id = uuid4().hex
        with self._lock:
            existing_accounts = self._existing_profile_accounts(root_url, username)
        response = self.client.auth(
            root_url,
            username,
            password,
            follow_ali=False,
            client_token=account_id,
        )
        available_profiles = [
            profile
            for profile in response.get("availableProfiles") or []
            if isinstance(profile, dict) and profile.get("id") and profile.get("name")
        ]
        if existing_accounts and len(available_profiles) > 1:
            return self._create_pending_login(
                account_id,
                root_url,
                username,
                password,
                response,
                available_profiles,
                existing_accounts,
            )
        try:
            selected_profile = self._selected_profile(response, username)
        except AuthlibProfileSelectionRequired as exc:
            return self._create_pending_login(
                account_id,
                root_url,
                username,
                password,
                response,
                exc.profiles,
                existing_accounts,
            )

        response_profile = response.get("selectedProfile")
        response_selected = isinstance(response_profile, dict) and response_profile.get("id") == selected_profile.get(
            "id"
        )
        token_selected = self._token_profile_id(response) == str(selected_profile.get("id") or "")
        target_account_id = existing_accounts.get(str(selected_profile.get("id") or ""), account_id)
        if response_selected or token_selected:
            return target_account_id, self._store_response(target_account_id, root_url, response, username)
        return target_account_id, self._refresh_with_profile(
            target_account_id,
            root_url,
            response,
            selected_profile,
            username,
        )

    def select_profile(self, account_id: str, profile_id: str) -> tuple[str, dict]:
        """
        为一次待完成的多角色登录绑定单个角色并保存账户。

        快照待选登录数据后即在锁外执行认证请求，仅状态写入短暂持锁。

        :param account_id: 账户的稳定标识
        :param profile_id: Authlib 玩家档案标识
        """
        with self._lock:
            pending = deepcopy(self.pending_accounts.get(account_id))
        if pending is None:
            raise KeyError(f"待选择角色的账户 '{account_id}' 不存在")
        profile = next(
            (item for item in pending["Profiles"] if str(item.get("id") or "") == profile_id),
            None,
        )
        if profile is None:
            raise AuthlibError("所选角色不属于本次登录账户")
        response = pending["Response"]
        target_account_id = pending["ExistingAccounts"].get(profile_id, account_id)
        bound_profile = response.get("selectedProfile")
        bound_profile_id = (
            str(bound_profile.get("id") or "") if isinstance(bound_profile, dict) else self._token_profile_id(response)
        )
        if bound_profile_id == profile_id:
            account = self._store_response(
                target_account_id,
                pending["YggdrasilAPI"],
                response,
                pending["Username"],
            )
        elif bound_profile_id:
            selected_response = self.client.auth(
                pending["YggdrasilAPI"],
                profile["name"],
                pending["Password"],
                follow_ali=False,
                client_token=response["clientToken"],
            )
            selected = self._selected_profile(selected_response, profile["name"])
            if str(selected.get("id") or "") != profile_id:
                raise AuthlibError("认证服务器没有绑定所选角色")
            if not (
                isinstance(selected_response.get("selectedProfile"), dict)
                or self._token_profile_id(selected_response) == profile_id
            ):
                account = self._refresh_with_profile(
                    target_account_id,
                    pending["YggdrasilAPI"],
                    selected_response,
                    profile,
                    pending["Username"],
                )
            else:
                account = self._store_response(
                    target_account_id,
                    pending["YggdrasilAPI"],
                    selected_response,
                    pending["Username"],
                )
        else:
            account = self._refresh_with_profile(
                target_account_id,
                pending["YggdrasilAPI"],
                response,
                profile,
                pending["Username"],
            )
        with self._lock:
            self.pending_accounts.pop(account_id, None)
        return target_account_id, account

    def delete_account(self, account_id: str) -> None:
        """
        删除本地外置账户。

        :param account_id: 账户的稳定标识
        """
        with self._lock:
            if account_id not in self.accounts:
                raise KeyError(f"账户 '{account_id}' 不存在")
            self.accounts.pop(account_id)
            self.tokens.pop(account_id)
            (self.token_path / f"{account_id}.json").unlink(missing_ok=True)
            self._save_accounts()

    def refresh_account(self, account_id: str) -> dict:
        """
        刷新外置账户的令牌与角色资料。

        刷新请求在锁外执行，仅状态写入短暂持锁。

        :param account_id: 账户的稳定标识
        """
        account, token = self._read_account_token(account_id)
        response = self.client.refresh(
            account["YggdrasilAPI"],
            token["AccessToken"],
            token["ClientToken"],
            follow_ali=False,
        )
        return self._store_response(account_id, account["YggdrasilAPI"], response)

    def get_token(self, account_id: str) -> dict[str, str]:
        """
        返回可启动游戏的有效外置登录令牌。

        validate 与刷新均为网络请求，在锁外执行避免阻塞账户列表等 IPC 读取。

        :param account_id: 账户的稳定标识
        """
        account, token = self._read_account_token(account_id)
        if not (account.get("Profiles") or {}).get("selectedProfile"):
            raise AuthlibError("外置登录账户缺少默认角色")
        if not self.client.validate(
            account["YggdrasilAPI"],
            token["AccessToken"],
            token["ClientToken"],
            follow_ali=False,
        ):
            self.refresh_account(account_id)
            account, token = self._read_account_token(account_id)
        return {
            "AccessToken": token["AccessToken"],
            "ClientToken": token["ClientToken"],
            "YggdrasilAPI": account["YggdrasilAPI"],
        }

    def get_texture_urls(self, account_id: str) -> dict[str, str]:
        """
        从外置登录会话档案中解析完整皮肤与披风 URL。

        后端只读取签名后的材质元数据，不下载或裁切图片，前端可据此完成统一渲染。

        :param account_id: 账户的稳定标识
        :return: 可用的 ``skinUrl``、``skinModel`` 与 ``capeUrl``；没有材质时返回空字典
        """
        with self._lock:
            account = self.accounts.get(account_id)
            if account is None:
                raise KeyError(f"账户 '{account_id}' 不存在")
            profile = (account.get("Profiles") or {}).get("selectedProfile") or {}
            if not profile:
                return {}
            server_url = account["YggdrasilAPI"].rstrip("/")
            profile_id = profile.get("id") or ""

        response = self.http.get(
            f"{server_url}/sessionserver/session/minecraft/profile/{profile_id}",
            params={"unsigned": "true"},
        )
        if response.status_code == 204:
            return {}
        response.raise_for_status()
        texture = next(
            (
                item.get("value")
                for item in response.json().get("properties", [])
                if item.get("name") == "textures" and isinstance(item.get("value"), str)
            ),
            None,
        )
        if texture is None:
            return {}
        texture_data = json.loads(base64.b64decode(texture))
        textures = texture_data.get("textures") or {}
        result: dict[str, str] = {}
        skin = textures.get("SKIN") or {}
        skin_url = skin.get("url")
        cape_url = (textures.get("CAPE") or {}).get("url")
        if isinstance(skin_url, str) and skin_url:
            result["skinUrl"] = skin_url
            # 皮肤站通过材质元数据的 metadata.model 声明纤细手臂，缺失时按经典手臂处理
            model = (skin.get("metadata") or {}).get("model")
            result["skinModel"] = "slim" if str(model or "").lower() == "slim" else "classic"
        if isinstance(cape_url, str) and cape_url:
            result["capeUrl"] = cape_url
        return result

    def close(self) -> None:
        """
        关闭认证客户端。
        """
        self.http.close()
        self.client.close()
