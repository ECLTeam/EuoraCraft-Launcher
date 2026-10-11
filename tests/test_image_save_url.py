# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 image_save_url 模块的自动化测试。
#
# 公开接口：
#   - class FakeAccounts
#       - add_offline(username, custom_uuid=…) -> Any
#   - class FakePlugins
#       - on_frontend_ready() -> None
#       - list_plugins() -> Any
#   - class FakeWardrobe
#       - list_items() -> Any
#   - class FakeAnnouncements
#       - get_announcements() -> Any
#   - test_normalize_image_url_accepts_http_and_https() -> None
#   - test_normalize_image_url_rejects_invalid_urls() -> None
#   - test_normalize_image_url_strips_trailing_punctuation() -> None
#   - test_guess_image_extension_from_content_type() -> None
#   - test_guess_image_extension_from_content_disposition() -> None
#   - test_guess_image_extension_from_final_url() -> None
#   - test_guess_image_extension_defaults_to_jpg() -> None
#   - test_image_save_url_rejects_invalid_url(tmp_path) -> None
#   - test_image_save_url_follows_redirect_and_returns_data_url(tmp_path, monkeypatch) -> None
#   - test_image_fetch_data_url_persists_remote_texture_across_api_instances(tmp_path, monkeypatch) -> None
#   - test_image_fetch_data_url_uses_stale_disk_cache_when_texture_server_fails(tmp_path, monkeypatch) -> None
# ============================================================

import asyncio
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from io import BytesIO
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image


class _MockEmitter:
    @staticmethod
    def emit_str(*args):
        pass

    @staticmethod
    def emit_str_to(*args):
        pass

    @staticmethod
    def emit_str_filter(*args):
        pass


pytauri_ffi_module = ModuleType("pytauri.ffi")
pytauri_ffi_module.Emitter = _MockEmitter
pytauri_ffi_module.EXT_MOD = SimpleNamespace(pytauri_plugins=MagicMock())
for _ffi_name in [
    "IS_DEV",
    "RESTART_EXIT_CODE",
    "VERSION",
    "App",
    "AppHandle",
    "Assets",
    "Builder",
    "BuilderArgs",
    "CloseRequestApi",
    "Context",
    "CursorIcon",
    "DragDropEvent",
    "DragDropEventType",
    "Event",
    "EventId",
    "EventTargetType",
    "ExitRequestApi",
    "ImplEmitter",
    "ImplListener",
    "ImplManager",
    "Listener",
    "LogicalRect",
    "Manager",
    "PhysicalRect",
    "Position",
    "PositionType",
    "Rect",
    "RunEvent",
    "RunEventType",
    "Size",
    "SizeType",
    "Theme",
    "Url",
    "UserAttentionType",
    "WebviewEvent",
    "WebviewEventType",
    "WebviewUrl",
    "WebviewUrlType",
    "WindowEvent",
    "WindowEventType",
]:
    setattr(pytauri_ffi_module, _ffi_name, object)
pytauri_ffi_module.EventTarget = SimpleNamespace(Any=lambda: object())
pytauri_ffi_module.builder_factory = lambda: None
pytauri_ffi_module.context_factory = lambda *_args, **_kwargs: None
pytauri_ffi_module.webview_version = lambda: None

pytauri_ffi_typing_module = ModuleType("pytauri.ffi._typing")
pytauri_ffi_typing_module.Pyo3PathFrom = object
pytauri_ffi_typing_module.Pyo3PathInto = object

pytauri_ipc_module = ModuleType("pytauri.ipc")
pytauri_ipc_module.WebviewWindow = object
pytauri_ipc_module.Commands = object
pytauri_ipc_module.State = object

pytauri_plugin_module = ModuleType("pytauri.plugin")
pytauri_plugin_module.Plugin = object

pytauri_webview_module = ModuleType("pytauri.webview")
pytauri_webview_module.WebviewWindow = object

# 只 mock 子模块，让 pytauri/__init__.py 正常执行并把 mock 子模块的属性导入到 pytauri 命名空间
sys.modules["pytauri.ffi"] = pytauri_ffi_module
sys.modules["pytauri.ffi._typing"] = pytauri_ffi_typing_module
sys.modules["pytauri.ipc"] = pytauri_ipc_module
sys.modules["pytauri.plugin"] = pytauri_plugin_module
sys.modules["pytauri.webview"] = pytauri_webview_module

FrontendApi = import_module("ECL.api").FrontendApi
frontend_module = import_module("ECL.api.frontend")
files_module = import_module("ECL.api.files")
ConfigStore = import_module("ECL.utils").ConfigStore
EventBus = import_module("ECL.foundation").EventBus


class FakeAccounts:
    def add_offline(self, username, custom_uuid=None):
        return {"id": "offline-id", "alias": username, "type": "offline", "uuid": "offline-uuid"}


class FakePlugins:
    def on_frontend_ready(self) -> None:
        pass

    def list_plugins(self):
        return []


class FakeWardrobe:
    def list_items(self):
        return []


class FakeAnnouncements:
    def get_announcements(self):
        return []


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
        accounts=FakeAccounts(),
        connector=SimpleNamespace(),
        wardrobe=FakeWardrobe(),
        announcements=FakeAnnouncements(),
        game=SimpleNamespace(),
        plugins=FakePlugins(),
        processes=SimpleNamespace(),
    )
    return FrontendApi(context)


def _make_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (64, 64), "red").save(output, format="PNG")
    return output.getvalue()


class _AsyncMockClient:
    def __init__(self, response_chain):
        self._response_chain = list(response_chain)
        self._index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, method, url, **kwargs):
        response = self._response_chain[self._index]
        self._index += 1
        # 模拟 httpx 的 follow_redirects：遇到 3xx 且还有后续响应时返回下一个
        while 300 <= response.status_code < 400 and self._index < len(self._response_chain):
            location = response.headers.get("location")
            if not location:
                break
            response = self._response_chain[self._index]
            self._index += 1
        return _AsyncMockStream(response)


class _AsyncMockStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return None


class _MockResponse:
    def __init__(self, status_code, content, headers=None, url=None):
        self.status_code = status_code
        self._content = content
        self.headers = httpx.Headers(headers or {})
        self.url = httpx.URL(url or "https://example.com/image.png")

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            raise httpx.HTTPStatusError("mock error", request=request, response=self)

    async def aiter_bytes(self, chunk_size=64 * 1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


def test_normalize_image_url_accepts_http_and_https() -> None:
    assert frontend_module._normalize_image_url("http://example.com/a.png") == "http://example.com/a.png"
    assert frontend_module._normalize_image_url("https://example.com/a.png") == "https://example.com/a.png"


def test_normalize_image_url_rejects_invalid_urls() -> None:
    assert frontend_module._normalize_image_url("ftp://example.com/a.png") is None
    assert frontend_module._normalize_image_url("") is None
    assert frontend_module._normalize_image_url(123) is None
    assert frontend_module._normalize_image_url("not-a-url") is None


def test_normalize_image_url_strips_trailing_punctuation() -> None:
    assert frontend_module._normalize_image_url("https://example.com/a.png,") == "https://example.com/a.png"
    assert frontend_module._normalize_image_url("https://example.com/a.png;\n") == "https://example.com/a.png"
    assert frontend_module._normalize_image_url("  https://example.com/a.png  ") == "https://example.com/a.png"


def test_guess_image_extension_from_content_type() -> None:
    response = _MockResponse(200, b"", headers={"content-type": "image/webp"})
    assert frontend_module.ImagePolicy.extension_by_mime["image/webp"] == ".webp"
    assert frontend_module._guess_image_extension(response, "https://example.com/a") == ".webp"


def test_guess_image_extension_from_content_disposition() -> None:
    response = _MockResponse(200, b"", headers={"content-disposition": 'attachment; filename="bg.PNG"'})
    assert frontend_module._guess_image_extension(response, "https://example.com/a") == ".png"


def test_guess_image_extension_from_final_url() -> None:
    response = _MockResponse(200, b"", url="https://example.com/path/to/photo.webp")
    assert frontend_module._guess_image_extension(response, "https://example.com/path/to/photo.webp") == ".webp"


def test_guess_image_extension_defaults_to_jpg() -> None:
    response = _MockResponse(200, b"", headers={"content-type": "image/jpeg"})
    assert frontend_module._guess_image_extension(response, "https://example.com/a") == ".jpg"


def test_image_save_url_rejects_invalid_url(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.image_save_url({"url": "not-a-url"}))

    assert result["success"] is False
    assert result["errorCode"] == "INVALID_IMAGE_URL"


def test_image_save_url_follows_redirect_and_returns_data_url(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    png_bytes = _make_png_bytes()
    redirect_response = _MockResponse(
        302,
        b"",
        headers={"location": "https://cdn.example.com/final.png"},
        url="https://example.com/redirect",
    )
    final_response = _MockResponse(
        200,
        png_bytes,
        headers={"content-type": "image/png"},
        url="https://cdn.example.com/final.png",
    )
    mock_client = _AsyncMockClient([redirect_response, final_response])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)

    result = asyncio.run(api.image_save_url({"url": "https://example.com/redirect"}))

    assert result["success"] is True
    data = result["data"]
    assert data["url"] == "https://example.com/redirect"
    assert data["dataUrl"].startswith("data:image/png;base64,")
    assert data["base64"]


def test_image_fetch_data_url_persists_remote_texture_across_api_instances(tmp_path, monkeypatch) -> None:
    png_bytes = _make_png_bytes()
    response = _MockResponse(
        200,
        png_bytes,
        headers={"content-type": "image/png"},
        url="https://textures.minecraft.net/texture/avatar",
    )
    downloads = []

    async def download(url):
        downloads.append(url)
        return png_bytes, response

    monkeypatch.setattr(files_module, "_download_remote_image", download)
    url = "https://textures.minecraft.net/texture/avatar"

    first = asyncio.run(_build_api(tmp_path).image_fetch_data_url({"url": url}))
    second = asyncio.run(_build_api(tmp_path).image_fetch_data_url({"url": url}))

    assert first["success"] is True
    assert first["data"]["cached"] is False
    assert second["success"] is True
    assert second["data"]["cached"] is True
    assert second["data"]["dataUrl"] == first["data"]["dataUrl"]
    assert downloads == [url]


def test_image_fetch_data_url_uses_stale_disk_cache_when_texture_server_fails(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://textures.minecraft.net/texture/avatar"
    png_bytes = _make_png_bytes()
    api._write_remote_image_cache(url, ".png", png_bytes)
    cache_file = next((api.data_path / "cache" / "remote-images").glob("*.png"))
    stale_time = time.time() - api.remote_image_cache_ttl_seconds - 10
    os.utime(cache_file, (stale_time, stale_time))

    async def fail_download(_url):
        request = httpx.Request("GET", url)
        response = httpx.Response(502, request=request)
        raise httpx.HTTPStatusError("bad gateway", request=request, response=response)

    monkeypatch.setattr(files_module, "_download_remote_image", fail_download)

    result = asyncio.run(api.image_fetch_data_url({"url": url}))

    assert result["success"] is True
    assert result["data"]["cached"] is True
    assert result["data"]["stale"] is True
    assert result["data"]["dataUrl"].startswith("data:image/png;base64,")


def test_explicit_image_refresh_replaces_same_url_and_keeps_old_file_on_failure(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://example.com/skin.png"
    original = _make_png_bytes()
    api._write_remote_image_cache(url, ".png", original)
    replacement = BytesIO()
    Image.new("RGBA", (64, 64), "blue").save(replacement, format="PNG")
    downloads = []

    async def download(requested_url):
        downloads.append(requested_url)
        return replacement.getvalue(), _MockResponse(200, b"", headers={"content-type": "image/png"}, url=url)

    monkeypatch.setattr(files_module, "_download_remote_image", download)
    first = asyncio.run(api.image_fetch_data_url({"url": url}))
    assert first["data"]["cached"] is True
    refreshed = asyncio.run(api.image_fetch_data_url({"url": url, "refresh": True}))
    assert refreshed["data"]["cached"] is False
    assert api._read_remote_image_cache(url)[0] == replacement.getvalue()
    assert downloads == [url]

    async def fail_download(_url):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(files_module, "_download_remote_image", fail_download)
    stale = asyncio.run(api.image_fetch_data_url({"url": url, "refresh": True}))
    assert stale["data"]["stale"] is True
    assert api._read_remote_image_cache(url)[0] == replacement.getvalue()


@pytest.mark.parametrize("refresh", ["true", 1, None, [], {}])
def test_image_refresh_rejects_non_boolean_values(tmp_path, refresh) -> None:
    result = asyncio.run(
        _build_api(tmp_path).image_fetch_data_url({"url": "https://example.com/skin.png", "refresh": refresh})
    )
    assert result["errorCode"] == "INVALID_REQUEST"


def test_corrupted_refresh_cannot_replace_valid_cached_image(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://example.com/skin.png"
    original = _make_png_bytes()
    api._write_remote_image_cache(url, ".png", original)

    async def download(_url):
        return b"<html>bad gateway</html>", _MockResponse(200, b"", headers={"content-type": "image/png"}, url=url)

    monkeypatch.setattr(files_module, "_download_remote_image", download)
    result = asyncio.run(api.image_fetch_data_url({"url": url, "refresh": True}))
    assert result["data"]["stale"] is True
    assert api._read_remote_image_cache(url)[0] == original


def test_concurrent_image_requests_merge_and_refresh_finishes_after_old_download(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://example.com/skin.png"
    downloads = []

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def download(_url):
            index = len(downloads)
            downloads.append(index)
            if index == 0:
                started.set()
                await release.wait()
            output = BytesIO()
            Image.new("RGBA", (64, 64), "red" if index == 0 else "blue").save(output, format="PNG")
            return output.getvalue(), _MockResponse(200, b"", headers={"content-type": "image/png"}, url=url)

        monkeypatch.setattr(files_module, "_download_remote_image", download)
        first = asyncio.create_task(api.image_fetch_data_url({"url": url}))
        await started.wait()
        same = asyncio.create_task(api.image_fetch_data_url({"url": url}))
        await asyncio.sleep(0)
        refresh = asyncio.create_task(api.image_fetch_data_url({"url": url, "refresh": True}))
        await asyncio.sleep(0)
        same_refresh = asyncio.create_task(api.image_fetch_data_url({"url": url, "refresh": True}))
        release.set()
        results = await asyncio.gather(first, same, refresh, same_refresh)
        assert all(result["success"] for result in results)
        assert results[0]["data"]["dataUrl"] == results[1]["data"]["dataUrl"]
        assert results[2]["data"]["dataUrl"] == results[3]["data"]["dataUrl"]
        assert results[0]["data"]["dataUrl"] != results[2]["data"]["dataUrl"]
        assert api._remote_image_tasks == {}

    asyncio.run(exercise())
    assert downloads == [0, 1]
    with Image.open(BytesIO(api._read_remote_image_cache(url)[0])) as cached_image:
        assert cached_image.getpixel((0, 0)) == (0, 0, 255, 255)


def test_cancelled_waiter_does_not_cancel_shared_download(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://example.com/skin.png"

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def download(_url):
            started.set()
            await release.wait()
            return _make_png_bytes(), _MockResponse(200, b"", headers={"content-type": "image/png"}, url=url)

        monkeypatch.setattr(files_module, "_download_remote_image", download)
        first = asyncio.create_task(api.image_fetch_data_url({"url": url}))
        await started.wait()
        other = asyncio.create_task(api.image_fetch_data_url({"url": url}))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await other)["success"] is True
        assert api._remote_image_tasks == {}

    asyncio.run(exercise())


def test_launcher_shutdown_cancels_shared_download_and_stops_new_requests(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    url = "https://example.com/skin.png"

    async def exercise():
        started = asyncio.Event()

        async def download(_url):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(files_module, "_download_remote_image", download)
        request = asyncio.create_task(api.image_fetch_data_url({"url": url}))
        await started.wait()
        api.events.emit("launcher:closing", None)
        with pytest.raises(asyncio.CancelledError):
            await request
        assert api._remote_image_tasks == {}
        assert (await api.image_fetch_data_url({"url": url}))["errorCode"] == "LAUNCHER_CLOSING"

    asyncio.run(exercise())


@pytest.mark.parametrize("should_refresh", [False, True])
def test_native_and_dev_channel_loops_share_image_requests(tmp_path, monkeypatch, should_refresh) -> None:
    api = _build_api(tmp_path)
    url = "https://example.com/skin.png"
    started = Event()
    joined = Event()
    release = Event()
    downloads = []

    async def download(_url):
        index = len(downloads)
        downloads.append(index)
        if index == 0:
            started.set()
            assert await asyncio.to_thread(release.wait, 3)
        return _make_png_bytes(), _MockResponse(200, b"", headers={"content-type": "image/png"}, url=url)

    async def second_loop():
        request = asyncio.create_task(api.image_fetch_data_url({"url": url, "refresh": should_refresh}))
        await asyncio.sleep(0)
        joined.set()
        return await request

    monkeypatch.setattr(files_module, "_download_remote_image", download)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(asyncio.run, api.image_fetch_data_url({"url": url}))
        assert started.wait(3)
        second = executor.submit(asyncio.run, second_loop())
        assert joined.wait(3)
        release.set()
        assert first.result(timeout=3)["success"] is True
        assert second.result(timeout=3)["success"] is True
    assert downloads == ([0, 1] if should_refresh else [0])
    assert api._remote_image_tasks == {}
    assert api._remote_image_active == set()
