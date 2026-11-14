# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 downloader 模块的自动化测试。
#
# 公开接口：
#   - test_downloader_follows_redirect(monkeypatch, tmp_path) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path

import httpx

from ECL.minecraft.game.Core.Downloader import Downloader


def _redirect_handler(request: httpx.Request) -> httpx.Response:
    """模拟 CDN 307 重定向：/file.jar 跳转到 /real.jar。"""
    if request.url.path == "/file.jar":
        return httpx.Response(307, headers={"Location": "http://example.com/real.jar"})
    if request.url.path == "/real.jar":
        return httpx.Response(200, content=b"REAL-CONTENT", headers={"Content-Length": "12"})
    return httpx.Response(404)


async def test_downloader_follows_redirect(monkeypatch, tmp_path: Path) -> None:
    # 此用例验证 HTTP 重定向，必须隔离用户或前序测试设置的真实下载代理。
    monkeypatch.delenv("ECL_DOWNLOAD_PROXY", raising=False)
    original = httpx.AsyncClient
    client_options: list[dict[str, object]] = []

    def _client_factory(**kwargs):
        client_options.append(kwargs)
        return original(transport=httpx.MockTransport(_redirect_handler), **kwargs)

    monkeypatch.setattr("ECL.minecraft.game.Core.Downloader.httpx.AsyncClient", _client_factory)

    target = tmp_path / "file.jar"
    downloader = Downloader([("http://example.com/file.jar", target)], max_rounds=1)
    await downloader.run()

    assert downloader.failed_entries == set()
    assert target.read_bytes() == b"REAL-CONTENT"
    assert client_options and all(options.get("trust_env") is False for options in client_options)
