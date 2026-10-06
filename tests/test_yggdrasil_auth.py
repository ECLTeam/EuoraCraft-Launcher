# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 yggdrasil_auth 模块的自动化测试。
#
# 公开接口：
#   - class OfflineClient
#       - validate(*args, **kwargs) -> Any
#       - close() -> Any
#   - class LoginClient
#       - follow_ali(url) -> Any
#       - auth(url, username, password, follow_ali, client_token) -> Any
#       - refresh(url, access_token, client_token, follow_ali, selected_profile) -> Any
#       - close() -> Any
#   - test_authlib_manager_uses_game_default_account_path(monkeypatch, tmp_path) -> None
#   - test_yggdrasil_refresh_can_bind_a_selected_profile() -> None
#   - test_authlib_login_resolves_and_saves_full_server_url_and_email(tmp_path) -> None
#   - test_authlib_login_uses_default_profile_from_access_token_when_top_level_field_is_missing(tmp_path) -> None
#   - test_authlib_login_matches_profile_name_when_server_does_not_select_one(tmp_path) -> None
#   - test_authlib_login_selects_one_profile_when_email_has_multiple_unselected_profiles(tmp_path) -> None
#   - test_authlib_relogin_allows_profile_choice_and_marks_logged_in_profiles(tmp_path) -> None
#   - test_authlib_injector_downloads_verified_artifact_once(tmp_path) -> None
#   - test_saved_authlib_account_loads_without_network_request(tmp_path) -> None
#   - test_refresh_saves_selected_profile_and_user_without_available_profiles(tmp_path) -> None
#   - test_get_texture_urls_parses_slim_model_from_metadata(tmp_path) -> None
#   - test_get_texture_urls_defaults_classic_when_model_missing(tmp_path) -> None
# ============================================================

import json
from base64 import b64encode, urlsafe_b64encode
from hashlib import sha256
from pathlib import Path

import httpx

from ECL.game import YggdrasilClient
from ECL.services.account.authlib import AuthlibAccountManager, AuthlibInjector


class OfflineClient:
    def validate(self, *args, **kwargs):
        raise AssertionError("加载账户时不应验证令牌")

    def close(self):
        pass


class LoginClient:
    def __init__(self):
        self.request = None

    def follow_ali(self, url):
        assert url == "skin.example.com"
        return "https://skin.example.com/api/yggdrasil"

    def auth(self, url, username, password, follow_ali, client_token):
        self.request = (url, username, password, follow_ali, client_token)
        return {
            "accessToken": "access-token",
            "clientToken": client_token,
            "availableProfiles": [
                {"id": "other-profile", "name": "OtherPlayer"},
                {"id": "profile-id", "name": "Player"},
            ],
            "selectedProfile": {"id": "profile-id", "name": "Player"},
            "user": {"id": "user-id", "properties": []},
        }

    def refresh(self, url, access_token, client_token, follow_ali, selected_profile):
        return {
            "accessToken": "refreshed-access-token",
            "clientToken": client_token,
            "selectedProfile": selected_profile,
            "user": {"id": "user-id", "properties": []},
        }

    def close(self):
        pass


def test_authlib_manager_uses_game_default_account_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    manager = AuthlibAccountManager(
        client=OfflineClient(),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    assert manager.account_path == tmp_path / ".ECL" / "accounts"
    manager.close()


def test_yggdrasil_refresh_can_bind_a_selected_profile() -> None:
    captured = {}

    def handle_refresh(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, request=request, json={"accessToken": "new", "clientToken": "client"})

    client = YggdrasilClient()
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handle_refresh))

    client.refresh(
        "https://skin.example.com/api/yggdrasil",
        "access",
        "client",
        follow_ali=False,
        selected_profile={"id": "profile-two", "name": "PlayerTwo"},
    )

    assert captured == {
        "accessToken": "access",
        "clientToken": "client",
        "requestUser": True,
        "selectedProfile": {"id": "profile-two", "name": "PlayerTwo"},
    }
    client.close()


def test_authlib_login_resolves_and_saves_full_server_url_and_email(tmp_path) -> None:
    client = LoginClient()
    manager = AuthlibAccountManager(
        tmp_path,
        client,
        httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    account_id, account = manager.add_account("skin.example.com", "player@example.com", "secret")

    assert account["YggdrasilAPI"] == "https://skin.example.com/api/yggdrasil"
    assert account["Username"] == "player@example.com"
    assert client.request[:4] == (
        "https://skin.example.com/api/yggdrasil",
        "player@example.com",
        "secret",
        False,
    )
    saved = json.loads((tmp_path / "accounts" / "yggdrasil_accounts_list.json").read_text(encoding="utf-8"))
    assert saved[account_id]["YggdrasilAPI"] == "https://skin.example.com/api/yggdrasil"
    assert saved[account_id]["Username"] == "player@example.com"
    assert saved[account_id]["Profiles"] == {
        "selectedProfile": {"id": "profile-id", "name": "Player"},
        "user": {"id": "user-id", "properties": []},
    }
    manager.close()


def test_authlib_login_uses_default_profile_from_access_token_when_top_level_field_is_missing(tmp_path) -> None:
    class TokenProfileClient(LoginClient):
        def auth(self, url, username, password, follow_ali, client_token):
            response = super().auth(url, username, password, follow_ali, client_token)
            response.pop("selectedProfile")
            payload = urlsafe_b64encode(json.dumps({"selectedProfile": "profile-id"}).encode()).decode().rstrip("=")
            response["accessToken"] = f"header.{payload}.signature"
            return response

    manager = AuthlibAccountManager(
        tmp_path,
        TokenProfileClient(),
        httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    _, account = manager.add_account("skin.example.com", "player@example.com", "secret")

    assert account["Profiles"]["selectedProfile"] == {"id": "profile-id", "name": "Player"}
    assert "availableProfiles" not in account["Profiles"]
    manager.close()


def test_authlib_login_matches_profile_name_when_server_does_not_select_one(tmp_path) -> None:
    class ProfileNameClient(LoginClient):
        def auth(self, url, username, password, follow_ali, client_token):
            response = super().auth(url, username, password, follow_ali, client_token)
            response.pop("selectedProfile")
            response["accessToken"] = "opaque-access-token"
            return response

    manager = AuthlibAccountManager(
        tmp_path,
        ProfileNameClient(),
        httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    _, account = manager.add_account("skin.example.com", "player", "secret")

    assert account["Profiles"]["selectedProfile"] == {"id": "profile-id", "name": "Player"}
    assert manager.tokens[next(iter(manager.tokens))]["AccessToken"] == "refreshed-access-token"
    manager.close()


def test_authlib_login_selects_one_profile_when_email_has_multiple_unselected_profiles(tmp_path) -> None:
    class MultiProfileClient(LoginClient):
        def __init__(self):
            super().__init__()
            self.refresh_request = None

        def auth(self, url, username, password, follow_ali, client_token):
            response = super().auth(url, username, password, follow_ali, client_token)
            response.pop("selectedProfile")
            response["accessToken"] = "opaque-access-token"
            return response

        def refresh(self, url, access_token, client_token, follow_ali, selected_profile):
            self.refresh_request = (url, access_token, client_token, follow_ali, selected_profile)
            return {
                "accessToken": "selected-access-token",
                "clientToken": client_token,
                "selectedProfile": selected_profile,
                "user": {"id": "user-id"},
            }

    client = MultiProfileClient()
    manager = AuthlibAccountManager(
        tmp_path,
        client,
        httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    account_id, pending = manager.add_account("skin.example.com", "player@example.com", "secret")

    assert pending["Profiles"] == {
        "availableProfiles": [
            {"id": "other-profile", "name": "OtherPlayer", "logged_in": False},
            {"id": "profile-id", "name": "Player", "logged_in": False},
        ]
    }
    assert manager.list_accounts() == {}

    selected_account_id, account = manager.select_profile(account_id, "other-profile")

    assert selected_account_id == account_id
    assert account["Profiles"] == {
        "selectedProfile": {"id": "other-profile", "name": "OtherPlayer"},
        "user": {"id": "user-id"},
    }
    assert client.refresh_request == (
        "https://skin.example.com/api/yggdrasil",
        "opaque-access-token",
        account_id,
        False,
        {"id": "other-profile", "name": "OtherPlayer"},
    )
    assert "availableProfiles" not in account["Profiles"]

    manager.close()


def test_authlib_relogin_allows_profile_choice_and_marks_logged_in_profiles(tmp_path) -> None:
    class RememberingClient(LoginClient):
        def __init__(self):
            super().__init__()
            self.login_count = 0
            self.selected_profile = None

        def auth(self, url, username, password, follow_ali, client_token):
            self.login_count += 1
            response = super().auth(url, username, password, follow_ali, client_token)
            response["selectedProfile"] = {"id": "other-profile", "name": "OtherPlayer"}
            if self.login_count > 1:
                response.pop("selectedProfile")
                response["accessToken"] = "opaque-access-token"
            return response

        def refresh(self, url, access_token, client_token, follow_ali, selected_profile):
            self.selected_profile = selected_profile
            return {
                "accessToken": "refreshed-access-token",
                "clientToken": client_token,
                "selectedProfile": selected_profile,
                "user": {"id": "user-id"},
            }

    client = RememberingClient()
    manager = AuthlibAccountManager(
        tmp_path,
        client,
        httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )
    first_id, _ = manager.add_account("skin.example.com", "player@example.com", "secret")

    pending_id, pending = manager.add_account("skin.example.com", "player@example.com", "secret")

    assert pending_id != first_id
    assert pending["Profiles"]["availableProfiles"] == [
        {"id": "other-profile", "name": "OtherPlayer", "logged_in": True},
        {"id": "profile-id", "name": "Player", "logged_in": False},
    ]

    second_id, second_account = manager.select_profile(pending_id, "profile-id")

    assert second_id != first_id
    assert second_account["Profiles"]["selectedProfile"] == {"id": "profile-id", "name": "Player"}
    assert len(manager.list_accounts()) == 2

    third_pending_id, third_pending = manager.add_account("skin.example.com", "player@example.com", "secret")
    assert all(profile["logged_in"] for profile in third_pending["Profiles"]["availableProfiles"])

    selected_existing_id, _ = manager.select_profile(third_pending_id, "other-profile")

    assert selected_existing_id == first_id
    assert len(manager.list_accounts()) == 2
    manager.close()


def test_authlib_injector_downloads_verified_artifact_once(tmp_path) -> None:
    jar_data = b"authlib-injector"
    checksum = sha256(jar_data).hexdigest()
    requests = []

    def download(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == AuthlibInjector.metadata_url:
            return httpx.Response(
                200,
                request=request,
                json={
                    "download_url": "https://download.example.com/authlib-injector.jar",
                    "checksums": {"sha256": checksum},
                },
            )
        return httpx.Response(200, request=request, content=jar_data)

    injector = AuthlibInjector(
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(download)),
    )

    first_path = injector.ensure()
    second_path = injector.ensure()

    assert first_path == second_path
    assert first_path.read_bytes() == jar_data
    assert requests == [
        AuthlibInjector.metadata_url,
        "https://download.example.com/authlib-injector.jar",
    ]
    injector.close()


def test_saved_authlib_account_loads_without_network_request(tmp_path) -> None:
    account_id = "saved-account"
    account_dir = tmp_path / "accounts"
    token_dir = account_dir / "yggdrasil_accounts"
    token_dir.mkdir(parents=True)
    account_info = {
        account_id: {
            "AccountId": account_id,
            "YggdrasilAPI": "https://skin.example.com/api/yggdrasil",
            "Profiles": {
                "selectedProfile": {
                    "id": "1cb5a9d2f3454fe5bf576e33138a2992",
                    "name": "Player",
                }
            },
        }
    }
    token_info = {"AccessToken": "access-token", "ClientToken": account_id}
    account_list_path = account_dir / "yggdrasil_accounts_list.json"
    account_list_path.write_text(json.dumps(account_info), encoding="utf-8")
    (token_dir / f"{account_id}.json").write_text(json.dumps(token_info), encoding="utf-8")
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: None))

    manager = AuthlibAccountManager(tmp_path, OfflineClient(), http_client)

    assert manager.list_accounts() == account_info
    assert json.loads(account_list_path.read_text(encoding="utf-8")) == account_info
    manager.close()


def test_refresh_saves_selected_profile_and_user_without_available_profiles(tmp_path) -> None:
    class RefreshClient:
        def refresh(self, url, access_token, client_token, follow_ali):
            assert (url, access_token, client_token, follow_ali) == (
                "https://skin.example.com/api/yggdrasil",
                "access-token",
                "saved-account",
                False,
            )
            return {
                "accessToken": "refreshed-access-token",
                "clientToken": "saved-account",
                "selectedProfile": {"id": "profile-two", "name": "PlayerTwo"},
                "user": {"id": "user-id", "properties": []},
            }

        def close(self):
            pass

    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: None))
    manager = AuthlibAccountManager(tmp_path, RefreshClient(), http_client)
    manager.accounts["saved-account"] = {
        "AccountId": "saved-account",
        "YggdrasilAPI": "https://skin.example.com/api/yggdrasil",
        "Username": "player@example.com",
        "Profiles": {
            "selectedProfile": {"id": "profile-two", "name": "PlayerTwo"},
            "availableProfiles": [
                {"id": "profile-one", "name": "PlayerOne"},
                {"id": "profile-two", "name": "PlayerTwo"},
            ],
        },
    }
    manager.tokens["saved-account"] = {
        "AccessToken": "access-token",
        "ClientToken": "saved-account",
    }

    account = manager.refresh_account("saved-account")

    assert account["Profiles"] == {
        "selectedProfile": {"id": "profile-two", "name": "PlayerTwo"},
        "user": {"id": "user-id", "properties": []},
    }
    assert account["Username"] == "player@example.com"
    saved_accounts = json.loads((tmp_path / "accounts" / "yggdrasil_accounts_list.json").read_text(encoding="utf-8"))
    assert "availableProfiles" not in saved_accounts["saved-account"]["Profiles"]
    manager.close()


def _texture_value(skin_url: str, model: str | None = None) -> str:
    skin = {"url": skin_url}
    if model:
        skin["metadata"] = {"model": model}
    payload = {
        "timestamp": 0,
        "profileId": "profile-id",
        "profileName": "Player",
        "textures": {"SKIN": skin},
    }
    return b64encode(json.dumps(payload).encode()).decode()


def _manager_with_profile(tmp_path, value: str) -> AuthlibAccountManager:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://skin.example.com/api/yggdrasil/sessionserver/session/minecraft/profile/profile-id?unsigned=true"
        )
        return httpx.Response(
            200,
            request=request,
            json={"properties": [{"name": "textures", "value": value}]},
        )

    manager = AuthlibAccountManager(
        tmp_path,
        OfflineClient(),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    manager.accounts["account-id"] = {
        "AccountId": "account-id",
        "YggdrasilAPI": "https://skin.example.com/api/yggdrasil",
        "Profiles": {"selectedProfile": {"id": "profile-id", "name": "Player"}},
    }
    return manager


def test_get_texture_urls_parses_slim_model_from_metadata(tmp_path) -> None:
    manager = _manager_with_profile(
        tmp_path,
        _texture_value("https://textures.example.com/skin.png", "slim"),
    )

    assert manager.get_texture_urls("account-id") == {
        "skinUrl": "https://textures.example.com/skin.png",
        "skinModel": "slim",
    }
    manager.close()


def test_get_texture_urls_defaults_classic_when_model_missing(tmp_path) -> None:
    manager = _manager_with_profile(
        tmp_path,
        _texture_value("https://textures.example.com/skin.png"),
    )

    assert manager.get_texture_urls("account-id") == {
        "skinUrl": "https://textures.example.com/skin.png",
        "skinModel": "classic",
    }
    manager.close()
