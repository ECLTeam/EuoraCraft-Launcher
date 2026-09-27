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

from threading import Event

import httpx
import pytest

from ECL.events import EventBus
from ECL.services.updates import StartupUpdateService, UpdateChecker, UpdateCheckResult, compare_versions, parse_version


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
        http_client=None,  # type: ignore[arg-type]  # 测试使用 checker_factory 隔离 HTTP 边界。
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
