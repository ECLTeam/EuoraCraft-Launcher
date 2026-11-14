from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from ECL.plugins.api.contracts import ProtocolError
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
    metadata = {"name": "public-test", "api_version": 2}
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("from ECL.plugins.api import Plugin\n", encoding="utf-8")
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


def reviewed(service):
    """返回目录全部活动账户资源身份；权限决定流程已移除。"""
    service.refresh_account_resources()
    return {"items": [{"resource_id": ref} for ref in sorted(service._account_catalog.active_refs())]}


def test_skin_chunks_are_bound_to_selected_account_and_revision(public):
    import base64

    service, _, broker = public
    reviewed(service)
    reference = call(broker, "accounts.list")["items"][0]["account_ref"]
    observed = []
    broker.public_queries._skin = lambda account_id: observed.append(account_id) or b"x" * 9000
    first = call(broker, "accounts.skin", {"account_ref": reference})
    assert len(base64.b64decode(first["content"])) == 8192
    second = call(
        broker,
        "accounts.skin",
        {"account_ref": reference, "offset": first["next_offset"], "revision": first["revision"]},
    )
    assert len(base64.b64decode(second["content"])) == 808
    assert second["next_offset"] is None
    assert len(observed) == 1
    with pytest.raises(ProtocolError):
        call(broker, "accounts.skin", {"account_ref": "f" * 32})
    assert len(observed) == 1


def test_account_query_returns_all_valid_refs_and_never_persistent_identity(public):
    service, _, broker = public
    reviewed(service)
    result = call(broker, "accounts.list")
    assert len(result["items"]) == 2
    item = result["items"][0]
    assert set(item) == {"account_ref", "alias", "uuid", "type", "is_current"}
    assert call(broker, "accounts.get", {"account_ref": item["account_ref"]}) == item
    encoded = json.dumps(result)
    assert "private-id" not in encoded and "private-account-id" not in encoded
    assert call(broker, "resources.list", {"capability": "accounts.read"})["items"] == result["items"]


def test_foreign_reference_is_rejected(public):
    service, _, broker = public
    reviewed(service)
    with pytest.raises(ProtocolError):
        call(broker, "accounts.get", {"account_ref": "f" * 32})
    with pytest.raises(ProtocolError):
        call(broker, "accounts.get", {"account_ref": "f" * 32, "token": True})


def test_account_changes_invalidate_old_selection_and_do_not_auto_grant_new_identity(public):
    service, accounts, broker = public
    reviewed(service)
    old = call(broker, "accounts.list")["items"]
    accounts[:] = [replace(account, alias=account.alias + " changed") for account in accounts]
    with pytest.raises(ProtocolError):
        call(broker, "accounts.get", {"account_ref": old[0]["account_ref"]})


def test_current_selection_updates_without_granting_any_other_account(public):
    service, accounts, broker = public
    reviewed(service)
    original = call(broker, "accounts.list")["items"]
    accounts[:] = [replace(account, is_current=not account.is_current) for account in accounts]
    updated = call(broker, "accounts.list")["items"]
    assert {item["account_ref"] for item in updated} == {item["account_ref"] for item in original}
    assert [item["is_current"] for item in updated] != [item["is_current"] for item in original]


def test_public_config_is_an_explicit_read_only_mapping(public):
    _service, _, broker = public
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
    reviewed(service)
    assert call(broker, "accounts.list")["items"]
    index = service.data_path / "plugins" / "account_resources.json"
    index.write_text('{"credentials":"private"}', encoding="utf-8")
    with pytest.raises(ProtocolError):
        call(broker, "accounts.list")
    assert index.read_text(encoding="utf-8") == '{"credentials":"private"}'


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
