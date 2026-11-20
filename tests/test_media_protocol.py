# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 ecl-media 媒体协议服务与 background_video_open 命令的自动化测试。
#
# 公开接口：
#   - test_register_local_signs_url_and_serves_full_body(tmp_path) -> None
#   - test_serve_returns_206_slice_for_range(tmp_path) -> None
#   - test_serve_unsatisfiable_range_returns_416(tmp_path) -> None
#   - test_serve_preflight_returns_allowed_headers(tmp_path) -> None
#   - test_serve_head_returns_headers_without_body(tmp_path) -> None
#   - test_serve_rejects_invalid_and_unknown_tokens(tmp_path) -> None
#   - test_multiple_registrations_keep_independent_tokens(tmp_path) -> None
#   - test_registration_reuses_token_for_same_path(tmp_path) -> None
#   - test_registration_evicts_oldest_token_at_capacity(tmp_path) -> None
#   - test_size_change_returns_410(tmp_path) -> None
#   - test_register_local_rejects_unsupported_or_missing(tmp_path) -> None
#   - test_close_and_revoke_clear_tokens(tmp_path) -> None
#   - test_background_video_open_signs_protocol_url(tmp_path) -> None
# ============================================================

from types import SimpleNamespace

import pytest

from ECL.api.media_protocol import MediaProtocolService

FRONTEND_API = None


def _make_service_with_file(tmp_path, content: bytes, name: str = "bg.mp4") -> tuple[MediaProtocolService, str, object]:
    service = MediaProtocolService()
    target = tmp_path / name
    target.write_bytes(content)
    url = service.register_local(str(target))
    return service, url, target


def test_register_local_signs_url_and_serves_full_body(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")
    assert url.startswith("http://ecl-media.localhost/") or url.startswith("ecl-media://localhost/")
    token = url.rstrip("/").rsplit("/", 1)[-1]

    response = service.serve(url, "GET")

    assert response.status == 200
    assert response.body == b"0123456789"
    assert ("content-type", "video/mp4") in response.headers
    assert ("accept-ranges", "bytes") in response.headers
    assert ("access-control-allow-origin", "*") in response.headers
    assert ("access-control-expose-headers", "etag, accept-ranges, content-range") in response.headers
    assert ("cache-control", "no-store") in response.headers
    assert token in url


def test_serve_returns_206_slice_for_range(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, bytes(range(256)) * 4)

    response = service.serve(url, "GET", {"range": "bytes=100-199"})

    assert response.status == 206
    assert response.body == bytes(range(256))[100:200]
    content_range = dict(response.headers)["content-range"]
    assert content_range == f"bytes 100-199/{256 * 4}"


def test_serve_suffix_range_returns_tail(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")

    response = service.serve(url, "GET", {"range": "bytes=-4"})

    assert response.status == 206
    assert response.body == b"6789"


def test_serve_unsatisfiable_range_returns_416(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")

    response = service.serve(url, "GET", {"range": "bytes=100-"})

    assert response.status == 416
    assert dict(response.headers)["content-range"] == "bytes */10"


def test_serve_preflight_returns_allowed_headers(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")

    response = service.serve(url, "OPTIONS", {"access-control-request-headers": "if-range"})

    assert response.status == 204
    headers = dict(response.headers)
    assert headers["access-control-allow-origin"] == "*"
    assert headers["access-control-allow-headers"] == "if-range"
    assert "GET" in headers["access-control-allow-methods"]


def test_serve_preflight_default_allows_range_and_if_range(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")

    response = service.serve(url, "OPTIONS")

    assert dict(response.headers)["access-control-allow-headers"] == "range, if-range"


def test_serve_head_returns_headers_without_body(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")

    response = service.serve(url, "HEAD")

    assert response.status == 200
    assert response.body == b""
    assert dict(response.headers)["content-length"] == "10"
    assert dict(response.headers)["content-type"] == "video/mp4"


def test_serve_rejects_invalid_and_unknown_tokens(tmp_path) -> None:
    service, _url, _target = _make_service_with_file(tmp_path, b"0123456789")
    unknown = f"{MediaProtocolService.origin()}/{'a' * 24}/bg.mp4"
    for bad_uri in (
        "",
        "ecl-media://localhost/",
        "ecl-media://localhost/short",
        unknown,
        "ecl-media://local/../../etc",
    ):
        response = service.serve(bad_uri, "GET")
        assert response.status == 403, bad_uri


def test_serve_rejects_unexpected_methods(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")

    response = service.serve(url, "POST")

    assert response.status == 405


def test_multiple_registrations_keep_independent_tokens(tmp_path) -> None:
    service = MediaProtocolService()
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_url = service.register_local(str(first))
    second_url = service.register_local(str(second))

    assert service.serve(first_url, "GET").body == b"first"
    assert service.serve(second_url, "GET").body == b"second"


def test_registration_reuses_token_for_same_path(tmp_path) -> None:
    service = MediaProtocolService()
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"clip")

    first_url = service.register_local(str(target))
    second_url = service.register_local(str(target))

    assert first_url == second_url
    assert len(service._bindings) == 1


def test_registration_evicts_oldest_token_at_capacity(tmp_path) -> None:
    service = MediaProtocolService()
    urls: list[str] = []
    for index in range(service.max_tokens + 1):
        target = tmp_path / f"clip{index}.mp4"
        target.write_bytes(f"clip-{index}".encode())
        urls.append(service.register_local(str(target)))

    assert len(service._bindings) == service.max_tokens
    # 最早签发的地址在容量达到上限后被淘汰，剩余地址仍可访问。
    assert service.serve(urls[0], "GET").status == 403
    assert service.serve(urls[-1], "GET").status == 200


def test_size_change_returns_410(tmp_path) -> None:
    service, url, target = _make_service_with_file(tmp_path, b"0123456789")
    target.write_bytes(b"changed!!")

    response = service.serve(url, "GET")

    assert response.status == 410


def test_register_local_rejects_unsupported_or_missing(tmp_path) -> None:
    service = MediaProtocolService()
    txt = tmp_path / "note.txt"
    txt.write_bytes(b"text")
    with pytest.raises(ValueError):
        service.register_local(str(txt))
    with pytest.raises(OSError):
        service.register_local(str(tmp_path / "missing.mp4"))
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        service.register_local(str(empty))


def test_close_and_revoke_clear_tokens(tmp_path) -> None:
    service, url, _target = _make_service_with_file(tmp_path, b"0123456789")
    service.revoke()
    assert service.serve(url, "GET").status == 403
    service2, url2, _t = _make_service_with_file(tmp_path, b"0123456789", name="bg2.mp4")
    service2.close()
    assert service2.serve(url2, "GET").status == 403


def test_background_video_open_signs_protocol_url(tmp_path, monkeypatch) -> None:
    from ECL.api.files import FileHandlers

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"vid")
    handler = object.__new__(FileHandlers)
    handler.config = SimpleNamespace(
        get_config=lambda section: {"background": {"media_type": "video", "video": {"path": str(video)}}}
    )
    handler.logger = __import__("logging").getLogger("test.media")
    from ECL.utils import get_logger

    handler.logger = get_logger("test.media")
    handler.media_protocol = MediaProtocolService()

    async def fake_run_sync(func, *args):
        return func(*args)

    monkeypatch.setattr("ECL.api.files.to_thread.run_sync", fake_run_sync)

    import asyncio

    result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(handler.background_video_open({}))

    assert result["success"] is True
    assert "ecl-media" in result["data"]["url"]
