# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 updates 模块的自动化测试。
#
# 公开接口：
#   - class FakeHttp
#       - get(url, **kwargs) -> httpx.Response
#   - test_compare_versions(left, right, expected) -> None
#   - test_parse_version_returns_none_for_invalid_input() -> None
#   - test_alpha_channel_considers_all_release_types() -> None
#   - test_alpha_channel_up_to_date_when_current_is_global_latest() -> None
#   - test_beta_channel_finds_newer_prerelease() -> None
#   - test_beta_channel_ignores_older_or_duplicate_prereleases() -> None
#   - test_release_channel_only_considers_stable_releases() -> None
#   - test_release_channel_finds_newer_stable_release() -> None
#   - test_rc_channel_uses_prerelease_channel() -> None
#   - test_empty_release_list_means_up_to_date() -> None
#   - test_network_error_returns_friendly_message() -> None
#   - test_http_error_status_returns_friendly_message() -> None
#   - test_invalid_json_returns_friendly_message() -> None
# ============================================================

import json
from datetime import UTC, datetime
from threading import Event

import httpx
import pytest

from ECL.foundation import EventBus
from ECL.services.updates import (
    AnnouncementService,
    StartupUpdateService,
    UpdateChecker,
    UpdateCheckResult,
    compare_versions,
    parse_version,
)


def _response(status_code: int = 200, *, json: object = None) -> httpx.Response:
    return httpx.Response(status_code, json=json)


class FakeHttp:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("测试未配置响应")
        return self.response


def _release(tag: str, *, prerelease: bool = False, body: str = "更新说明") -> dict:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "html_url": f"https://github.com/ECLTeam/EuoraCraft-Launcher/releases/tag/{tag}",
        "body": body,
    }


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.4.2", "1.4.2", 0),
        ("v1.4.2", "1.4.2", 0),
        ("1.4.3", "1.4.2", 1),
        ("1.4.2", "1.4.3", -1),
        ("1.10.0", "1.9.9", 1),
        ("1.4.2-alpha.1", "1.4.2", -1),
        ("1.4.2", "1.4.2-alpha.1", 1),
        ("1.4.2-alpha.1", "1.4.2-alpha.2", -1),
        ("1.4.2-alpha.2", "1.4.2-beta.1", -1),
        ("1.4.2-beta.1", "1.4.2-rc.1", -1),
        ("1.4.2-rc.1", "1.4.2", -1),
        ("1.4.2-alpha.1+20260906", "1.4.2-alpha.1+20260907", 0),
        ("1.4.2-beta.2+20260906", "1.4.2-beta.1+20260906", 1),
        ("不是版本", "1.4.2", 0),
        ("1.4.2", "不是版本", 0),
    ],
)
def test_compare_versions(left: str, right: str, expected: int) -> None:
    result = compare_versions(left, right)
    assert (result > 0) - (result < 0) == expected


def test_parse_version_returns_none_for_invalid_input() -> None:
    assert parse_version("") is None
    assert parse_version("v") is None
    assert parse_version("1.4.2-alpha.one") is None
    assert parse_version("1.4.2-alpha.") is None


def test_alpha_channel_considers_all_release_types() -> None:
    http = FakeHttp(
        _response(
            json=[
                _release("v1.4.2-alpha.1", prerelease=True),
                _release("v1.4.2-beta.1", prerelease=True),
                _release("v1.4.2", prerelease=False, body="正式版"),
            ]
        )
    )

    result = UpdateChecker(
        http,
        current_version="1.4.2-alpha.3+20260906",
        version_type="alpha",
    ).check()

    assert result.status == "update_available"
    assert result.channel == "alpha"
    assert result.latest_version == "1.4.2"
    assert result.latest_notes == "正式版"
    assert http.calls


def test_alpha_channel_up_to_date_when_current_is_global_latest() -> None:
    http = FakeHttp(
        _response(
            json=[
                _release("v1.4.2-alpha.1", prerelease=True),
                _release("v1.4.2-beta.1", prerelease=True),
            ]
        )
    )

    result = UpdateChecker(
        http,
        current_version="1.4.2-beta.1+20260906",
        version_type="alpha",
    ).check()

    assert result.status == "up_to_date"


def test_beta_channel_finds_newer_prerelease() -> None:
    http = FakeHttp(
        _response(
            json=[
                _release("v1.4.2", prerelease=False),
                _release("v1.4.2-alpha.1", prerelease=True),
                _release("v1.4.2-beta.2+20260906", prerelease=True, body="Beta 修复"),
            ]
        )
    )

    result = UpdateChecker(
        http,
        current_version="1.4.2-beta.1+20260905",
        version_type="beta",
    ).check()

    assert result.status == "update_available"
    assert result.channel == "beta"
    assert result.latest_version == "1.4.2-beta.2+20260906"
    assert result.latest_url.endswith("v1.4.2-beta.2+20260906")
    assert result.latest_notes == "Beta 修复"


def test_beta_channel_ignores_older_or_duplicate_prereleases() -> None:
    http = FakeHttp(
        _response(
            json=[
                _release("v1.4.2-beta.1+20260905", prerelease=True),
                _release("v1.4.2-beta.2+20260906", prerelease=True),
                _release("v1.4.2-beta.3", prerelease=True),
            ]
        )
    )

    result = UpdateChecker(
        http,
        current_version="1.4.2-beta.3+20260907",
        version_type="beta",
    ).check()

    assert result.status == "up_to_date"
    assert result.latest_version is None


def test_release_channel_only_considers_stable_releases() -> None:
    http = FakeHttp(
        _response(
            json=[
                _release("v1.4.3-beta.1", prerelease=True),
                _release("v1.4.3-alpha.2", prerelease=True),
                _release("v1.4.2", prerelease=False),
            ]
        )
    )

    result = UpdateChecker(
        http,
        current_version="1.4.2",
        version_type="release",
    ).check()

    assert result.status == "up_to_date"


def test_release_channel_finds_newer_stable_release() -> None:
    http = FakeHttp(
        _response(
            json=[
                _release("v1.4.3-beta.1", prerelease=True),
                _release("v1.4.3", prerelease=False, body="正式版"),
            ]
        )
    )

    result = UpdateChecker(
        http,
        current_version="1.4.2",
        version_type="release",
    ).check()

    assert result.status == "update_available"
    assert result.latest_version == "1.4.3"
    assert result.latest_notes == "正式版"


def test_rc_channel_uses_prerelease_channel() -> None:
    http = FakeHttp(_response(json=[_release("v1.4.2-rc.2+20260906", prerelease=True)]))

    result = UpdateChecker(
        http,
        current_version="1.4.2-rc.1+20260905",
        version_type="rc",
    ).check()

    assert result.status == "update_available"
    assert result.channel == "beta"


def test_empty_release_list_means_up_to_date() -> None:
    http = FakeHttp(_response(json=[]))

    result = UpdateChecker(
        http,
        current_version="1.4.2",
        version_type="release",
    ).check()

    assert result.status == "up_to_date"


def test_network_error_returns_friendly_message() -> None:
    http = FakeHttp(error=httpx.ConnectError("offline"))

    result = UpdateChecker(
        http,
        current_version="1.4.2",
        version_type="release",
    ).check()

    assert result.status == "error"
    assert result.message == "无法连接更新服务器，请检查网络后重试"


def test_http_error_status_returns_friendly_message() -> None:
    http = FakeHttp(_response(403))

    result = UpdateChecker(
        http,
        current_version="1.4.2",
        version_type="release",
    ).check()

    assert result.status == "error"
    assert result.message == "更新服务器响应异常，请稍后重试"


def test_invalid_json_returns_friendly_message() -> None:
    http = FakeHttp(httpx.Response(200, content=b"not-json"))

    result = UpdateChecker(
        http,
        current_version="1.4.2",
        version_type="release",
    ).check()

    assert result.status == "error"
    assert result.message == "更新服务器数据解析失败，请稍后重试"


def test_startup_update_service_checks_once_and_caches_frontend_result() -> None:
    """
    启动更新服务只发起一次检测，并把结果缓存后广播给前端事件桥。

    重复 ``start`` 模拟前端多次刷新或重复就绪，不能再次调用检测器。
    """

    calls: list[str] = []
    completed = Event()
    received: list[dict[str, object]] = []
    events = EventBus()
    events.subscribe(
        "update:check_completed",
        lambda payload: (received.append(payload), completed.set()),
    )

    class FakeChecker:
        def check(self) -> UpdateCheckResult:
            calls.append("check")
            return UpdateCheckResult(
                status="update_available",
                current_version="1.0.0",
                channel="release",
                latest_version="1.1.0",
            )

    service = StartupUpdateService(
        http_client=None,  # type: ignore[arg-type]  # 测试通过 checker_factory 替换 HTTP 请求。
        event_bus=events,
        current_version="1.0.0",
        version_type="release",
        checker_factory=FakeChecker,
    )

    assert service.start() is True
    assert service.start() is False
    assert completed.wait(timeout=1)
    assert calls == ["check"]
    assert received == [
        {
            "status": "update_available",
            "current_version": "1.0.0",
            "channel": "release",
            "latest_version": "1.1.0",
            "latest_url": None,
            "latest_notes": None,
            "message": None,
        }
    ]
    assert service.result() == received[0]


@pytest.fixture
def announcement_http_client():
    def reject_unexpected_network(request):
        raise AssertionError(f"未配置的公告请求：{request.url}")

    with httpx.Client(transport=httpx.MockTransport(reject_unexpected_network)) as client:
        yield client


now = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _notice_payload(*announcements):
    return {
        "schema_version": 1,
        "updated_at": "2026-07-30T20:00:00+08:00",
        "announcements": list(announcements),
    }


def _announcement(
    notice_id: str = "maintenance-20260730",
    *,
    title: str = "内部测试开发",
    priority: int = 100,
    start_at: str = "2026-07-30T00:00:00+08:00",
    end_at: str | None = "2026-08-02T00:00:00+08:00",
):
    return {
        "id": notice_id,
        "title": title,
        "date": "2026-07-30",
        "content": "**本条为测试公告，仅用于内部测试**",
        "enabled": True,
        "priority": priority,
        "start_at": start_at,
        "end_at": end_at,
    }


def test_announcements_returns_only_remote_content(tmp_path, announcement_http_client) -> None:
    payload = _notice_payload(_announcement())
    manager = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=lambda _url: payload, clock=lambda: now
    )

    data = manager.get_announcements()

    assert data == [
        {
            "id": "maintenance-20260730",
            "title": "内部测试开发",
            "date": "2026-07-30",
            "content": "**本条为测试公告，仅用于内部测试**",
        }
    ]
    assert json.loads(manager.notice_cache_path.read_text(encoding="utf-8")) == payload


def test_announcements_filters_dates_disabled_items_duplicates_and_sorts_priority(
    tmp_path, announcement_http_client
) -> None:
    disabled = {**_announcement("disabled"), "enabled": False}
    payload = _notice_payload(
        _announcement("normal", title="普通公告", priority=10),
        _announcement("important", title="重要公告", priority=100),
        disabled,
        _announcement("future", start_at="2026-08-03T00:00:00+08:00"),
        _announcement("expired", end_at="2026-07-30T11:00:00+00:00"),
        _announcement("important", title="重复公告", priority=200),
        {"id": "", "title": "无效公告", "content": "缺少有效 ID"},
    )
    manager = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=lambda _url: payload, clock=lambda: now
    )

    data = manager.get_announcements()

    assert [item["id"] for item in data] == ["important", "normal"]


def test_announcements_permanent_announcement_remains_available_in_cache(tmp_path, announcement_http_client) -> None:
    payload = _notice_payload(_announcement(end_at=None))
    online = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=lambda _url: payload, clock=lambda: now
    )

    assert [item["id"] for item in online.get_announcements()] == ["maintenance-20260730"]
    assert json.loads(online.notice_cache_path.read_text(encoding="utf-8"))["announcements"][0]["end_at"] is None

    def offline(_url):
        raise httpx.ConnectError("offline")

    later = datetime(2030, 1, 1, tzinfo=UTC)
    cached = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=offline, clock=lambda: later
    )

    assert [item["id"] for item in cached.get_announcements()] == ["maintenance-20260730"]


@pytest.mark.parametrize("end_at", ["null", "9999-99-99T99:99:99+99:99"])
def test_announcements_filters_invalid_end_time_without_hiding_valid_announcements(
    tmp_path, announcement_http_client, end_at
) -> None:
    payload = _notice_payload(_announcement("invalid", end_at=end_at), _announcement("valid", end_at=None))
    manager = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=lambda _url: payload, clock=lambda: now
    )

    assert [item["id"] for item in manager.get_announcements()] == ["valid"]


def test_announcements_fetches_announcements_from_euora_api(tmp_path, announcement_http_client) -> None:
    payload = _notice_payload(_announcement(end_at=None))
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manager = AnnouncementService(tmp_path, http_client=client, clock=lambda: now)
        assert len(manager.get_announcements()) == 1

    assert requested_urls == ["https://api.euora.cn/raw/ECLTeam/EuoraCraft-Launcher.API/main/notice.json"]


def test_announcements_preserves_valid_remote_announcement_translations(tmp_path, announcement_http_client) -> None:
    payload = _notice_payload(
        {
            **_announcement(),
            "locales": {
                "zh-CN": {"title": "中文公告", "content": "中文正文"},
                "en-US": {"title": "English notice", "content": "English content"},
                "ja-JP": {"title": "不完整翻译"},
                "": {"title": "无效语言", "content": "无效正文"},
            },
        }
    )
    manager = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=lambda _url: payload, clock=lambda: now
    )

    announcement = manager.get_announcements()[0]

    assert announcement["title"] == "内部测试开发"
    assert announcement["content"] == "**本条为测试公告，仅用于内部测试**"
    assert announcement["locales"] == {
        "zh-CN": {"title": "中文公告", "content": "中文正文"},
        "en-US": {"title": "English notice", "content": "English content"},
    }


def test_announcements_uses_last_valid_cache_when_remote_request_fails(tmp_path, announcement_http_client) -> None:
    payload = _notice_payload(_announcement())
    online = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=lambda _url: payload, clock=lambda: now
    )
    online.get_announcements()

    def offline(_url):
        raise httpx.ConnectError("offline")

    offline_manager = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=offline, clock=lambda: now
    )

    assert offline_manager.get_announcements()[0]["id"] == "maintenance-20260730"


def test_announcements_returns_empty_announcements_when_remote_and_cache_are_invalid(
    tmp_path, announcement_http_client
) -> None:
    (tmp_path / "notice.json").write_text("{invalid", encoding="utf-8")
    manager = AnnouncementService(
        tmp_path,
        http_client=announcement_http_client,
        notice_loader=lambda _url: {"schema_version": 99, "announcements": []},
        clock=lambda: now,
    )

    data = manager.get_announcements()

    assert data == []


def test_announcements_does_not_refetch_within_refresh_window(tmp_path, announcement_http_client) -> None:
    calls = 0

    def load_notice(_url):
        nonlocal calls
        calls += 1
        return _notice_payload(_announcement())

    manager = AnnouncementService(
        tmp_path, http_client=announcement_http_client, notice_loader=load_notice, clock=lambda: now
    )

    manager.get_announcements()
    manager.get_announcements()

    assert calls == 1


def test_announcements_injected_client_does_not_pass_verify_per_request(tmp_path, announcement_http_client) -> None:
    payload = _notice_payload(_announcement())

    def handler(request: httpx.Request) -> httpx.Response:
        # 断言请求未把 verify 作为请求参数携带（否则 httpx 会在构造时报 TypeError）
        assert "verify" not in request.extensions
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        manager = AnnouncementService(tmp_path, http_client=client, clock=lambda: now)
        data = manager._download_notice("https://example.test/notice.json")

    assert data == payload
