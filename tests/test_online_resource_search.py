from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from ECL.api.models import OnlineResourceSearchRequest
from ECL.services.game.resource_search import (
    ResourceSearchService,
    SearchBatch,
    SearchCriteria,
    SearchItem,
    SearchSource,
    SearchWiki,
)
from ECL.utils import GameServiceError


@pytest.fixture
def service():
    value = ResourceSearchService()
    yield value
    value.close()


def item(source: SearchSource, project_id: str, *, wiki: str = "", title: str = "Sodium", **fields) -> SearchItem:
    return SearchItem(
        id=project_id,
        project_id=project_id,
        source=source,
        slug="sodium",
        title=title,
        display_title=title,
        author="Author",
        resource_type="mod",
        wiki=SearchWiki(id=wiki, title="钠") if wiki else None,
        **fields,
    )


def pages(mr: list[SearchItem], cf: list[SearchItem], calls: list | None = None):
    def fetch(source: SearchSource, criteria: SearchCriteria, offset: int) -> SearchBatch:
        if calls is not None:
            calls.append((source, offset))
        rows = mr if source == "modrinth" else cf
        return SearchBatch(items=rows[offset : offset + 20], raw_count=len(rows[offset : offset + 20]), total=len(rows))

    return fetch


@pytest.mark.parametrize(
    ("mr_wiki", "cf_wiki", "cf_title", "cf_author", "expected"),
    [
        ("", "", "Sodium", "Author", 1),
        ("1", "1", "Different", "Other", 1),
        ("1", "2", "Sodium", "Author", 2),
        ("", "", "Sodium fork", "Author", 2),
        ("", "", "Sodium", "", 2),
        ("", "", "SODIUM", "AUTHOR", 1),
    ],
)
def test_conservative_identity(service, mr_wiki, cf_wiki, cf_title, cf_author, expected):
    mr = item("modrinth", "123", wiki=mr_wiki)
    cf = item("curseforge", "123", wiki=cf_wiki, title=cf_title).model_copy(update={"author": cf_author})
    result = service.search(SearchCriteria(), pages([mr], [cf]), curseforge_available=True)
    assert result.total == expected
    if expected == 1:
        assert [(ref.source, ref.project_id) for ref in result.items[0].alternatives] == [
            ("modrinth", "123"),
            ("curseforge", "123"),
        ]


def test_same_batch_and_cross_page_duplicates_update_prior_page(service):
    mr = [item("modrinth", str(index), title=f"M{index}", wiki=str(index + 1)) for index in range(40)]
    # 重复的同源项目以及第二批才读到的跨源项目。
    cf = [item("curseforge", str(index + 100), title=f"C{index}") for index in range(20)]
    cf += [item("curseforge", "999", wiki="1"), item("curseforge", "999", wiki="1")]
    calls = []
    fetch = pages(mr, cf, calls)
    first = service.search(SearchCriteria(), fetch, curseforge_available=True)
    second = service.search(SearchCriteria(), fetch, curseforge_available=True, session_id=first.session_id, page=2)
    third = service.search(SearchCriteria(), fetch, curseforge_available=True, session_id=first.session_id, page=3)
    restored = service.search(SearchCriteria(), fetch, curseforge_available=True, session_id=first.session_id, page=1)
    assert [row.group_id for row in first.items] == [row.group_id for row in restored.items]
    assert len({row.group_id for row in first.items + second.items + third.items}) == 60
    assert len(next(row for row in restored.items if row.project_id == "0").alternatives) == 2
    assert ("modrinth", 20) in calls and ("curseforge", 20) in calls
    assert len(calls) == 4


def test_same_provider_different_ids_and_ambiguous_wiki_stay_separate(service):
    mr = [item("modrinth", "a", wiki="1"), item("modrinth", "b", wiki="1")]
    result = service.search(SearchCriteria(), pages(mr, [item("curseforge", "c", wiki="1")]), curseforge_available=True)
    assert result.total == 3


def test_empty_fields_are_not_identity(service):
    mr = item("modrinth", "a").model_copy(update={"title": "", "slug": "", "author": ""})
    cf = item("curseforge", "b").model_copy(update={"title": "", "slug": "", "author": ""})
    assert service.search(SearchCriteria(), pages([mr], [cf]), curseforge_available=True).total == 2


def test_ranking_is_scale_independent_and_ties_interleave(service):
    mr = [item("modrinth", str(index), title=f"M{index}", downloads=10) for index in range(3)]
    cf = [item("curseforge", str(index + 100), title=f"C{index}", downloads=1_000_000) for index in range(3)]
    result = service.search(SearchCriteria(), pages(mr, cf), curseforge_available=True)
    assert [row.source for row in result.items] == ["modrinth", "curseforge"] * 3
    assert [row.project_id for row in result.items] == ["0", "100", "1", "101", "2", "102"]


@pytest.mark.parametrize("query", ["Sodium", "钠"])
def test_exact_name_and_translation_outrank_provider_position(service, query):
    mr = [
        item("modrinth", "noise", title="Other").model_copy(update={"slug": "other"}),
        item("modrinth", "exact", wiki="1"),
    ]
    result = service.search(SearchCriteria(query=query), pages(mr, []), curseforge_available=True)
    assert result.items[0].project_id == "exact"


@pytest.mark.parametrize(("sort", "field"), [("newest", "date_created"), ("updated", "date_modified")])
def test_date_sort_uses_actual_utc_dates_and_missing_last(service, sort, field):
    mr = [
        item("modrinth", "old", title="Old", **{field: "2026-01-02T10:00:00+08:00"}),
        item("modrinth", "missing", title="Missing"),
    ]
    cf = [item("curseforge", "new", title="New", **{field: "2026-01-02T03:00:00Z"})]
    result = service.search(SearchCriteria(sort=sort), pages(mr, cf), curseforge_available=True)
    assert [row.project_id for row in result.items] == ["new", "old", "missing"]


def test_duplicate_heavy_batches_backfill_using_raw_offsets(service):
    mr = [item("modrinth", "repeat")] * 40 + [item("modrinth", str(index), title=f"M{index}") for index in range(20)]
    calls = []
    result = service.search(SearchCriteria(), pages(mr, [], calls), curseforge_available=False)
    assert len(result.items) == 20 and result.has_more
    assert calls == [("modrinth", 0), ("modrinth", 20), ("modrinth", 40)]


def test_budget_short_page_is_stable_and_empty_extension_does_not_make_page(service):
    mr = [item("modrinth", "repeat")] * 100
    fetch = pages(mr, [])
    first = service.search(SearchCriteria(), fetch, curseforge_available=False)
    assert len(first.items) == 1 and first.has_more
    next_result = service.search(
        SearchCriteria(), fetch, curseforge_available=False, session_id=first.session_id, page=2
    )
    assert next_result.page == 1 and not next_result.has_more
    assert len(next_result.items) == 1 and next_result.total_exact


def test_partial_failure_retries_without_advancing_failed_source(service):
    calls = []
    has_failed = False

    def fetch(source, criteria, offset):
        nonlocal has_failed
        calls.append((source, offset))
        if source == "curseforge" and not has_failed:
            has_failed = True
            raise RuntimeError("private network detail")
        return pages([item("modrinth", "m")], [item("curseforge", "c", title="Other")])(source, criteria, offset)

    first = service.search(SearchCriteria(), fetch, curseforge_available=True)
    assert first.sources["curseforge"].error_code == "RESOURCE_SOURCE_UNAVAILABLE"
    assert "private" not in first.sources["curseforge"].error
    second = service.search(SearchCriteria(), fetch, curseforge_available=True, session_id=first.session_id, page=2)
    assert second.items[0].project_id == "c"
    assert calls.count(("curseforge", 0)) == 2 and second.total_exact


def test_both_failure_is_error_and_next_request_recovers(service):
    def broken(*args):
        raise RuntimeError("network")

    with pytest.raises(GameServiceError, match="搜索失败"):
        service.search(SearchCriteria(), broken, curseforge_available=True)
    recovered = service.search(SearchCriteria(), pages([item("modrinth", "m")], []), curseforge_available=True)
    assert recovered.items[0].project_id == "m"


def test_expiration_refresh_filter_mismatch_and_jump(service):
    now = 0.0
    service._clock = lambda: now
    fetch = pages([item("modrinth", "m")], [])
    first = service.search(SearchCriteria(), fetch, curseforge_available=False)
    with pytest.raises(GameServiceError) as mismatch:
        service.search(SearchCriteria(query="other"), fetch, curseforge_available=False, session_id=first.session_id)
    assert mismatch.value.error_code == "SEARCH_SESSION_MISMATCH"
    with pytest.raises(GameServiceError) as jump:
        service.search(SearchCriteria(), fetch, curseforge_available=False, session_id=first.session_id, page=3)
    assert jump.value.error_code == "INVALID_SEARCH_PAGE"
    refreshed = service.search(SearchCriteria(), fetch, curseforge_available=False, refresh=True)
    assert refreshed.session_id != first.session_id
    now = 601
    with pytest.raises(GameServiceError) as expired:
        service.search(SearchCriteria(), fetch, curseforge_available=False, session_id=first.session_id)
    assert expired.value.error_code == "SEARCH_SESSION_EXPIRED"


def test_caps_and_lru(service):
    service.max_groups = 2
    service.max_sessions = 1
    fetch = pages([item("modrinth", str(index), title=f"M{index}") for index in range(4)], [])
    first = service.search(SearchCriteria(), fetch, curseforge_available=False)
    assert first.total == 2 and first.truncated and not first.total_exact and not first.has_more
    service.search(SearchCriteria(query="new"), fetch, curseforge_available=False)
    with pytest.raises(GameServiceError) as expired:
        service.search(SearchCriteria(), fetch, curseforge_available=False, session_id=first.session_id)
    assert expired.value.error_code == "SEARCH_SESSION_EXPIRED"


def test_platforms_run_concurrently_and_same_session_reuses_read(service):
    barrier = Barrier(2)
    entered = Event()
    release = Event()
    calls = []
    lock = Lock()

    def fetch(source, criteria, offset):
        with lock:
            calls.append(source)
        barrier.wait(timeout=5)
        entered.set()
        assert release.wait(timeout=5)
        return SearchBatch(items=[item(source, source, title=source)], raw_count=1, total=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.search, SearchCriteria(), fetch, curseforge_available=True)
        assert entered.wait(timeout=5)
        second = executor.submit(service.search, SearchCriteria(), fetch, curseforge_available=True)
        release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert a.session_id == b.session_id and len(calls) == 2


def test_close_rejects_new_requests(service):
    service.close()
    with pytest.raises(GameServiceError) as error:
        service.search(SearchCriteria(), pages([], []), curseforge_available=True)
    assert error.value.error_code == "SEARCH_CLOSED"


def test_four_platform_requests_are_the_concurrency_limit(service):
    barrier = Barrier(4)
    active = 0
    maximum = 0
    lock = Lock()

    def fetch(source, criteria, offset):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return SearchBatch(items=[], raw_count=0, total=0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [
            executor.submit(service.search, SearchCriteria(query=query), fetch, curseforge_available=True)
            for query in ("a", "b")
        ]
        for task in tasks:
            assert task.result(timeout=5).total_exact
    assert maximum == 4


def test_cached_results_are_copies_and_refresh_preserves_other_consumers(service):
    fetch = pages([item("modrinth", "m")], [])
    first = service.search(SearchCriteria(), fetch, curseforge_available=False)
    first.items[0].title = "caller mutation"
    fresh = service.search(SearchCriteria(), fetch, curseforge_available=False, refresh=True)
    restored = service.search(SearchCriteria(), fetch, curseforge_available=False, session_id=first.session_id)
    assert restored.items[0].title == "Sodium"
    assert fresh.session_id != restored.session_id


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "unknown"},
        {"page": 0},
        {"limit": 51},
        {"source": "all", "offset": 20},
        {"source": "all", "resource_type": "world"},
        {"source": "ftb", "resource_type": "mod"},
        {"refresh": True, "page": 2},
        {"source": "all", "limit": 10},
    ],
)
def test_request_rejects_invalid_modes(payload):
    with pytest.raises(ValueError):
        OnlineResourceSearchRequest.model_validate(payload)
