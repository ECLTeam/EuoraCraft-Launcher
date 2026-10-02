# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 mcmod 模块的自动化测试。
#
# 公开接口：
#   - test_lookup_by_slug(tmp_path) -> None
#   - test_lookup_by_local_mod_alias(tmp_path) -> None
#   - test_search_chinese_prefers_exact_and_prefix(tmp_path) -> None
#   - test_to_english_query_uses_exact_match(tmp_path) -> None
#   - test_mcmod_url_and_wiki_info(tmp_path) -> None
#   - test_missing_data_file_returns_empty(tmp_path) -> None
#   - test_corrupted_data_file_returns_empty(tmp_path) -> None
#   - test_list_local_mod_uses_chinese_display_name(tmp_path) -> None
#   - test_map_search_hits_fills_wiki_and_chinese_title(tmp_path) -> None
#   - test_map_search_hits_skips_wiki_for_unknown_slug(tmp_path) -> None
#   - test_fetch_project_versions_keeps_required_dependency_metadata(tmp_path) -> None
#   - test_map_search_hits_maps_curseforge_format(tmp_path) -> None
#   - test_search_online_resources_mod_facet_excludes_modpack(tmp_path) -> None
#   - test_search_online_resources_omits_empty_facets(tmp_path) -> None
#   - test_search_curseforge_403_raises_key_invalid(tmp_path) -> None
#   - test_search_curseforge_uses_hmcl_style_params(tmp_path) -> None
#   - test_search_curseforge_maps_sort_and_resource_type(tmp_path) -> None
#   - test_search_curseforge_worlds_uses_world_class_and_mapping(tmp_path) -> None
#   - test_curseforge_world_detail_and_files_are_mapped(tmp_path) -> None
#   - test_curseforge_file_uses_download_url_endpoint_as_fallback(tmp_path) -> None
#   - test_install_online_world_downloads_then_imports_archive(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from ECL.services.game import GameService
from ECL.services.game.mcmod import McmodTranslator

sample_mods = [
    {"id": 2785, "name": "钠", "english": "Sodium", "abbr": "", "cf": "sodium", "mr": "sodium", "modIds": ["sodium"]},
    {
        "id": 2021,
        "name": "机械动力",
        "english": "Create",
        "abbr": "",
        "cf": "create",
        "mr": "create",
        "modIds": ["create"],
    },
    {
        "id": 459,
        "name": "JEI物品管理器",
        "english": "Just Enough Items",
        "abbr": "JEI",
        "cf": "jei",
        "mr": "jei",
        "modIds": ["jei"],
    },
    {
        "id": 9999,
        "name": "工业时代2",
        "english": "Industrial Craft 2",
        "abbr": "IC2",
        "cf": "industrial-craft",
        "mr": "industrial-craft",
        "modIds": ["ic2"],
    },
]


def _write_sample(tmp_path: Path) -> Path:
    path = tmp_path / "mcmod_data.json"
    path.write_text(json.dumps({"version": 1, "mods": sample_mods}, ensure_ascii=False), encoding="utf-8")
    return path


def test_lookup_by_slug(tmp_path: Path) -> None:
    translator = McmodTranslator(_write_sample(tmp_path))

    assert translator.lookup_by_modrinth_slug("sodium")["name"] == "钠"
    assert translator.lookup_by_curseforge_slug("jei")["id"] == 459
    assert translator.lookup_by_modrinth_slug("SODIUM")["id"] == 2785
    assert translator.lookup_by_modrinth_slug("missing") is None


def test_lookup_by_local_mod_alias(tmp_path: Path) -> None:
    translator = McmodTranslator(_write_sample(tmp_path))

    assert translator.lookup_by_alias("Sodium")["name"] == "钠"
    assert translator.lookup_by_alias("sodium-extra", "sodium")["id"] == 2785
    assert translator.lookup_by_alias("missing") is None


def test_search_chinese_prefers_exact_and_prefix(tmp_path: Path) -> None:
    translator = McmodTranslator(_write_sample(tmp_path))

    assert [m["id"] for m in translator.search_chinese("钠")] == [2785]
    assert [m["id"] for m in translator.search_chinese("工业")] == [9999]
    assert translator.search_chinese("") == []


def test_to_english_query_uses_exact_match(tmp_path: Path) -> None:
    translator = McmodTranslator(_write_sample(tmp_path))

    assert translator.to_english_query("钠") == "Sodium"
    assert translator.to_english_query("机械动力") == "Create"
    assert translator.to_english_query("工业") == "Industrial Craft"
    assert translator.to_english_query("不存在的模组") == ""


def test_mcmod_url_and_wiki_info(tmp_path: Path) -> None:
    translator = McmodTranslator(_write_sample(tmp_path))

    assert translator.mcmod_url(2785) == "https://www.mcmod.cn/class/2785.html"
    wiki = translator.to_wiki_info(translator.lookup_by_modrinth_slug("sodium"))
    assert wiki == {
        "id": "2785",
        "title": "钠",
        "englishName": "Sodium",
        "summary": "",
        "url": "https://www.mcmod.cn/class/2785.html",
    }


def test_missing_data_file_returns_empty(tmp_path: Path) -> None:
    translator = McmodTranslator(tmp_path / "nonexistent.json")

    assert translator.lookup_by_modrinth_slug("sodium") is None
    assert translator.search_chinese("钠") == []
    assert translator.to_english_query("钠") == ""


def test_corrupted_data_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "mcmod_data.json"
    path.write_text("{broken json", encoding="utf-8")
    translator = McmodTranslator(path)

    assert translator.lookup_by_modrinth_slug("sodium") is None
    assert translator.to_english_query("钠") == ""


class _FakeAccounts:
    def current_account(self):
        return {"id": "offline", "type": "offline"}


def test_list_local_mod_uses_chinese_display_name(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "mcmod_data.json").write_text(
        json.dumps({"version": 1, "mods": sample_mods}, ensure_ascii=False), encoding="utf-8"
    )
    game_path = tmp_path / ".minecraft"
    mods_path = game_path / "mods"
    mods_path.mkdir(parents=True)
    with ZipFile(mods_path / "sodium.jar", "w") as archive:
        archive.writestr(
            "fabric.mod.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "sodium",
                    "name": "Sodium",
                    "version": "0.6.13",
                    "authors": ["CaffeineMC"],
                    "depends": {"minecraft": ">=1.21.1", "fabricloader": ">=0.16.0"},
                }
            ),
        )

    mods = GameService(_FakeAccounts(), resource_path=tmp_path).list_mods(game_path)

    assert len(mods) == 1
    assert mods[0]["name"] == "Sodium"
    assert mods[0]["display_name"] == "钠"
    assert mods[0]["english_name"] == "Sodium"
    assert mods[0]["mcmod_url"] == "https://www.mcmod.cn/class/2785.html"


def test_map_search_hits_fills_wiki_and_chinese_title(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "mcmod_data.json").write_text(
        json.dumps({"version": 1, "mods": sample_mods}, ensure_ascii=False), encoding="utf-8"
    )
    service = GameService(_FakeAccounts(), resource_path=tmp_path)

    hits = [
        {
            "project_id": "AANobbMI",
            "slug": "sodium",
            "title": "Sodium",
            "description": "desc",
            "author": "jellysquid3",
            "downloads": 1,
            "follows": 0,
            "date_modified": "2026-01-01T00:00:00Z",
            "categories": ["fabric"],
            "versions": ["1.20.1"],
            "icon_url": "https://example.com/icon.png",
        }
    ]
    items = service.map_search_hits("modrinth", hits, "mod")

    assert items[0]["displayTitle"] == "钠"
    assert items[0]["title"] == "Sodium"
    assert items[0]["wiki"]["id"] == "2785"
    assert items[0]["wiki"]["url"] == "https://www.mcmod.cn/class/2785.html"


def test_map_search_hits_skips_wiki_for_unknown_slug(tmp_path: Path) -> None:
    service = GameService(_FakeAccounts(), resource_path=tmp_path)

    hits = [
        {
            "project_id": "X",
            "slug": "unknown-mod",
            "title": "Unknown Mod",
            "description": "desc",
            "author": "a",
            "downloads": 0,
            "follows": 0,
            "categories": [],
            "versions": [],
        }
    ]
    items = service.map_search_hits("modrinth", hits, "mod")

    assert items[0]["displayTitle"] == "Unknown Mod"
    assert items[0]["wiki"] is None


def test_fetch_project_versions_keeps_required_dependency_metadata(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path)

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        assert url.endswith("/project/sodium/version")
        response = type("R", (), {})()
        response.raise_for_status = lambda: None
        response.json = lambda: [
            {
                "id": "version-1",
                "project_id": "sodium",
                "name": "Sodium 1.0",
                "version_number": "1.0",
                "game_versions": ["1.21.1"],
                "loaders": ["fabric"],
                "files": [{"filename": "sodium.jar", "primary": True}],
                "downloads": 1,
                "release_type": "release",
                "dependencies": [
                    {
                        "project_id": "fabric-api",
                        "version_id": None,
                        "file_name": None,
                        "dependency_type": "required",
                    }
                ],
            }
        ]
        return response

    with patch("httpx.get", side_effect=fake_get):
        versions = service.fetch_project_versions("modrinth", "sodium", "1.21.1", "fabric")

    assert versions[0]["dependencies"] == [
        {
            "projectId": "fabric-api",
            "versionId": None,
            "filename": None,
            "dependencyType": "required",
        }
    ]


def test_map_search_hits_maps_curseforge_format(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "mcmod_data.json").write_text(
        json.dumps({"version": 1, "mods": sample_mods}, ensure_ascii=False), encoding="utf-8"
    )
    service = GameService(_FakeAccounts(), resource_path=tmp_path)

    hits = [
        {
            "id": 394468,
            "name": "Sodium",
            "slug": "sodium",
            "summary": "cf desc",
            "downloadCount": 42,
            "dateModified": "2026-01-01T00:00:00Z",
            "authors": [{"name": "jellysquid3"}],
            "logo": {"url": "https://example.com/logo.png"},
        }
    ]
    items = service.map_search_hits("curseforge", hits, "mod")

    assert items[0]["title"] == "Sodium"
    assert items[0]["displayTitle"] == "钠"
    assert items[0]["description"] == "cf desc"
    assert items[0]["author"] == "jellysquid3"
    assert items[0]["downloads"] == 42
    assert items[0]["iconUrl"] == "https://example.com/logo.png"
    assert items[0]["projectUrl"] == "https://www.curseforge.com/minecraft/mc-mods/sodium"
    assert items[0]["wiki"]["id"] == "2785"


def test_search_online_resources_mod_facet_excludes_modpack(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path)
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        response = type("R", (), {})()
        response.raise_for_status = lambda: None
        response.json = lambda: {"hits": [], "total_hits": 0}
        return response

    with patch("httpx.get", side_effect=fake_get):
        service.search_online_resources("sodium", "1.20.1", "fabric", resource_type="mod")

    import json as json_module

    facets = json_module.loads(captured["params"]["facets"])
    assert ["project_type:mod"] in facets


def test_search_online_resources_omits_empty_facets(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path)
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        response = type("R", (), {})()
        response.raise_for_status = lambda: None
        response.json = lambda: {"hits": [], "total_hits": 0}
        return response

    with patch("httpx.get", side_effect=fake_get):
        service.search_online_resources("", "", "", resource_type="mod")

    import json as json_module

    facets = json_module.loads(captured["params"]["facets"])
    assert facets == [["project_type:mod"]]


def test_search_curseforge_403_raises_key_invalid(tmp_path: Path) -> None:
    from unittest.mock import patch

    from ECL.services.game.base import GameServiceError

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        response = type("R", (), {})()
        response.status_code = 403
        response.raise_for_status = lambda: None
        return response

    with patch("httpx.get", side_effect=fake_get), pytest.raises(GameServiceError) as exc_info:
        service.search_online_resources("iris", "", "", source="curseforge", resource_type="mod")

    assert exc_info.value.error_code == "CURSEFORGE_KEY_INVALID"
    assert captured["headers"]["x-api-key"] == "test-key"


def test_search_curseforge_uses_hmcl_style_params(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        response = type("R", (), {})()
        response.status_code = 200
        response.raise_for_status = lambda: None
        response.json = lambda: {"data": [], "pagination": {"totalCount": 0}}
        return response

    with patch("httpx.get", side_effect=fake_get):
        service.search_online_resources("", "", "", source="curseforge", resource_type="mod")

    params = captured["params"]
    assert params["gameId"] == 432
    assert params["classId"] == 6
    assert params["gameVersion"] == ""
    assert params["searchFilter"] == ""
    assert params["sortField"] == 2
    assert params["sortOrder"] == "desc"
    assert params["pageSize"] == 20
    assert params["index"] == 0


@pytest.mark.parametrize(("loader", "expected"), [("forge", 1), ("fabric", 4), ("quilt", 5), ("neoforge", 6)])
def test_curseforge_search_filters_mod_loader(tmp_path: Path, monkeypatch, loader: str, expected: int) -> None:
    from types import SimpleNamespace

    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs["params"])
        return SimpleNamespace(
            status_code=200, raise_for_status=lambda: None, json=lambda: {"data": [], "pagination": {"totalCount": 0}}
        )

    monkeypatch.setattr("ECL.services.game.resources._proxied_get", fake_get)
    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test")
    try:
        service.search_online_resources("", "1.21.1", loader, source="curseforge")
        assert captured["modLoaderType"] == expected
        assert captured["gameVersion"] == "1.21.1"
    finally:
        service.close()


def test_search_curseforge_maps_sort_and_resource_type(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["params"] = params
        response = type("R", (), {})()
        response.status_code = 200
        response.raise_for_status = lambda: None
        response.json = lambda: {"data": [], "pagination": {"totalCount": 0}}
        return response

    with patch("httpx.get", side_effect=fake_get):
        service.search_online_resources(
            "sodium", "1.20.1", "fabric", source="curseforge", resource_type="shaderpack", sort="downloads"
        )

    params = captured["params"]
    assert params["classId"] == 6552
    assert params["gameVersion"] == "1.20.1"
    assert params["searchFilter"] == "sodium"
    assert params["sortField"] == 6


def test_search_curseforge_worlds_uses_world_class_and_mapping(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")
    captured: dict[str, object] = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        captured["params"] = params
        response = type("R", (), {})()
        response.status_code = 200
        response.raise_for_status = lambda: None
        response.json = lambda: {
            "data": [
                {
                    "id": 123,
                    "name": "Sky World",
                    "slug": "sky-world",
                    "summary": "A world",
                    "authors": [{"name": "Builder"}],
                    "logo": {"url": "https://example.com/world.png"},
                }
            ],
            "pagination": {"totalCount": 1},
        }
        return response

    with patch("httpx.get", side_effect=fake_get):
        result = service.search_online_resources("sky", "1.21.1", "", source="curseforge", resource_type="world")

    assert captured["params"]["classId"] == 17
    assert result["resource_type"] == "world"
    items = service.map_search_hits("curseforge", result["items"], result["resource_type"])
    assert items[0]["resourceType"] == "world"
    assert items[0]["projectUrl"] == "https://www.curseforge.com/minecraft/worlds/sky-world"


def test_curseforge_world_detail_and_files_are_mapped(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        response = type("R", (), {})()
        response.status_code = 200
        response.raise_for_status = lambda: None
        if url.endswith("/mods/123"):
            response.json = lambda: {
                "data": {
                    "id": 123,
                    "name": "Sky World",
                    "slug": "sky-world",
                    "summary": "A world",
                    "authors": [{"name": "Builder"}],
                    "logo": {"url": "https://example.com/world.png"},
                    "links": {"websiteUrl": "https://www.curseforge.com/minecraft/worlds/sky-world"},
                    "latestFiles": [{"gameVersions": ["1.21.1", "Java 21"]}],
                }
            }
        else:
            assert url.endswith("/mods/123/files")
            assert params == {"pageSize": 50, "index": 0, "gameVersion": "1.21.1"}
            response.json = lambda: {
                "data": [
                    {
                        "id": 456,
                        "modId": 123,
                        "displayName": "Sky World 1.0",
                        "fileName": "sky-world.zip",
                        "releaseType": 1,
                        "gameVersions": ["1.21.1", "Java 21"],
                        "downloadCount": 7,
                    }
                ]
            }
        return response

    with patch("httpx.get", side_effect=fake_get):
        info = service.fetch_project_info("curseforge", "123", "world")
        versions = service.fetch_project_versions("curseforge", "123", "1.21.1")

    assert info["source"] == "curseforge"
    assert info["resourceType"] == "world"
    assert info["gameVersions"] == ["1.21.1"]
    assert versions == [
        {
            "id": "456",
            "projectId": "123",
            "name": "Sky World 1.0",
            "versionNumber": "Sky World 1.0",
            "gameVersions": ["1.21.1"],
            "loaders": [],
            "filename": "sky-world.zip",
            "datePublished": None,
            "downloads": 7,
            "releaseType": "release",
            "dependencies": [],
        }
    ]


def test_curseforge_file_uses_download_url_endpoint_as_fallback(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")

    def fake_get(url, headers=None, timeout=None, **kwargs):
        response = type("R", (), {})()
        response.status_code = 200
        response.raise_for_status = lambda: None
        response.json = (
            (lambda: {"data": "https://edge.forgecdn.net/files/world.zip"})
            if url.endswith("/download-url")
            else (lambda: {"data": {"fileName": "world.zip", "downloadUrl": None}})
        )
        return response

    with patch("httpx.get", side_effect=fake_get):
        selected = service._fetch_curseforge_file("123", "456")

    assert selected["filename"] == "world.zip"
    assert selected["url"] == "https://edge.forgecdn.net/files/world.zip"


def test_install_online_world_downloads_then_imports_archive(tmp_path: Path) -> None:
    from unittest.mock import patch

    service = GameService(_FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test-key")
    saves = tmp_path / "saves"

    def fake_download(url, destination, filename, task_id):
        assert url == "https://example.com/world.zip"
        assert filename == "world.zip"
        destination.write_bytes(b"world archive")

    def fake_import(root, source, context=None):
        assert root == saves
        assert source.read_bytes() == b"world archive"
        assert context is None
        return {"worldId": "Sky World"}

    with (
        patch.object(service, "_world_root", return_value=saves),
        patch.object(
            service,
            "_select_online_file",
            return_value={"filename": "world.zip", "url": "https://example.com/world.zip", "hashes": {}},
        ),
        patch.object(service, "_download_online_file", side_effect=fake_download),
        patch.object(service, "_import_world_source", side_effect=fake_import),
    ):
        result = service.install_online_resource(
            tmp_path,
            "instance",
            "world",
            "curseforge",
            "123",
            "456",
        )

    assert result == {"filename": "Sky World", "source": "curseforge", "skipped": False}
