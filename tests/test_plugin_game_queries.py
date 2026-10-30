from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import (
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.services.game import GameService


@pytest.fixture
def catalogs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value=1", encoding="utf-8")
    binding = PluginContentBinding.capture(source, (), "windows-x86_64-cp311")
    store = PluginGrantStore(tmp_path, implemented_capabilities=PluginHostBroker.implemented_capabilities)
    game = GameService.__new__(GameService)
    game._game_download_source_provider = lambda: "official"
    game.minecraft_versions_classified = lambda source: {
        "release": [{"id": f"1.{index}", "releaseTime": "2026-01-01", "url": "private-secret"} for index in range(5)],
        "snapshot": [{"id": "26w01a", "releaseTime": "2026-01-01"}],
        "old_beta": [{"id": "b1.7", "releaseTime": "2011-01-01"}],
    }
    game.loader_versions = lambda loader, version, source: ["0.2", "0.1"]
    brokers = []

    def create(allow=True):
        session = store.open_session(uuid4().hex, binding)
        store.update(
            session,
            (),
            expected_revision=0,
            content_digest=binding.content_digest,
            caller=PermissionManagementCaller.main_webview,
        )
        broker = PluginHostBroker(
            session, tmp_path, lambda *args: {}, run_state=lambda: "enabled", game_operations=game
        )
        brokers.append(broker)
        return broker

    yield create(), game, store, create
    for broker in brokers:
        broker.close()
        broker.session.close()


def call(broker, method="game.catalog", params=None, deadline=None):
    return broker.handle(RpcRequest(method, params or {}, deadline or monotonic() + 5, uuid4().hex))


def test_fixed_catalog_projection_and_latest_loader_flag(catalogs):
    broker, _, _, _ = catalogs
    page = call(broker, params={"type": "release", "limit": 2})
    assert page["items"] == [
        {"version_id": "1.0", "type": "release", "release_time": "2026-01-01"},
        {"version_id": "1.1", "type": "release", "release_time": "2026-01-01"},
    ]
    following = call(broker, params={"type": "release", "limit": 2, "cursor": page["next_cursor"]})
    assert following["items"][0]["version_id"] == "1.2"
    loaders = call(broker, "game.loaders", {"game_version": "1.0", "loader": "fabric"})
    assert loaders["items"] == [
        {"loader_version": "0.2", "is_latest": True},
        {"loader_version": "0.1", "is_latest": False},
    ]
    assert "url" not in str(page) and "private-secret" not in str(page)
    assert "b1.7" not in str(call(broker))


@pytest.mark.parametrize("params", [{"source": "evil"}, {"type": "old_beta"}, {"limit": True}, {"limit": 101}])
def test_catalog_rejects_unknown_fields_types_and_out_of_scope_categories(catalogs, params):
    with pytest.raises(ProtocolError, match="参数") as error:
        call(catalogs[0], params=params)
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"


def test_query_cursors_do_not_cross_run_or_query(catalogs):
    broker, _, _, create = catalogs
    cursor = call(broker, params={"type": "release", "limit": 1})["next_cursor"]
    for other, params in (
        (broker, {"type": "snapshot", "limit": 1, "cursor": cursor}),
        (create(), {"type": "release", "limit": 1, "cursor": cursor}),
    ):
        with pytest.raises(ProtocolError) as error:
            call(other, params=params)
        assert error.value.error_code == "PLUGIN_CURSOR_INVALID"


def test_catalog_is_basic_and_unknown_game_version_is_rejected(catalogs):
    broker, _, _, create = catalogs
    assert call(create(False))["items"]
    with pytest.raises(ProtocolError) as error:
        call(broker, "game.loaders", {"game_version": "arbitrary", "loader": "forge"})
    assert error.value.error_code == "PLUGIN_GAME_VERSION_INVALID"


def test_latest_flag_matches_install_default_even_when_upstream_order_is_unsorted(catalogs):
    broker, game, _, _ = catalogs
    game.loader_versions = lambda *_: ["0.1", "0.9", "0.2"]
    items = call(broker, "game.loaders", {"game_version": "1.0", "loader": "fabric"})["items"]
    assert [item["loader_version"] for item in items] == ["0.9", "0.2", "0.1"]
    assert [item["is_latest"] for item in items] == [True, False, False]


def test_revocation_during_network_read_prevents_result_delivery(catalogs):
    broker, game, store, _ = catalogs
    entered, release = Event(), Event()
    original = game.minecraft_versions_classified

    def load(source):
        entered.set()
        assert release.wait(5)
        return original(source)

    game.minecraft_versions_classified = load
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(call, broker)
        assert entered.wait(3)
        try:
            store.update(
                broker.session,
                (),
                expected_revision=broker.session.view().revision,
                content_digest=broker.session.binding.content_digest,
                caller=PermissionManagementCaller.main_webview,
            )
        finally:
            release.set()
        with pytest.raises(ProtocolError) as error:
            result.result(5)
        assert error.value.error_code == "PLUGIN_GRANT_EXPIRED"


def test_expired_query_is_not_delivered(catalogs):
    with pytest.raises(ProtocolError) as error:
        call(catalogs[0], deadline=monotonic() - 1)
    assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
