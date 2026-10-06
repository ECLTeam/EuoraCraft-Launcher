# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 info_card 模块的自动化测试。
#
# 公开接口：
#   - now（固定时钟）
#   - test_info_card_uses_backend_content_and_remote_announcements(tmp_path) -> None
#   - test_info_card_filters_dates_disabled_items_duplicates_and_sorts_priority(tmp_path) -> None
#   - test_info_card_uses_last_valid_cache_when_remote_request_fails(tmp_path) -> None
#   - test_info_card_returns_empty_announcements_when_remote_and_cache_are_invalid(tmp_path) -> None
#   - test_info_card_does_not_refetch_within_refresh_window(tmp_path) -> None
#   - test_info_card_injected_client_does_not_pass_verify_per_request(tmp_path) -> None
# ============================================================

import json
from datetime import UTC, datetime

import httpx

from ECL.host import InfoCardManager

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
    end_at: str = "2026-08-02T00:00:00+08:00",
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


def test_info_card_uses_backend_content_and_remote_announcements(tmp_path) -> None:
    payload = _notice_payload(_announcement())
    manager = InfoCardManager(tmp_path, notice_loader=lambda _url: payload, clock=lambda: now)

    data = manager.get_info_card()

    assert data["tip_title"] == "你知道吗"
    assert data["announcement_title"] == "公告"
    assert data["tips"]
    assert data["welcome"] == {
        "title": "欢迎使用 EuoraCraft Launcher",
        "content": "选择账户和游戏版本后即可开始游戏。",
    }
    assert data["announcements"] == [
        {
            "id": "maintenance-20260730",
            "title": "内部测试开发",
            "date": "2026-07-30",
            "content": "**本条为测试公告，仅用于内部测试**",
        }
    ]
    assert json.loads(manager.notice_cache_path.read_text(encoding="utf-8")) == payload


def test_info_card_filters_dates_disabled_items_duplicates_and_sorts_priority(tmp_path) -> None:
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
    manager = InfoCardManager(tmp_path, notice_loader=lambda _url: payload, clock=lambda: now)

    data = manager.get_info_card()

    assert [item["id"] for item in data["announcements"]] == ["important", "normal"]


def test_info_card_preserves_valid_remote_announcement_translations(tmp_path) -> None:
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
    manager = InfoCardManager(tmp_path, notice_loader=lambda _url: payload, clock=lambda: now)

    announcement = manager.get_info_card()["announcements"][0]

    assert announcement["title"] == "内部测试开发"
    assert announcement["content"] == "**本条为测试公告，仅用于内部测试**"
    assert announcement["locales"] == {
        "zh-CN": {"title": "中文公告", "content": "中文正文"},
        "en-US": {"title": "English notice", "content": "English content"},
    }


def test_info_card_uses_last_valid_cache_when_remote_request_fails(tmp_path) -> None:
    payload = _notice_payload(_announcement())
    online = InfoCardManager(tmp_path, notice_loader=lambda _url: payload, clock=lambda: now)
    online.get_info_card()

    def offline(_url):
        raise httpx.ConnectError("offline")

    offline_manager = InfoCardManager(tmp_path, notice_loader=offline, clock=lambda: now)

    assert offline_manager.get_info_card()["announcements"][0]["id"] == "maintenance-20260730"


def test_info_card_returns_empty_announcements_when_remote_and_cache_are_invalid(tmp_path) -> None:
    (tmp_path / "notice.json").write_text("{invalid", encoding="utf-8")
    manager = InfoCardManager(
        tmp_path,
        notice_loader=lambda _url: {"schema_version": 99, "announcements": []},
        clock=lambda: now,
    )

    data = manager.get_info_card()

    assert data["announcements"] == []
    assert data["tips"]


def test_info_card_does_not_refetch_within_refresh_window(tmp_path) -> None:
    calls = 0

    def load_notice(_url):
        nonlocal calls
        calls += 1
        return _notice_payload(_announcement())

    manager = InfoCardManager(tmp_path, notice_loader=load_notice, clock=lambda: now)

    manager.get_info_card()
    manager.get_info_card()

    assert calls == 1


def test_info_card_injected_client_does_not_pass_verify_per_request(tmp_path) -> None:
    payload = _notice_payload(_announcement())

    def handler(request: httpx.Request) -> httpx.Response:
        # 断言请求未把 verify 作为请求参数携带（否则 httpx 会在构造时报 TypeError）
        assert "verify" not in request.extensions
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    manager = InfoCardManager(tmp_path, http_client=client, clock=lambda: now)

    data = manager._download_notice("https://example.test/notice.json")

    assert data == payload
