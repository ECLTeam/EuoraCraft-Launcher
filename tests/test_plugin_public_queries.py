from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.public_queries import RuntimePublicQueries
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.account.accounts import AccountManager
from ECL.utils import AccountError
from ECL.utils.account_reading import AccountSummary
from ECL.utils.config import default_config
from tests.test_accounts import FakeMicrosoftManager
from tests.test_plugin_host_files import call


@pytest.fixture
def public(tmp_path):
    accounts = [
        AccountSummary("private-account-id", "Player", "a" * 32, "offline", True),
        AccountSummary("other-private-id", "Other", "b" * 32, "microsoft", False),
    ]
    service = PluginWorkerService(
        tmp_path / "data", account_snapshot=lambda: tuple(accounts), public_config=lambda: default_config
    )
    source = tmp_path / "plugin"
    source.mkdir()
    metadata = {
        "name": "public-test",
        "api_version": 2,
        "permissions": [{"capability": "accounts.read", "required": False}],
    }
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("from ECL.plugin_api import Plugin\n", encoding="utf-8")
    service.register(metadata, source, "directory")
    entry = service._entry("public-test")
    api = RuntimePublicQueries(
        entry.session, service._account_catalog, service.refresh_account_resources, lambda: default_config
    )
    broker = PluginHostBroker(
        entry.session, service.data_path, lambda *_: {}, run_state=lambda: "enabled", public_queries=api
    )
    try:
        yield service, accounts, broker
    finally:
        broker.close()
        service.close()


def approve(service, references, revision=None):
    view = service.permissions("public-test")
    return service.update_permissions(
        "public-test",
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="accounts.read", decision="allow", resource_refs=tuple(references)),),
        PermissionManagementCaller.main_webview,
        account_resource_revision=revision,
    )


def reviewed(service):
    view = service.permissions("public-test")
    return service.permission_resources("public-test", view["content_digest"], None, "", None, 100, "accounts.read")


def test_account_query_requires_explicit_selection_and_never_returns_persistent_identity(public):
    service, _, broker = public
    page = reviewed(service)
    with pytest.raises(ProtocolError):
        call(broker, "accounts.list")
    selected = page["items"][0]["resource_id"]
    approve(service, [selected], page["catalog_revision"])
    result = call(broker, "accounts.list")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert set(item) == {"account_ref", "alias", "uuid", "type", "is_current"}
    assert call(broker, "accounts.get", {"account_ref": item["account_ref"]}) == item
    encoded = json.dumps([result, call(broker, "permissions.list")])
    assert selected not in encoded and "private-id" not in encoded and "private-account-id" not in encoded
    assert call(broker, "resources.list", {"capability": "accounts.read"})["items"] == result["items"]


def test_foreign_reference_empty_choice_and_stale_revision_are_rejected(public):
    service, _, broker = public
    page = reviewed(service)
    selected = page["items"][0]["resource_id"]
    with pytest.raises(ProtocolError, match="变化"):
        approve(service, [selected], page["catalog_revision"] + 1)
    with pytest.raises(PluginGrantError):
        approve(service, [], page["catalog_revision"])
    approve(service, [selected], page["catalog_revision"])
    with pytest.raises(ProtocolError):
        call(broker, "accounts.get", {"account_ref": "f" * 32})
    with pytest.raises(ProtocolError):
        call(broker, "accounts.get", {"account_ref": "f" * 32, "token": True})


def test_account_changes_invalidate_old_selection_and_do_not_auto_grant_new_identity(public):
    service, accounts, broker = public
    page = reviewed(service)
    approve(service, [item["resource_id"] for item in page["items"]], page["catalog_revision"])
    old = call(broker, "accounts.list")["items"]
    accounts[:] = [replace(account, alias=account.alias + " changed") for account in accounts]
    with pytest.raises(ProtocolError):
        call(broker, "accounts.get", {"account_ref": old[0]["account_ref"]})
    fresh = reviewed(service)
    assert all(not item["selected"] for item in fresh["items"] if item["available"])
    assert len([item for item in fresh["items"] if not item["available"]]) == 2


def test_current_selection_updates_without_granting_any_other_account(public):
    service, accounts, broker = public
    page = reviewed(service)
    approve(service, [item["resource_id"] for item in page["items"]], page["catalog_revision"])
    original = call(broker, "accounts.list")["items"]
    accounts[:] = [replace(account, is_current=not account.is_current) for account in accounts]
    updated = call(broker, "accounts.list")["items"]
    assert {item["account_ref"] for item in updated} == {item["account_ref"] for item in original}
    assert [item["is_current"] for item in updated] != [item["is_current"] for item in original]


def test_public_config_is_an_explicit_read_only_mapping(public):
    service, _, broker = public
    view = service.permissions("public-test")
    service.update_permissions(
        "public-test", view["content_digest"], view["revision"], (), PermissionManagementCaller.main_webview
    )
    result = call(broker, "config.public")
    assert result == {
        "locale": "zh-CN",
        "theme_id": "classic",
        "theme_mode": "system",
        "accent_color": "#5B6FF5",
        "default_memory_mb": 4096,
    }
    with pytest.raises(ProtocolError):
        call(broker, "config.public", {"section": "game"})
    with pytest.raises(ProtocolError):
        call(broker, "config.set", {"memory_size": 1024})


def test_corrupt_account_index_fails_closed_and_preserves_file(public):
    service, _, broker = public
    page = reviewed(service)
    approve(service, [page["items"][0]["resource_id"]], page["catalog_revision"])
    assert call(broker, "accounts.list")["items"]
    index = service.data_path / "plugin_account_resources.json"
    index.write_text('{"credentials":"private"}', encoding="utf-8")
    with pytest.raises(ProtocolError):
        call(broker, "accounts.list")
    assert index.read_text(encoding="utf-8") == '{"credentials":"private"}'
    assert service.permissions("public-test")["items"][0]["decision"] == "deny"


def test_real_account_summary_omits_email_tokens_and_never_refreshes_authentication(tmp_path, monkeypatch):
    microsoft = FakeMicrosoftManager()
    manager = AccountManager(tmp_path, microsoft_manager=microsoft)
    try:
        monkeypatch.setattr(
            manager,
            "_all_accounts",
            lambda: [
                {
                    "id": "internal",
                    "alias": "private@example.com",
                    "uuid": "a" * 32,
                    "type": "microsoft",
                    "access_token": "secret",
                    "custom": {"password": "secret"},
                }
            ],
        )
        monkeypatch.setattr(microsoft, "refresh_profile", lambda *_: pytest.fail("summary refreshed authentication"))
        summary = manager.plugin_account_snapshot()
        assert summary[0].alias == ""
        assert "secret" not in repr(summary) and "example.com" not in repr(summary)
        monkeypatch.setattr(
            manager,
            "_all_accounts",
            lambda: [{"id": "internal", "alias": "Player", "uuid": "not-a-player-uuid", "type": "offline"}],
        )
        with pytest.raises(AccountError) as invalid:
            manager.plugin_account_snapshot()
        assert invalid.value.error_code == "PLUGIN_ACCOUNT_QUERY_FAILED"
        assert "not-a-player-uuid" not in str(invalid.value)
    finally:
        asyncio.run(manager.close())
