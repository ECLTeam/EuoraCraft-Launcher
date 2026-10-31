from __future__ import annotations

import json

import httpx
import pytest

from ECL.plugin_api.contracts import ProtocolError
from tests.test_plugin_game_launch import game_launch  # noqa: F401 - pytest fixture
from tests.test_plugin_host_files import call


@pytest.mark.parametrize(
    "kind,project_type", [("mods", "mod"), ("resourcepacks", "resourcepack"), ("shaders", "shader")]
)
def test_real_catalog_search_versions_and_file_selection_return_only_public_fields(
    request, monkeypatch, kind, project_type
):
    state = request.getfixturevalue("game_launch")
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        if url.endswith("/search"):
            payload = {
                "total_hits": 1,
                "hits": [
                    {"project_id": "project", "title": "Fixture", "description": "Resource", "private": "credential"}
                ],
            }
        elif url.endswith("/project/project/version"):
            payload = [
                {
                    "id": "release",
                    "project_id": "project",
                    "name": "Release",
                    "version_number": "1.0",
                    "game_versions": ["1.21.1"],
                    "loaders": ["fabric"],
                    "files": [{"filename": "fixture.jar", "primary": True}],
                }
            ]
        else:
            assert url.endswith("/version/release")
            payload = {
                "files": [
                    {
                        "filename": "fixture.jar",
                        "url": "https://cdn.modrinth.com/data/project/fixture.jar",
                        "primary": True,
                        "size": 7,
                        "hashes": {"sha512": "a" * 128},
                        "private": "credential",
                    }
                ]
            }
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr("ECL.services.game.resources._proxied_get", get)
    result = call(state.broker, kind + ".search", {"query": "fixture", "game_version": "1.21.1", "loader": "fabric"})
    assert result["items"] == [
        {"source": "modrinth", "project_id": "project", "title": "Fixture", "description": "Resource"}
    ]
    assert project_type in requests[0][1]["params"]["facets"]
    versions = call(
        state.broker, kind + ".versions", {"project_id": "project", "game_version": "1.21.1", "loader": "fabric"}
    )
    assert versions["items"][0]["version_id"] == "release"
    download = call(state.broker, kind + ".download_info", {"project_id": "project", "version_id": "release"})
    assert download["filename"] == "fixture.jar"
    assert download["origin"] == "https:cdn.modrinth.com:443"
    assert "credential" not in json.dumps([result, versions, download])
    assert not state.manager._operations


def test_catalog_rejects_arbitrary_urls_traversal_and_oversized_queries(request, monkeypatch):
    state = request.getfixturevalue("game_launch")
    monkeypatch.setattr(
        state.game, "search_online_resources", lambda *_args, **_kwargs: pytest.fail("invalid input reached network")
    )
    for method, params in [
        ("mods.search", {"source": "https://private.invalid"}),
        ("mods.search", {"query": "x" * 129}),
        ("mods.versions", {"project_id": "../private"}),
        ("mods.download_info", {"project_id": "project", "version_id": "../private"}),
    ]:
        with pytest.raises(ProtocolError):
            call(state.broker, method, params)
