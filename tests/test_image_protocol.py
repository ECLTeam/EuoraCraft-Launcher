# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 ecl-img 图片协议服务与 image_url_resolve 命令的自动化测试。
#
# 公开接口：
#   - _build_api(tmp_path) -> FrontendApi — 构造带临时数据目录的前端 API 实例。
#   - test_serve_returns_bound_file_bytes_with_headers() -> None
#   - test_serve_conditional_request_returns_304() -> None
#   - test_serve_head_returns_headers_without_body() -> None
#   - test_serve_rejects_unexpected_methods() -> None
#   - test_serve_rejects_invalid_and_unknown_tokens() -> None
#   - test_expired_token_is_rejected() -> None
#   - test_close_clears_all_tokens() -> None
#   - test_register_local_rejects_unsupported_extension() -> None
#   - test_oversized_file_is_rejected(tmp_path, monkeypatch) -> None
#   - test_token_capacity_evicts_earliest_expiry(tmp_path) -> None
#   - test_remote_cache_binding_serves_cached_bytes(tmp_path) -> None
#   - test_missing_bound_file_returns_404(tmp_path) -> None
#   - test_url_resolve_rejects_kind_without_source(tmp_path) -> None
#   - test_url_resolve_local_missing_file(tmp_path) -> None
#   - test_url_resolve_local_rejects_unsupported_extension(tmp_path) -> None
#   - test_url_resolve_local_rejects_symlink(tmp_path) -> None
#   - test_url_resolve_local_signs_url_that_serves_bytes(tmp_path) -> None
#   - test_url_resolve_remote_rejects_invalid_url(tmp_path) -> None
#   - test_url_resolve_remote_signs_url_after_download(tmp_path, monkeypatch) -> None
#   - test_url_resolve_remote_rejects_requests_after_closing(tmp_path) -> None
# ============================================================

import asyncio
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from ECL.api.bridge import ImagePolicy
from ECL.api.image_protocol import ImageProtocolService

FrontendApi = import_module("ECL.api").FrontendApi
files_module = import_module("ECL.api.files")
ConfigStore = import_module("ECL.utils.config").ConfigStore
EventBus = import_module("ECL.utils").EventBus


def _build_api(tmp_path):
    launcher = SimpleNamespace(
        app_path=tmp_path,
        data_path=tmp_path / "ECL_data",
        config={"launcher": {"debug": False}},
        debug=False,
        launcher_version="1.4.2-alpha.3+20260906",
        launcher_version_type="alpha",
    )
    bus = EventBus()
    context = SimpleNamespace(
        state=launcher,
        events=bus,
        config=ConfigStore(tmp_path / "ECL_data", bus),
        http=SimpleNamespace(),
        accounts=SimpleNamespace(),
        connector=SimpleNamespace(),
        wardrobe=SimpleNamespace(),
        announcements=SimpleNamespace(),
        game=SimpleNamespace(),
        plugins=SimpleNamespace(list_plugins=lambda: [], on_frontend_ready=lambda: None),
        processes=SimpleNamespace(),
    )
    return FrontendApi(context)


def _make_service_with_file(tmp_path, content: bytes, name: str = "logo.png") -> tuple[ImageProtocolService, str]:
    service = ImageProtocolService()
    target = tmp_path / name
    target.write_bytes(content)
    url = service.register_local(target)
    return service, url


def _header_values(response, name: str) -> list[str]:
    return [value for header, value in response.headers if header.lower() == name]


def test_serve_returns_bound_file_bytes_with_headers(tmp_path) -> None:
    content = b"\x89PNG fake bytes"
    service, url = _make_service_with_file(tmp_path, content)

    response = service.serve(url, "GET", None)

    assert response.status == 200
    assert response.body == content
    assert _header_values(response, "content-type") == ["image/png"]
    assert _header_values(response, "cache-control") == ["no-cache"]
    assert _header_values(response, "access-control-allow-origin") == ["*"]
    etag = _header_values(response, "etag")
    assert len(etag) == 1 and etag[0].startswith('"img-')


def test_serve_conditional_request_returns_304(tmp_path) -> None:
    service, url = _make_service_with_file(tmp_path, b"payload")
    etag = _header_values(service.serve(url, "GET", None), "etag")[0]

    matched = service.serve(url, "GET", etag)
    weak = service.serve(url, "GET", f"W/{etag}")
    listed = service.serve(url, "GET", f'"other", {etag}')

    assert matched.status == 304 and matched.body == b""
    assert weak.status == 304
    assert listed.status == 304
    assert _header_values(matched, "etag") == [etag]
    mismatched = service.serve(url, "GET", '"img-different"')
    assert mismatched.status == 200 and mismatched.body == b"payload"


def test_serve_head_returns_headers_without_body(tmp_path) -> None:
    service, url = _make_service_with_file(tmp_path, b"12345678")

    response = service.serve(url, "HEAD", None)

    assert response.status == 200
    assert response.body == b""
    assert _header_values(response, "content-length") == ["8"]


def test_serve_rejects_unexpected_methods(tmp_path) -> None:
    service, url = _make_service_with_file(tmp_path, b"payload")

    assert service.serve(url, "POST", None).status == 405
    assert service.serve(url, "DELETE", None).status == 405


def test_serve_rejects_invalid_and_unknown_tokens() -> None:
    service = ImageProtocolService()

    for uri in [
        f"{service.origin()}/short",
        f"{service.origin()}/{'a' * 200}/x.png",
        f"{service.origin()}/../etc/passwd",
        f"{service.origin()}/{'x' * 22}",
        "not-a-uri",
    ]:
        assert service.serve(uri, "GET", None).status == 403


def test_expired_token_is_rejected(tmp_path) -> None:
    service, url = _make_service_with_file(tmp_path, b"payload")
    token = url.split("/", 3)[3].split("/")[0]
    with service._lock:
        binding = service._bindings[token]
        service._bindings[token] = type(binding)(file_path=binding.file_path, mime=binding.mime, expires_at=0.0)

    assert service.serve(url, "GET", None).status == 403
    assert token not in service._bindings


def test_close_clears_all_tokens(tmp_path) -> None:
    service, url = _make_service_with_file(tmp_path, b"payload")

    service.close()

    assert service._bindings == {}
    assert service.serve(url, "GET", None).status == 403


def test_register_local_rejects_unsupported_extension(tmp_path) -> None:
    service = ImageProtocolService()
    target = tmp_path / "notes.txt"
    target.write_bytes(b"text")

    with pytest.raises(ValueError):
        service.register_local(target)


def test_oversized_file_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ImagePolicy, "max_remote_image_bytes", 4)
    service, url = _make_service_with_file(tmp_path, b"123456789")

    assert service.serve(url, "GET", None).status == 413


def test_token_capacity_evicts_earliest_expiry(tmp_path) -> None:
    service = ImageProtocolService()
    service.max_tokens = 1
    first_path = tmp_path / "first.png"
    first_path.write_bytes(b"first")
    second_path = tmp_path / "second.png"
    second_path.write_bytes(b"second")
    first_url = service.register_local(first_path)
    second_url = service.register_local(second_path)

    assert service.serve(first_url, "GET", None).status == 403
    assert service.serve(second_url, "GET", None).status == 200


def test_remote_cache_binding_serves_cached_bytes(tmp_path) -> None:
    service = ImageProtocolService()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    digest = "a" * 64
    (cache_dir / f"{digest}.webp").write_bytes(b"webp-bytes")

    url = service.register_remote_cache(digest, ".webp", cache_dir)
    response = service.serve(url, "GET", None)

    assert response.status == 200
    assert response.body == b"webp-bytes"
    assert _header_values(response, "content-type") == ["image/webp"]


def test_missing_bound_file_returns_404(tmp_path) -> None:
    service = ImageProtocolService()
    target = tmp_path / "gone.png"
    target.write_bytes(b"payload")
    url = service.register_local(target)
    target.unlink()

    assert service.serve(url, "GET", None).status == 404


def test_url_resolve_rejects_kind_without_source(tmp_path) -> None:
    api = _build_api(tmp_path)
    missing_path = asyncio.run(api.image_url_resolve({"kind": "local"}))
    missing_url = asyncio.run(api.image_url_resolve({"kind": "remote"}))

    assert missing_path["errorCode"] == "INVALID_REQUEST"
    assert missing_url["errorCode"] == "INVALID_REQUEST"


def test_url_resolve_local_missing_file(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.image_url_resolve({"kind": "local", "path": str(tmp_path / "nope.png")}))

    assert result["success"] is False
    assert result["errorCode"] == "FILE_NOT_FOUND"


def test_url_resolve_local_rejects_unsupported_extension(tmp_path) -> None:
    api = _build_api(tmp_path)
    target = tmp_path / "notes.txt"
    target.write_bytes(b"text")

    result = asyncio.run(api.image_url_resolve({"kind": "local", "path": str(target)}))

    assert result["success"] is False
    assert result["errorCode"] == "INVALID_IMAGE_TYPE"


def test_url_resolve_local_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "real.png"
    target.write_bytes(b"payload")
    link = tmp_path / "link.png"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前环境无法创建符号链接")
    api = _build_api(tmp_path)

    result = asyncio.run(api.image_url_resolve({"kind": "local", "path": str(link)}))

    assert result["success"] is False
    assert result["errorCode"] == "INVALID_PATH"


def test_url_resolve_local_signs_url_that_serves_bytes(tmp_path) -> None:
    api = _build_api(tmp_path)
    content = b"icon-bytes"
    target = tmp_path / "cover.png"
    target.write_bytes(content)

    result = asyncio.run(api.image_url_resolve({"kind": "local", "path": str(target)}))

    assert result["success"] is True
    url = result["data"]["url"]
    assert url.startswith(api.image_protocol.origin())
    response = api.image_protocol.serve(url, "GET", None)
    assert response.status == 200
    assert response.body == content


def test_url_resolve_remote_rejects_invalid_url(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.image_url_resolve({"kind": "remote", "url": "ftp://example.com/a.png"}))

    assert result["success"] is False
    assert result["errorCode"] == "INVALID_IMAGE_URL"


def test_url_resolve_remote_signs_url_after_download(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://textures.example.com/avatar"
    # 下载结果会经过 PIL 图片校验，必须使用真实可解码的 PNG 内容
    output = BytesIO()
    Image.new("RGBA", (8, 8), "red").save(output, format="PNG")
    content = output.getvalue()
    response = SimpleNamespace(
        status_code=200,
        headers={"content-type": "image/png"},
        url=url,
        raise_for_status=lambda: None,
    )

    async def download(_url):
        return content, response

    monkeypatch.setattr(files_module, "_download_remote_image", download)

    result = asyncio.run(api.image_url_resolve({"kind": "remote", "url": url}))

    assert result["success"] is True
    assert result["data"]["cached"] is False
    assert result["data"]["url_source"] == url
    serve_response = api.image_protocol.serve(result["data"]["url"], "GET", None)
    assert serve_response.status == 200
    assert serve_response.body == content
    # 第二次请求命中磁盘缓存，且签发的地址仍可读取同一份字节
    again = asyncio.run(api.image_url_resolve({"kind": "remote", "url": url}))
    assert again["data"]["cached"] is True
    assert api.image_protocol.serve(again["data"]["url"], "GET", None).body == content


def test_url_resolve_remote_rejects_requests_after_closing(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.events.emit("launcher:closing", None)

    result = asyncio.run(api.image_url_resolve({"kind": "remote", "url": "https://example.com/a.png"}))

    assert result["success"] is False
    assert result["errorCode"] == "LAUNCHER_CLOSING"
