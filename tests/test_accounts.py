# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 accounts 模块的自动化测试。
#
# 公开接口：
#   - class FakeMicrosoftManager
#       - get_microsoft_accounts() -> dict[str, dict[str, Any]]
#       - add_microsoft_account() -> str
#       - del_microsoft_account(account_id) -> None
#       - refresh_profile(account_id) -> dict[str, Any]
#       - get_minecraft_token(account_id) -> str
#       - upload_skin(account_id, variant, image) -> dict[str, Any]
#       - reset_skin(account_id) -> dict[str, Any]
#       - set_cape(account_id, cape_id) -> dict[str, Any]
#       - reset_cape(account_id) -> dict[str, Any]
#       - aclose() -> None
#   - class FakeAuthlibManager
#       - list_accounts() -> dict[str, dict[str, Any]]
#       - resolve_server(url) -> str
#       - add_account(url, username, password) -> tuple[str, dict[str, Any]]
#       - delete_account(account_id) -> None
#       - refresh_account(account_id) -> dict[str, Any]
#       - select_profile(account_id, profile_id) -> tuple[str, dict[str, Any]]
#       - get_token(account_id) -> dict[str, str]
#       - get_texture_urls(account_id) -> dict[str, str]
#       - close() -> None
#   - class BlockingMicrosoftManager
#       - add_microsoft_account() -> str
#   - class DuplicateMicrosoftManager
#       - add_microsoft_account() -> str
#   - test_offline_accounts_persist_and_emit_changes(tmp_path) -> None
#   - test_offline_account_accepts_custom_uuid(tmp_path) -> None
#   - test_offline_account_exposes_launch_credentials(tmp_path) -> None
#   - test_offline_account_rejects_invalid_custom_uuid(tmp_path) -> None
#   - test_offline_default_skins_load_from_resource_path(tmp_path) -> None
#   - test_offline_add_with_skin_exposes_skin_url_in_list(tmp_path) -> None
#   - test_offline_texture_urls_uses_selected_skin(tmp_path) -> None
#   - test_offline_set_skin_updates_and_persists(tmp_path) -> None
#   - test_offline_invalid_skin_is_rejected(tmp_path) -> None
#   - test_offline_without_skin_has_no_skin_url(tmp_path) -> None
#   - test_authlib_account_can_login_launch_refresh_and_remove(tmp_path) -> None
#   - test_authlib_multi_profile_login_only_becomes_current_after_one_profile_is_selected(tmp_path) -> None
#   - test_authlib_server_uses_ali_resolution(tmp_path) -> None
#   - test_authlib_login_uses_server_error_message(tmp_path, monkeypatch) -> None
#   - test_account_manager_uses_runtime_microsoft_client_id(tmp_path, monkeypatch) -> None
#   - test_microsoft_login_requires_configured_client_id(tmp_path, monkeypatch) -> None
#   - test_microsoft_device_login_flow(tmp_path) -> None
#   - test_launcher_microsoft_login_reports_each_stage(tmp_path, monkeypatch) -> None
#   - test_repeated_microsoft_login_replaces_existing_account(tmp_path) -> None
#   - test_existing_microsoft_duplicates_are_merged_on_startup(tmp_path) -> None
#   - test_remove_and_switch_accounts(tmp_path) -> None
#   - test_complete_microsoft_login_keeps_pending_state(tmp_path) -> None
#   - test_cancel_microsoft_login_stops_device_flow(tmp_path) -> None
#   - test_close_cancels_login_and_releases_manager(tmp_path) -> None
#   - test_microsoft_account_includes_capes(tmp_path) -> None
#   - test_upload_skin_refreshes_account(tmp_path) -> None
#   - test_upload_skin_normalizes_variant(tmp_path) -> None
#   - test_reset_skin(tmp_path) -> None
#   - test_set_and_reset_cape(tmp_path) -> None
#   - test_skin_operations_require_microsoft_account(tmp_path) -> None
#   - test_set_cape_rejects_empty_cape_id(tmp_path) -> None
#   - test_texture_urls_returns_microsoft_skin_and_active_cape(tmp_path) -> None
#   - test_texture_urls_delegates_authlib_metadata_without_downloading_image(tmp_path) -> None
# ============================================================

from __future__ import annotations

import asyncio
import time
from importlib import import_module
from typing import Any

import httpx
import pytest

import ECL.services.account.accounts as accounts_service
from ECL.foundation import EventBus
from ECL.services import AccountManager
from ECL.services.account.accounts import LauncherMicrosoftAccountManager


class FakeMicrosoftManager:
    def __init__(self):
        self.on_device_code = None
        self.accounts: dict[str, dict[str, Any]] = {}
        self.closed = False

    def get_microsoft_accounts(self) -> dict[str, dict[str, Any]]:
        return self.accounts.copy()

    async def add_microsoft_account(self) -> str:
        self.on_device_code(
            {
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/link",
                "message": "Use the device code",
                "interval": 1,
            }
        )
        account_id = "microsoft-account"
        self.accounts[account_id] = {
            "AccountId": account_id,
            "Email": "player@example.com",
            "Profile": {
                "id": "0123456789abcdef0123456789abcdef",
                "name": "Player",
                "skins": [{"url": "https://textures.example.com/skin.png"}],
            },
            "Skin": {},
        }
        return account_id

    def del_microsoft_account(self, account_id: str) -> None:
        self.accounts.pop(account_id)

    async def refresh_profile(self, account_id: str) -> dict[str, Any]:
        return self.accounts[account_id]

    async def get_minecraft_token(self, account_id: str) -> str:
        assert account_id in self.accounts
        return "minecraft-access-token"

    async def upload_skin(self, account_id: str, variant: str, image: bytes) -> dict[str, Any]:
        assert account_id in self.accounts
        self.skin_variant = variant
        return {"uploaded": True}

    async def reset_skin(self, account_id: str) -> dict[str, Any]:
        assert account_id in self.accounts
        return {"reset": True}

    async def set_cape(self, account_id: str, cape_id: str) -> dict[str, Any]:
        assert account_id in self.accounts
        self.selected_cape_id = cape_id
        return {"cape": cape_id}

    async def reset_cape(self, account_id: str) -> dict[str, Any]:
        assert account_id in self.accounts
        return {"cape": None}

    async def aclose(self) -> None:
        self.closed = True


class FakeAuthlibManager:
    def __init__(self):
        self.accounts: dict[str, dict[str, Any]] = {}
        self.closed = False

    def list_accounts(self) -> dict[str, dict[str, Any]]:
        return self.accounts.copy()

    def resolve_server(self, url: str) -> str:
        return f"{url.rstrip('/')}/api/yggdrasil"

    def add_account(self, url: str, username: str, password: str) -> tuple[str, dict[str, Any]]:
        assert password == "secret-password"
        account_id = "yggdrasil-account"
        self.accounts[account_id] = {
            "AccountId": account_id,
            "YggdrasilAPI": url,
            "Username": username,
            "Profiles": {
                "selectedProfile": {
                    "id": "0123456789abcdef0123456789abcdef",
                    "name": "AuthlibPlayer",
                }
            },
        }
        return account_id, self.accounts[account_id]

    def delete_account(self, account_id: str) -> None:
        self.accounts.pop(account_id)

    def refresh_account(self, account_id: str) -> dict[str, Any]:
        return self.accounts[account_id]

    def select_profile(self, account_id: str, profile_id: str) -> tuple[str, dict[str, Any]]:
        account = {
            "AccountId": account_id,
            "YggdrasilAPI": "https://skin.example.com/api/yggdrasil",
            "Username": "player@example.com",
            "Profiles": {"selectedProfile": {"id": profile_id, "name": "SelectedPlayer"}},
        }
        self.accounts[account_id] = account
        return account_id, account

    def get_token(self, account_id: str) -> dict[str, str]:
        assert account_id in self.accounts
        return {
            "AccessToken": "yggdrasil-access-token",
            "ClientToken": account_id,
            "YggdrasilAPI": self.accounts[account_id]["YggdrasilAPI"],
        }

    def get_texture_urls(self, account_id: str) -> dict[str, str]:
        assert account_id in self.accounts
        return {
            "skinUrl": "https://textures.example.com/authlib-skin.png",
            "capeUrl": "https://textures.example.com/authlib-cape.png",
        }

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _use_isolated_authlib_manager(monkeypatch):
    monkeypatch.setattr(accounts_service, "AuthlibAccountManager", FakeAuthlibManager)


class BlockingMicrosoftManager(FakeMicrosoftManager):
    def __init__(self):
        super().__init__()
        self.flow: dict[str, Any] | None = None

    async def add_microsoft_account(self) -> str:
        self.flow = {
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/link",
            "message": "Use the device code",
            "interval": 1,
            "expires_at": time.time() + 900,
        }
        self.on_device_code(self.flow)
        while self.flow["expires_at"] > time.time():
            await asyncio.sleep(0.01)
        raise RuntimeError("device flow cancelled")


class DuplicateMicrosoftManager(FakeMicrosoftManager):
    def __init__(self):
        super().__init__()
        self.login_count = 0

    async def add_microsoft_account(self) -> str:
        self.login_count += 1
        self.on_device_code(
            {
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/link",
                "message": "Use the device code",
                "interval": 1,
            }
        )
        account_id = f"microsoft-account-{self.login_count}"
        self.accounts[account_id] = {
            "AccountId": account_id,
            "Email": "player@example.com",
            "Profile": {
                "id": "0123456789abcdef0123456789abcdef",
                "name": "Player",
                "skins": [],
            },
            "Skin": {},
        }
        return account_id


def test_offline_accounts_persist_and_emit_changes(tmp_path) -> None:
    events = []
    event_bus = EventBus()
    event_bus.subscribe("accounts:changed", events.append)
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager(), event_bus=event_bus)

    account = manager.add_offline("Steve")

    assert account["type"] == "offline"
    assert account["alias"] == "Steve"
    assert manager.current_account()["id"] == account["id"]
    assert events[-1]["current"]["id"] == account["id"]

    restored = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())
    assert restored.current_account()["alias"] == "Steve"


def test_offline_account_accepts_custom_uuid(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())

    account = manager.add_offline("CustomPlayer", "0123456789abcdef0123456789abcdef")

    assert account["uuid"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert account["id"] == "offline:01234567-89ab-cdef-0123-456789abcdef"


async def test_offline_account_exposes_launch_credentials(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())
    manager.add_offline("Steve", "0123456789abcdef0123456789abcdef")

    assert await manager.get_launch_credentials() == {
        "player_name": "Steve",
        "uuid": "0123456789abcdef0123456789abcdef",
        "user_type": "legacy",
        "access_token": "None",
    }


def test_offline_account_rejects_invalid_custom_uuid(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())

    with pytest.raises(accounts_service.AccountError) as error:
        manager.add_offline("CustomPlayer", "not-a-uuid")

    assert error.value.error_code == "INVALID_OFFLINE_UUID"


def _make_skin_resource(tmp_path) -> str:
    """构造仅含一个皮肤 PNG 的只读资源目录，返回其路径。"""
    resource_path = tmp_path / "resources" / "Skins"
    resource_path.mkdir(parents=True)
    (resource_path / "Alice.png").write_bytes(b"\x89PNG-fake-skin-bytes")
    return str(tmp_path)


def test_offline_default_skins_load_from_resource_path(tmp_path) -> None:
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        resource_path=_make_skin_resource(tmp_path),
    )

    skins = manager.default_skins()

    assert len(skins) == 1
    assert skins[0]["id"] == "alice"
    assert skins[0]["name"] == "Alice"
    assert skins[0]["skinUrl"].startswith("data:image/png;base64,")


def test_offline_add_with_skin_exposes_skin_url_in_list(tmp_path) -> None:
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        resource_path=_make_skin_resource(tmp_path),
    )

    account = manager.add_offline("Steve", skin="Alice")

    assert account["skin"] == "alice"
    listed = next(item for item in manager.list_accounts()["accounts"] if item["id"] == account["id"])
    assert listed["skinUrl"].startswith("data:image/png;base64,")


def test_offline_texture_urls_uses_selected_skin(tmp_path) -> None:
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        resource_path=_make_skin_resource(tmp_path),
    )

    account = manager.add_offline("Steve", skin="Alice")

    textures = manager.texture_urls(account["id"])
    assert textures["skinUrl"].startswith("data:image/png;base64,")
    assert textures["skinModel"] == "slim"


def test_offline_set_skin_updates_and_persists(tmp_path) -> None:
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        resource_path=_make_skin_resource(tmp_path),
    )
    account = manager.add_offline("Steve")

    accounts = manager.set_offline_skin(account["id"], "Alice")

    updated = next(item for item in accounts if item["id"] == account["id"])
    assert updated["skin"] == "alice"
    assert updated["skinUrl"].startswith("data:image/png;base64,")


def test_offline_invalid_skin_is_rejected(tmp_path) -> None:
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        resource_path=_make_skin_resource(tmp_path),
    )

    with pytest.raises(accounts_service.AccountError) as error:
        manager.add_offline("Steve", skin="missing")

    assert error.value.error_code == "INVALID_OFFLINE_SKIN"


def test_offline_without_skin_has_no_skin_url(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())

    account = manager.add_offline("Steve")

    assert "skin" not in account
    assert "skinUrl" not in manager.list_accounts()["accounts"][0]


async def test_authlib_account_can_login_launch_refresh_and_remove(tmp_path) -> None:
    authlib_manager = FakeAuthlibManager()
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        authlib_manager=authlib_manager,
    )

    account = manager.add_authlib(
        "https://skin.example.com/api/yggdrasil",
        "player@example.com",
        "secret-password",
    )

    assert account == {
        "id": "yggdrasil-account",
        "alias": "AuthlibPlayer",
        "type": "authlib",
        "email": "player@example.com",
        "uuid": "0123456789abcdef0123456789abcdef",
        "auth_server": "https://skin.example.com/api/yggdrasil",
        "isCurrent": True,
    }
    assert await manager.get_launch_credentials() == {
        "player_name": "AuthlibPlayer",
        "uuid": "0123456789abcdef0123456789abcdef",
        "user_type": "yggdrasil",
        "access_token": "yggdrasil-access-token",
        "auth_server": "https://skin.example.com/api/yggdrasil",
    }
    assert (await manager.refresh_account(account["id"]))["alias"] == "AuthlibPlayer"

    manager.remove_account(account["id"])

    assert manager.list_accounts()["accounts"] == []


def test_authlib_multi_profile_login_only_becomes_current_after_one_profile_is_selected(tmp_path) -> None:
    class MultiProfileAuthlibManager(FakeAuthlibManager):
        def add_account(self, url: str, username: str, password: str) -> tuple[str, dict[str, Any]]:
            return "pending-authlib", {
                "AccountId": "pending-authlib",
                "YggdrasilAPI": url,
                "Username": username,
                "Profiles": {
                    "availableProfiles": [
                        {"id": "profile-one", "name": "PlayerOne"},
                        {"id": "profile-two", "name": "PlayerTwo"},
                    ]
                },
            }

    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        authlib_manager=MultiProfileAuthlibManager(),
    )

    pending = manager.add_authlib(
        "https://skin.example.com/api/yggdrasil",
        "player@example.com",
        "secret-password",
    )

    assert pending["profile_selection_required"] is True
    assert pending["available_profiles"][1]["name"] == "PlayerTwo"
    assert manager.list_accounts()["current"] is None

    account = manager.select_authlib_profile("pending-authlib", "profile-two")

    assert account["alias"] == "SelectedPlayer"
    assert account["uuid"] == "profile-two"
    assert account["isCurrent"] is True


def test_authlib_server_uses_ali_resolution(tmp_path) -> None:
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        authlib_manager=FakeAuthlibManager(),
    )

    assert manager.resolve_authlib_server("https://skin.example.com") == ("https://skin.example.com/api/yggdrasil")


def test_authlib_login_uses_server_error_message(tmp_path, monkeypatch) -> None:
    authlib_manager = FakeAuthlibManager()
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        authlib_manager=authlib_manager,
    )

    def reject_login(url: str, username: str, password: str):
        request = httpx.Request("POST", f"{url}/authserver/authenticate")
        response = httpx.Response(
            403,
            request=request,
            json={
                "error": "ForbiddenOperationException",
                "errorMessage": "Invalid credentials. Invalid username or password.",
            },
        )
        raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

    monkeypatch.setattr(authlib_manager, "add_account", reject_login)

    with pytest.raises(accounts_service.AccountError) as error:
        manager.add_authlib("https://littleskin.cn/api/yggdrasil", "player@example.com", "wrong")

    assert error.value.error_code == "AUTHLIB_LOGIN_FAILED"
    assert str(error.value) == "外置登录失败: Invalid credentials. Invalid username or password."


async def test_account_manager_uses_runtime_microsoft_client_id(tmp_path, monkeypatch) -> None:
    captured_options = {}

    class CapturingMicrosoftManager(FakeMicrosoftManager):
        def __init__(self, **options):
            super().__init__()
            captured_options.update(options)

    monkeypatch.setattr(accounts_service, "LauncherMicrosoftAccountManager", CapturingMicrosoftManager)

    manager = AccountManager(tmp_path, microsoft_client_id="runtime-client-id")

    assert captured_options["client_id"] == "runtime-client-id"
    assert "cache_path" not in captured_options
    assert manager.microsoft_login_config() == {"available": True, "needs_client_id": False}
    await manager.close()


async def test_microsoft_login_requires_configured_client_id(tmp_path, monkeypatch) -> None:
    class CapturingMicrosoftManager(FakeMicrosoftManager):
        def __init__(self, **options):
            super().__init__()

    monkeypatch.setattr(accounts_service.BuildEnvironment, "microsoft_client_id", "")
    monkeypatch.setattr(accounts_service, "LauncherMicrosoftAccountManager", CapturingMicrosoftManager)
    manager = AccountManager(tmp_path)

    assert manager.microsoft_login_config() == {"available": False, "needs_client_id": True}
    with pytest.raises(accounts_service.AccountError) as error:
        await manager.start_microsoft_login()

    assert error.value.error_code == "MICROSOFT_CLIENT_ID_REQUIRED"
    assert manager._login_task is None
    await manager.close()


async def test_microsoft_device_login_flow(tmp_path) -> None:
    login_events = []
    event_bus = EventBus()
    event_bus.subscribe("accounts:microsoft_login_status", login_events.append)
    microsoft_manager = FakeMicrosoftManager()
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager, event_bus=event_bus)

    started = await manager.start_microsoft_login()
    if manager._login_task is not None:
        await asyncio.wait_for(manager._login_task, timeout=1)

    assert started["status"] in {"pending", "completed"}
    if started["status"] == "pending":
        assert started["userCode"] == "ABCD-EFGH"
        assert started["verificationUri"] == "https://microsoft.com/link"
        assert manager.poll_microsoft_login()["status"] == "ready"
        completed = manager.complete_microsoft_login()
        assert completed["status"] == "completed"
        assert completed["account"]["alias"] == "Player"

    account_list = manager.list_accounts()
    assert account_list["current"]["type"] == "microsoft"
    assert account_list["current"]["skinUrl"] == "https://textures.example.com/skin.png"
    assert login_events[-1] == {"status": "ready"}


async def test_launcher_microsoft_login_reports_each_stage(tmp_path, monkeypatch) -> None:
    microsoft_auth_core = import_module("ECL.game.auth.microsoft")
    stages = []

    class FakeMicrosoftAuth:
        def __init__(self, **_options):
            pass

        async def get_token(self):
            return "microsoft-token", "player@example.com"

    class FakeMinecraftClient:
        async def get_minecraft_token(self, token):
            assert token == "microsoft-token"
            return "minecraft-token", 0.0, 3600

        async def get_profile(self, token):
            assert token == "minecraft-token"
            return {"id": "profile-id", "name": "Player", "skins": []}

        async def aclose(self):
            pass

    monkeypatch.setattr(microsoft_auth_core, "MicrosoftAuth", FakeMicrosoftAuth)
    manager = LauncherMicrosoftAccountManager(
        "client-id",
        cache_path=tmp_path,
        on_device_code=lambda _flow: None,
        on_progress=stages.append,
    )
    await manager._progress_client.client.close()
    manager._progress_client.client = FakeMinecraftClient()

    account_id = await manager.add_microsoft_account()

    assert account_id in manager.get_microsoft_accounts()
    assert stages == ["authorization_confirmed", "minecraft_token", "profile", "saving"]
    await manager.aclose()


async def test_repeated_microsoft_login_replaces_existing_account(tmp_path) -> None:
    microsoft_manager = DuplicateMicrosoftManager()
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager)

    for _ in range(2):
        await manager.start_microsoft_login()
        await asyncio.wait_for(manager._login_task, timeout=1)
        manager.complete_microsoft_login()

    account_list = manager.list_accounts()

    assert [account["id"] for account in account_list["accounts"]] == ["microsoft-account-2"]
    assert account_list["current"]["id"] == "microsoft-account-2"
    assert list(microsoft_manager.accounts) == ["microsoft-account-2"]


def test_existing_microsoft_duplicates_are_merged_on_startup(tmp_path) -> None:
    microsoft_manager = DuplicateMicrosoftManager()
    microsoft_manager.login_count = 2
    for account_id in ("microsoft-account-1", "microsoft-account-2"):
        microsoft_manager.accounts[account_id] = {
            "AccountId": account_id,
            "Email": "player@example.com",
            "Profile": {
                "id": "0123456789abcdef0123456789abcdef",
                "name": "Player",
                "skins": [],
            },
            "Skin": {},
        }

    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager)

    assert [account["id"] for account in manager.list_accounts()["accounts"]] == ["microsoft-account-2"]
    assert list(microsoft_manager.accounts) == ["microsoft-account-2"]


def test_remove_and_switch_accounts(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())
    first = manager.add_offline("Steve")
    second = manager.add_offline("Alex")

    manager.switch_account(first["id"])
    assert manager.current_account()["id"] == first["id"]

    manager.remove_account(first["id"])
    assert manager.current_account()["id"] == second["id"]


def test_complete_microsoft_login_keeps_pending_state(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())
    manager._login_state = {"status": "pending", "interval": 3}

    result = manager.complete_microsoft_login()

    assert result == {"status": "pending", "retry_after": 3}
    assert manager.poll_microsoft_login() == {"status": "pending", "retry_after": 3}


async def test_cancel_microsoft_login_stops_device_flow(tmp_path) -> None:
    login_events = []
    event_bus = EventBus()
    event_bus.subscribe("accounts:microsoft_login_status", login_events.append)
    microsoft_manager = BlockingMicrosoftManager()
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager, event_bus=event_bus)

    started = await manager.start_microsoft_login()
    assert started["status"] == "pending"
    assert manager.cancel_microsoft_login() is True

    await asyncio.gather(manager._login_task, return_exceptions=True)

    assert manager._login_task.done()
    assert microsoft_manager.flow["expires_at"] == 0
    assert microsoft_manager.flow["interval"] == 2
    assert manager.poll_microsoft_login() == {"status": "error", "message": "Microsoft 登录已取消"}
    assert login_events[-1] == {"status": "cancelled"}


async def test_close_cancels_login_and_releases_manager(tmp_path) -> None:
    microsoft_manager = BlockingMicrosoftManager()
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager)

    await manager.start_microsoft_login()
    await manager.close()

    assert microsoft_manager.closed is True


def test_microsoft_account_includes_capes(tmp_path) -> None:
    microsoft_manager = FakeMicrosoftManager()
    microsoft_manager.accounts["microsoft-account"] = {
        "AccountId": "microsoft-account",
        "Email": "player@example.com",
        "Profile": {
            "id": "0123456789abcdef0123456789abcdef",
            "name": "Player",
            "skins": [{"url": "https://textures.example.com/skin.png"}],
            "capes": [
                {
                    "id": "migrator",
                    "alias": "Migrator Cape",
                    "state": "ACTIVE",
                    "url": "https://textures.example.com/cape.png",
                },
                {"id": "minecon-2016", "alias": "MINECON 2016 Cape", "state": "INACTIVE"},
            ],
        },
        "Skin": {},
    }
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager)

    account = manager.list_accounts()["accounts"][0]
    assert account["capes"] == [
        {"id": "migrator", "name": "Migrator Cape", "state": "ACTIVE", "url": "https://textures.example.com/cape.png"},
        {"id": "minecon-2016", "name": "MINECON 2016 Cape", "state": "INACTIVE", "url": ""},
    ]


async def _manager_with_microsoft_account(tmp_path) -> tuple[AccountManager, FakeMicrosoftManager]:
    microsoft_manager = FakeMicrosoftManager()
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager)
    await microsoft_manager.add_microsoft_account()
    return manager, microsoft_manager


async def test_upload_skin_refreshes_account(tmp_path) -> None:
    manager, microsoft_manager = await _manager_with_microsoft_account(tmp_path)

    account = await manager.upload_skin("microsoft-account", "slim", b"\x89PNG")

    assert microsoft_manager.skin_variant == "slim"
    assert account["type"] == "microsoft"
    assert account["id"] == "microsoft-account"


async def test_upload_skin_normalizes_variant(tmp_path) -> None:
    manager, microsoft_manager = await _manager_with_microsoft_account(tmp_path)

    await manager.upload_skin("microsoft-account", "SLIM", b"bytes")
    assert microsoft_manager.skin_variant == "slim"

    await manager.upload_skin("microsoft-account", "classic", b"bytes")
    assert microsoft_manager.skin_variant == "classic"


async def test_reset_skin(tmp_path) -> None:
    manager, _microsoft_manager = await _manager_with_microsoft_account(tmp_path)

    account = await manager.reset_skin("microsoft-account")
    assert account["id"] == "microsoft-account"


async def test_set_and_reset_cape(tmp_path) -> None:
    manager, microsoft_manager = await _manager_with_microsoft_account(tmp_path)

    account = await manager.set_cape("microsoft-account", "migrator")
    assert account["id"] == "microsoft-account"
    assert microsoft_manager.selected_cape_id == "migrator"

    account = await manager.reset_cape("microsoft-account")
    assert account["id"] == "microsoft-account"


async def test_skin_operations_require_microsoft_account(tmp_path) -> None:
    manager = AccountManager(tmp_path, microsoft_manager=FakeMicrosoftManager())
    manager.add_offline("Steve")

    with pytest.raises(accounts_service.AccountError):
        await manager.upload_skin("offline:not-a-real-uuid", "classic", b"bytes")
    with pytest.raises(accounts_service.AccountError):
        await manager.set_cape("offline:not-a-real-uuid", "migrator")
    with pytest.raises(accounts_service.AccountError):
        await manager.reset_cape("offline:not-a-real-uuid")


async def test_set_cape_rejects_empty_cape_id(tmp_path) -> None:
    manager, _microsoft_manager = await _manager_with_microsoft_account(tmp_path)

    with pytest.raises(accounts_service.AccountError):
        await manager.set_cape("microsoft-account", "   ")


def test_texture_urls_returns_microsoft_skin_and_active_cape(tmp_path) -> None:
    microsoft_manager = FakeMicrosoftManager()
    microsoft_manager.accounts["microsoft-account"] = {
        "Email": "player@example.com",
        "Profile": {
            "id": "player-id",
            "name": "Player",
            "skins": [{"url": "https://textures.example.com/skin.png"}],
            "capes": [
                {"id": "inactive", "state": "INACTIVE", "url": "https://textures.example.com/old.png"},
                {"id": "active", "state": "ACTIVE", "url": "https://textures.example.com/cape.png"},
            ],
        },
    }
    manager = AccountManager(tmp_path, microsoft_manager=microsoft_manager)

    assert manager.texture_urls("microsoft-account") == {
        "skinUrl": "https://textures.example.com/skin.png",
        "skinModel": "classic",
        "capeUrl": "https://textures.example.com/cape.png",
    }


def test_texture_urls_delegates_authlib_metadata_without_downloading_image(tmp_path) -> None:
    authlib_manager = FakeAuthlibManager()
    manager = AccountManager(
        tmp_path,
        microsoft_manager=FakeMicrosoftManager(),
        authlib_manager=authlib_manager,
    )
    account = manager.add_authlib("https://example.com", "player@example.com", "secret-password")

    assert manager.texture_urls(account["id"]) == {
        "skinUrl": "https://textures.example.com/authlib-skin.png",
        "capeUrl": "https://textures.example.com/authlib-cape.png",
    }
