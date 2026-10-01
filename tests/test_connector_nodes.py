from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ECL.services.connector import ConnectorService
from ECL.services.connector_nodes import ConnectorNodeSettings
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


def test_node_settings_persist_and_invalid_change_preserves_config(tmp_path) -> None:
    store = ConfigStore(tmp_path)
    store.save_config("connector", {"mode": "custom", "nodes": ["tcp://Relay.test:1", "tcp://relay.test:1"]})
    assert ConfigStore(tmp_path).get_config("connector") == {"mode": "custom", "nodes": ["tcp://relay.test:1"]}
    with pytest.raises(ValidationError):
        store.save_config("connector", {"mode": "custom", "nodes": []})
    assert store.get_config("connector")["nodes"] == ["tcp://relay.test:1"]


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
