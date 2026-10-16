from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ECL.services.game.resource_metadata import ResourceMetadataCache, ResourceMetadataError


def test_metadata_coalesces_network_load_and_partitions_sources() -> None:
    cache = ResourceMetadataCache()
    entered = Event()
    release = Event()
    calls = 0

    def load() -> object:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return {"id": "demo", "source": "modrinth", "title": "资源"}

    key = ("modrinth", "demo", "mod", "official")
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            first = workers.submit(cache.get, key, load)
            assert entered.wait(3)
            second = workers.submit(cache.get, key, load)
            release.set()
            assert first.result().id == second.result().id == "demo"
        assert cache.get(key, load).title == "资源"
        assert calls == 1
        cache.get((*key[:-1], "mcim"), load)
        assert calls == 2
    finally:
        release.set()
        cache.close()


def test_metadata_failures_are_not_cached_and_capacity_is_bounded() -> None:
    cache = ResourceMetadataCache(capacity=1)
    calls = 0

    def load() -> object:
        nonlocal calls
        calls += 1
        return {"id": "demo", "source": "modrinth"}

    try:
        with pytest.raises(ResourceMetadataError):
            cache.get(("bad",), lambda: {})
        cache.get(("bad",), load)
        cache.get(("second",), load)
        cache.get(("bad",), load)
        assert calls == 3
    finally:
        cache.close()


def test_metadata_close_does_not_publish_late_network_result() -> None:
    cache = ResourceMetadataCache()
    entered = Event()
    release = Event()

    def load() -> object:
        entered.set()
        assert release.wait(3)
        return {"id": "demo", "source": "modrinth"}

    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(cache.get, ("demo",), load)
        assert entered.wait(3)
        cache.close()
        release.set()
        with pytest.raises(ResourceMetadataError, match="已关闭"):
            future.result()
    with pytest.raises(ResourceMetadataError):
        cache.get(("demo",), load)
