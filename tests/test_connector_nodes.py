from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ECL.services.connector import ConnectorService
from ECL.services.connector_nodes import ConnectorNodeConfig, ConnectorNodeError, ConnectorNodeSettings
from ECL.utils.config import ConfigStore


@pytest.mark.parametrize(
    "uri",
    [
        "tcp://relay.test:11010",
        "udp://127.0.0.1:42",
        "quic://[::1]:123",
        "faketcp://relay.test:1",
        "ws://relay.test:80/peer",
        "wss://relay.test:443/peer",
    ],
)
def test_supported_node_uris(uri: str) -> None:
    assert ConnectorNodeSettings(mode="custom", nodes=[uri]).nodes == [uri]


@pytest.mark.parametrize(
    "uri",
    [
        "https://relay.test:443",
        "tcp://relay.test",
        "tcp://relay.test:0",
        "tcp://relay.test:65536",
        "tcp://user:pass@relay.test:1",
        "tcp://relay.test:1/path",
        "tcp://relay.test:1?token=x",
        "tcp://relay.test:1#fragment",
        "tcp://relay .test:1",
        "tcp://[bad]:1",
    ],
)
def test_invalid_node_uris(uri: str) -> None:
    with pytest.raises(ValidationError):
        ConnectorNodeSettings(mode="custom", nodes=[uri])


def test_custom_only_never_fetches_public_nodes(monkeypatch) -> None:
    settings = ConnectorNodeSettings(mode="custom", nodes=["tcp://relay.test:11010"])
    service = ConnectorService(node_settings_provider=lambda: settings)
    monkeypatch.setattr(service, "_fetch_public_nodes", lambda **kwargs: pytest.fail("公共节点不得被请求"))
    try:
        assert service.fetch_nodes(force=True) == settings.nodes
    finally:
        service.close()


def test_append_deduplicates_and_live_room_keeps_nodes(monkeypatch) -> None:
    selected = ConnectorNodeSettings(mode="append", nodes=["tcp://relay.test:1"])
    service = ConnectorService(node_settings_provider=lambda: selected)
    monkeypatch.setattr(service, "_fetch_public_nodes", lambda **kwargs: ["tcp://relay.test:1", "tcp://public.test:2"])
    try:
        assert service.fetch_nodes() == ["tcp://relay.test:1", "tcp://public.test:2"]
        active_nodes = list(service._nodes)
        selected = ConnectorNodeSettings(mode="custom", nodes=["tcp://new.test:3"])
        assert service.fetch_nodes() == selected.nodes
        assert service._nodes == active_nodes
    finally:
        service.close()


def test_config_accepts_drafts_but_rejects_invalid_structure(tmp_path) -> None:
    store = ConfigStore(tmp_path)
    for nodes in ([], [" unfinished ", ""]):
        store.save_config("connector", {"mode": "custom", "nodes": nodes})
        assert ConfigStore(tmp_path).get_config("connector") == {"mode": "custom", "nodes": nodes}
    with pytest.raises(ValidationError):
        store.save_config("connector", {"mode": "custom", "nodes": [42]})
    assert store.get_config("connector")["nodes"] == [" unfinished ", ""]


def test_runtime_normalizes_without_changing_saved_draft() -> None:
    draft = ConnectorNodeConfig(mode="custom", nodes=[" tcp://Relay.test:1 ", "", "tcp://relay.test:1"])
    assert ConnectorNodeSettings.for_connection(draft).nodes == ["tcp://relay.test:1"]
    assert draft.nodes == [" tcp://Relay.test:1 ", "", "tcp://relay.test:1"]


def test_automatic_and_preload_ignore_custom_drafts(monkeypatch) -> None:
    draft = ConnectorNodeConfig(mode="automatic", nodes=["unfinished"])
    service = ConnectorService(node_settings_provider=lambda: draft)
    monkeypatch.setattr(service, "_fetch_public_nodes", lambda **kwargs: ["tcp://public.test:1"])
    try:
        assert service.fetch_nodes() == ["tcp://public.test:1"]
        draft.mode = "append"
        assert service.preload_nodes() == ["tcp://public.test:1"]
        draft.mode = "custom"
        assert service.preload_nodes() == []
        with pytest.raises(ConnectorNodeError):
            service.fetch_nodes()
    finally:
        service.close()


@pytest.mark.parametrize("operation", ["join", "host_port"])
def test_invalid_nodes_restore_idle_and_release_transition(monkeypatch, operation) -> None:
    import ECL.services.connector as connector_module

    draft = ConnectorNodeConfig(mode="custom", nodes=[])
    service = ConnectorService(node_settings_provider=lambda: draft)
    monkeypatch.setattr(connector_module, "validate_code", lambda value: True)
    try:
        for _ in range(2):
            with pytest.raises(ConnectorNodeError) as captured:
                service.join("U/AAAA-BBBB-CCCC-DDDD") if operation == "join" else service.host_port(25565)
            assert captured.value.error_code == "CONNECTOR_NODES_INVALID"
            assert "pydantic" not in str(captured.value)
            assert service._mode == "idle"
            assert not service._transitioning
    finally:
        service.close()


def test_concurrent_node_and_game_writes_preserve_both_sections(tmp_path, monkeypatch) -> None:
    store = ConfigStore(tmp_path)
    store.get_config()
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    original_write = store._write_config
    writes = []

    def controlled_write(config):
        writes.append(config)
        if len(writes) == 1:
            entered.set()
            assert release.wait(timeout=3)
        original_write(config)

    def save_game():
        second_started.set()
        store.save_config("game", {"memory_size": 6144})

    monkeypatch.setattr(store, "_write_config", controlled_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.save_config, "connector", {"mode": "custom", "nodes": ["tcp://relay.test:1"]})
        assert entered.wait(timeout=3)
        second = pool.submit(save_game)
        assert second_started.wait(timeout=3)
        try:
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    persisted = ConfigStore(tmp_path)
    assert persisted.get_config("connector")["mode"] == "custom"
    assert persisted.get_config("game")["memory_size"] == 6144
