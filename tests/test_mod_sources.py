# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 mod_sources 模块的自动化测试。
#
# 公开接口：
#   - test_normalize_mod_source_falls_back_to_official() -> None
#   - test_alternate_mod_source_is_bidirectional() -> None
#   - test_mod_api_base_maps_platforms() -> None
#   - test_rewrite_mod_file_url_replaces_mirrored_hosts() -> None
#   - test_rewrite_mod_file_url_keeps_mediafilez_and_unknown_hosts() -> None
#   - test_rewrite_mod_file_url_is_noop_for_official_source() -> None
#   - test_rewrite_mod_file_url_preserves_query_and_fragment() -> None
#   - test_mod_user_agent_uses_launcher_version() -> None
#   - test_request_falls_back_to_alternate_source() -> None
#   - test_request_raises_alternate_error_with_primary_cause() -> None
#   - test_request_respects_can_fallback_false() -> None
#   - test_request_validates_response_structure() -> None
# ============================================================

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx
import pytest

from ECL.common.version import __version__
from ECL.services.game.mod_sources import (
    ModSourcePolicy,
    ModSourceRequestPolicy,
    alternate_mod_source,
    display_mod_source,
    mod_api_base,
    mod_user_agent,
    normalize_mod_source,
    rewrite_mod_file_url,
)


def _logger() -> logging.Logger:
    return logging.getLogger("EuoraCraft-Launcher.Tests.ModSources")


def _policy(source: str) -> ModSourceRequestPolicy:
    return ModSourceRequestPolicy(lambda: source, _logger())


def test_normalize_mod_source_falls_back_to_official() -> None:
    assert normalize_mod_source("official") == "official"
    assert normalize_mod_source("MCIM") == "mcim"
    assert normalize_mod_source("  Mcim  ") == "mcim"
    assert normalize_mod_source("") == "official"
    assert normalize_mod_source(None) == "official"
    assert normalize_mod_source("bmclapi") == "official"


def test_alternate_mod_source_is_bidirectional() -> None:
    assert alternate_mod_source("official") == "mcim"
    assert alternate_mod_source("mcim") == "official"
    assert alternate_mod_source("unknown") == "mcim"


def test_display_mod_source_returns_chinese_names() -> None:
    assert display_mod_source("official") == "官方"
    assert display_mod_source("mcim") == "MCIM"


def test_mod_api_base_maps_platforms() -> None:
    assert mod_api_base("official", "modrinth") == "https://api.modrinth.com/v2"
    assert mod_api_base("mcim", "modrinth") == "https://mod.mcimirror.top/modrinth/v2"
    assert mod_api_base("official", "curseforge") == "https://api.curseforge.com/v1"
    assert mod_api_base("mcim", "curseforge") == "https://mod.mcimirror.top/curseforge/v1"
    assert mod_api_base("unknown", "modrinth") == "https://api.modrinth.com/v2"


def test_rewrite_mod_file_url_replaces_mirrored_hosts() -> None:
    assert (
        rewrite_mod_file_url("https://cdn.modrinth.com/data/A/versions/B/f.jar", "mcim")
        == "https://mod.mcimirror.top/data/A/versions/B/f.jar"
    )
    assert (
        rewrite_mod_file_url("https://edge.forgecdn.net/files/8937/628/f.jar", "mcim")
        == "https://mod.mcimirror.top/files/8937/628/f.jar"
    )
    assert (
        rewrite_mod_file_url("https://media.forgecdn.net/avatars/29/69/a.jpeg", "mcim")
        == "https://mod.mcimirror.top/avatars/29/69/a.jpeg"
    )


def test_rewrite_mod_file_url_keeps_mediafilez_and_unknown_hosts() -> None:
    # MCIM 文档明确要求 mediafilez.forgecdn.net 不得替换，映射表未收录即天然保留。
    for url in (
        "https://mediafilez.forgecdn.net/files/1/2/f.jar",
        "https://example.com/files/1/2/f.jar",
        "https://www.curseforge.com/minecraft/mc-mods/jei",
        "https://modrinth.com/mod/sodium",
    ):
        assert rewrite_mod_file_url(url, "mcim") == url


def test_rewrite_mod_file_url_is_noop_for_official_source() -> None:
    url = "https://cdn.modrinth.com/data/A/versions/B/f.jar"
    assert rewrite_mod_file_url(url, "official") == url
    assert rewrite_mod_file_url(url, "unknown") == url


def test_rewrite_mod_file_url_preserves_query_and_fragment() -> None:
    rewritten = rewrite_mod_file_url("https://cdn.modrinth.com/data/A/f.jar?x=1&y=2#frag", "mcim")
    parsed = urlsplit(rewritten)
    assert parsed.netloc == "mod.mcimirror.top"
    assert parsed.path == "/data/A/f.jar"
    assert parsed.query == "x=1&y=2"
    assert parsed.fragment == "frag"


def test_rewrite_mod_file_url_handles_empty_and_non_http_values() -> None:
    assert rewrite_mod_file_url("", "mcim") == ""
    assert rewrite_mod_file_url("data:image/png;base64,AAAA", "mcim") == "data:image/png;base64,AAAA"


def test_mod_user_agent_uses_launcher_version() -> None:
    assert mod_user_agent() == f"{ModSourcePolicy.user_agent_name}/{__version__}"
    assert mod_user_agent().startswith("EuoraCraft-Launcher/")


def test_request_falls_back_to_alternate_source() -> None:
    attempts: list[str] = []

    def operation(source: str) -> str:
        attempts.append(source)
        if source == "official":
            raise httpx.ConnectError("official down")
        return "ok"

    assert _policy("official").request("测试操作", operation) == "ok"
    assert attempts == ["official", "mcim"]


def test_request_falls_back_from_mcim_to_official() -> None:
    attempts: list[str] = []

    def operation(source: str) -> str:
        attempts.append(source)
        if source == "mcim":
            raise httpx.ConnectError("mcim down")
        return "ok"

    assert _policy("mcim").request("测试操作", operation) == "ok"
    assert attempts == ["mcim", "official"]


def test_request_raises_alternate_error_with_primary_cause() -> None:
    def operation(source: str) -> str:
        raise httpx.ConnectError(f"{source} down")

    with pytest.raises(httpx.ConnectError) as exc_info:
        _policy("official").request("测试操作", operation)

    assert "mcim down" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert "official down" in str(exc_info.value.__cause__)


def test_request_respects_can_fallback_false() -> None:
    attempts: list[str] = []

    def operation(source: str) -> str:
        attempts.append(source)
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        _policy("official").request("测试操作", operation, can_fallback=False)
    assert attempts == ["official"]


def test_request_validates_response_structure() -> None:
    def operation(source: str) -> dict:
        return {"items": []} if source == "official" else {"items": [1]}

    result = _policy("official").request("测试操作", operation, is_valid=lambda payload: bool(payload.get("items")))
    assert result == {"items": [1]}


def test_request_falls_back_on_json_decode_error() -> None:
    attempts: list[str] = []

    def operation(source: str) -> dict:
        attempts.append(source)
        if source == "official":
            raise json.JSONDecodeError("bad", "", 0)
        return {"ok": True}

    assert _policy("official").request("测试操作", operation) == {"ok": True}
    assert attempts == ["official", "mcim"]


def test_request_source_reads_provider_lazily() -> None:
    current = {"source": "official"}
    policy = ModSourceRequestPolicy(lambda: current["source"], _logger())
    assert policy.source() == "official"
    current["source"] = "mcim"
    assert policy.source() == "mcim"
