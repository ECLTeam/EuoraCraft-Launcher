# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_auth_providers 模块的自动化测试。
#
# 公开接口：
#   - test_plugin_auth_provider_lists_and_adds_account(tmp_path) -> None
#   - test_plugin_account_resolves_launch_credentials(tmp_path) -> None
#   - test_plugin_account_persists_across_restart(tmp_path) -> None
#   - test_plugin_account_fails_when_provider_disabled(tmp_path) -> None
#   - test_plugin_without_accounts_permission_cannot_register_provider(tmp_path) -> None
# ============================================================

import asyncio
import json

import pytest
from test_accounts import FakeMicrosoftManager

from ECL.plugins import PluginManager
from ECL.services import AccountManager
from ECL.utils import AccountError


def _register_provider_plugin(data_path, *, permission: bool = True) -> None:
    plugin_dir = (
        data_path.parent / "resources/resources/system_plugins/auth-demo"
        if permission
        else data_path / "plugins/auth-demo"
    )
    plugin_dir.mkdir(parents=True)
    permissions = [{"scope": "accounts", "action": "write", "resource": "demo"}] if permission else []
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "auth-demo",
                "entry_point": "main:AuthPlugin",
                "permissions": permissions,
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins import AuthField, Plugin\n"
        "class AuthPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_auth_provider(\n"
        "            'demo', 'Demo 登录',\n"
        "            [AuthField('token', '访问令牌', placeholder='paste token')],\n"
        "            self.authenticate,\n"
        "            self.resolve,\n"
        "            description='Demo provider',\n"
        "        )\n"
        "    def authenticate(self, values):\n"
        "        token = str(values.get('token') or '').strip()\n"
        "        if not token:\n"
        "            raise ValueError('token required')\n"
        "        return {'id': 'demo-user', 'alias': 'DemoPlayer', 'uuid': '00000000-0000-0000-0000-000000000001', 'data': {'token': token}}\n"
        "    def resolve(self, account):\n"
        "        return {\n"
        "            'player_name': account['alias'],\n"
        "            'uuid': account['uuid'],\n"
        "            'user_type': 'msa',\n"
        "            'access_token': 'demo-token',\n"
        "        }\n",
        encoding="utf-8",
    )


def _build(tmp_path, *, permission: bool = True) -> tuple[AccountManager, PluginManager]:
    data_path = tmp_path / "data"
    data_path.mkdir(parents=True)
    _register_provider_plugin(data_path, permission=permission)
    manager = AccountManager(data_path, microsoft_manager=FakeMicrosoftManager())
    framework = PluginManager(auth_providers=manager.plugin_auth_providers)
    framework.initialize(data_path, tmp_path / "resources")
    return manager, framework


def test_plugin_auth_provider_lists_and_adds_account(tmp_path) -> None:
    manager, _ = _build(tmp_path)

    providers = manager.list_auth_providers()
    assert providers == [
        {
            "id": "demo",
            "title": "Demo 登录",
            "description": "Demo provider",
            "fields": [
                {"key": "token", "label": "访问令牌", "type": "text", "required": True, "placeholder": "paste token"}
            ],
        }
    ]

    account = manager.add_plugin_account("demo", {"token": "abc"})
    assert account["type"] == "plugin"
    assert account["provider"] == "demo"
    assert account["alias"] == "DemoPlayer"
    assert account["isCurrent"] is True

    all_accounts = manager.list_accounts()["accounts"]
    assert any(item["type"] == "plugin" for item in all_accounts)


def test_plugin_account_resolves_launch_credentials(tmp_path) -> None:
    manager, _ = _build(tmp_path)
    manager.add_plugin_account("demo", {"token": "abc"})

    credentials = asyncio.run(manager.get_launch_credentials())
    assert credentials == {
        "player_name": "DemoPlayer",
        "uuid": "00000000000000000000000000000001",
        "user_type": "msa",
        "access_token": "demo-token",
    }


def test_plugin_account_persists_across_restart(tmp_path) -> None:
    manager, _ = _build(tmp_path)
    manager.add_plugin_account("demo", {"token": "abc"})

    restored = AccountManager(tmp_path / "data", microsoft_manager=FakeMicrosoftManager())
    accounts = restored.list_accounts()["accounts"]
    assert any(item["type"] == "plugin" and item["provider"] == "demo" for item in accounts)


def test_plugin_account_fails_when_provider_disabled(tmp_path) -> None:
    manager, framework = _build(tmp_path)
    manager.add_plugin_account("demo", {"token": "abc"})
    framework.auth_providers.unregister_owner("auth-demo")

    with pytest.raises(AccountError) as error:
        asyncio.run(manager.get_launch_credentials())
    assert error.value.error_code == "AUTH_PROVIDER_UNAVAILABLE"


def test_plugin_without_accounts_permission_cannot_register_provider(tmp_path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir(parents=True)
    _register_provider_plugin(data_path, permission=False)
    manager = AccountManager(data_path, microsoft_manager=FakeMicrosoftManager())
    framework = PluginManager(auth_providers=manager.plugin_auth_providers)
    framework.initialize(data_path, tmp_path / "resources")

    assert framework._status.get("auth-demo") == "unsupported"
    assert manager.list_auth_providers() == []


def test_private_provider_data_is_available_after_restart_only_to_provider(tmp_path) -> None:
    manager, _ = _build(tmp_path)
    manager.add_plugin_account("demo", {"token": "persisted-secret"})
    restored = AccountManager(tmp_path / "data", microsoft_manager=FakeMicrosoftManager())
    received = []

    def resolve(account):
        received.append(account)
        return {
            "player_name": account["alias"],
            "uuid": account["uuid"],
            "user_type": "msa",
            "access_token": account["data"]["token"],
        }

    restored.plugin_auth_providers.register("auth-demo", "demo", "Demo", [], lambda _: {}, resolve)
    assert asyncio.run(restored.get_launch_credentials())["access_token"] == "persisted-secret"
    assert received[0]["data"] == {"token": "persisted-secret"}
    assert all("data" not in account for account in restored.list_accounts()["accounts"])


def test_provider_failure_preserves_account_and_hides_exception_text(tmp_path) -> None:
    manager, framework = _build(tmp_path)
    manager.add_plugin_account("demo", {"token": "persisted-secret"})
    before = manager.state_path.read_bytes()

    def fail(_account):
        raise RuntimeError("private-credential-from-plugin")

    framework.auth_providers.get("demo").resolve_credentials = fail
    with pytest.raises(AccountError) as error:
        asyncio.run(manager.get_launch_credentials())
    assert error.value.error_code == "AUTH_PROVIDER_CREDENTIAL_FAILED"
    assert "private-credential-from-plugin" not in str(error.value)
    assert manager.state_path.read_bytes() == before
    assert len(manager.list_accounts()["accounts"]) == 1


def test_disabled_provider_cannot_deliver_inflight_credentials(tmp_path) -> None:
    manager, framework = _build(tmp_path)
    manager.add_plugin_account("demo", {"token": "persisted-secret"})

    def resolve(account):
        framework.auth_providers.unregister_owner("auth-demo")
        return {"player_name": account["alias"], "uuid": account["uuid"], "user_type": "msa", "access_token": "old"}

    framework.auth_providers.get("demo").resolve_credentials = resolve
    with pytest.raises(AccountError) as error:
        asyncio.run(manager.get_launch_credentials())
    assert error.value.error_code == "AUTH_PROVIDER_UNAVAILABLE"


def test_account_switch_during_provider_callback_rejects_old_credentials(tmp_path) -> None:
    manager, framework = _build(tmp_path)
    first = manager.add_plugin_account("demo", {"token": "persisted-secret"})
    manager.add_offline("OtherPlayer")
    other = manager.list_accounts()["current"]["id"]
    manager.switch_account(first["id"])

    def resolve(account):
        manager.switch_account(other)
        return {"player_name": account["alias"], "uuid": account["uuid"], "user_type": "msa", "access_token": "old"}

    framework.auth_providers.get("demo").resolve_credentials = resolve
    with pytest.raises(AccountError) as error:
        asyncio.run(manager.get_launch_credentials())
    assert error.value.error_code == "ACCOUNT_CHANGED"
