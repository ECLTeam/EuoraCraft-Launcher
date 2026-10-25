# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 minecraft_versions 模块的自动化测试。
#
# 公开接口：
#   - class FakeAccounts
#   - test_neoforge_loader_name_uses_frontend_canonical_mapping() -> None
#   - test_version_service_normalizes_scanner_output(tmp_path) -> None
#   - test_version_service_scans_with_original_search_minecraft(tmp_path) -> None
#   - test_version_service_skips_missing_versions_directory(tmp_path) -> None
#   - test_version_service_rejects_invalid_path_payload() -> None
#   - test_loader_versions_returns_plain_version_strings(tmp_path, monkeypatch) -> None
#   - test_loader_versions_handles_plain_list_and_empty_result(tmp_path, monkeypatch) -> None
#   - test_bmcl_forge_versions_use_same_newest_first_order() -> None
#   - test_forge_versions_read_from_maven_index() -> None
# ============================================================

import json
import zipfile
from types import SimpleNamespace

import pytest

from ECL.game import SearchMinecraft
from ECL.services import GameService, VersionScanError


class FakeAccounts:
    pass


def test_neoforge_loader_name_uses_frontend_canonical_mapping() -> None:
    version_json = {
        "libraries": [{"name": "net.neoforged:neoforge:26.2.0.25-beta"}],
        "arguments": {"game": ["--fml.neoForgeVersion", "26.2.0.25-beta"]},
    }

    loader = SearchMinecraft._find_loader_type(version_json)

    assert loader == "NeoForge"
    assert SearchMinecraft._find_loader_version(version_json, loader) == "26.2.0.25-beta"


def _write_version_json(game_path, version_name: str, data: dict) -> None:
    version_path = game_path / "versions" / version_name
    version_path.mkdir(parents=True)
    (version_path / f"{version_name}.json").write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )
    with zipfile.ZipFile(version_path / f"{version_name}.jar", "w") as archive:
        archive.writestr("version.json", json.dumps({"id": version_name}))


def test_version_service_normalizes_scanner_output(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    _write_version_json(game_path, "fabric-profile", {"id": "fabric-profile"})
    requested_paths = []

    class FakeSearchMinecraft:
        def __init__(self, path):
            requested_paths.append(path)

        def search_minecraft(self):
            return {
                "fabric-profile": {
                    "VanillaType": "Release",
                    "VanillaVersion": "1.20.1",
                    "VersionPath": str(game_path / "versions" / "fabric-profile"),
                    "RequestJava": "17",
                    "LoaderType": "Fabric",
                    "LoaderVersion": "0.16.14",
                }
            }

    service = GameService(FakeAccounts(), search_factory=FakeSearchMinecraft)

    versions = service.scan_versions([str(game_path), str(game_path / "versions")])

    assert requested_paths == [game_path]
    assert len(versions) == 1
    version = versions[0]
    assert version is not None
    expected = {
        "id": "fabric-profile",
        "versionId": "fabric-profile",
        "versionType": "Release",
        "path": str(game_path),
        "displayName": "fabric-profile",
        "primaryLoader": "Fabric",
        "loaderVersion": "0.16.14",
        "vanillaName": "1.20.1",
        "requiredJava": 17,
        "hasFabric": True,
        "sourceName": ".minecraft",
    }
    assert expected.items() <= version.items()
    assert version["categoryId"] == "modded"
    assert version["fieldSources"]["alias"] == "auto"
    assert version["launchCount"] == 0


def test_version_service_scans_with_original_search_minecraft(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    _write_version_json(
        game_path,
        "1.20.1",
        {
            "id": "1.20.1",
            "type": "release",
            "releaseTime": "2023-06-12T13:00:00+00:00",
            "javaVersion": {"majorVersion": 17},
            "libraries": [],
        },
    )

    versions = GameService(FakeAccounts()).scan_versions(str(game_path))

    assert len(versions) == 1
    assert versions[0]["versionId"] == "1.20.1"
    assert versions[0]["versionType"] == "Release"
    assert versions[0]["primaryLoader"] == "Vanilla"
    assert "isBroken" not in versions[0] and "health" not in versions[0]


def test_version_service_skips_missing_versions_directory(tmp_path) -> None:
    service = GameService(FakeAccounts(), search_factory=lambda _path: None)

    assert service.scan_versions(str(tmp_path / "empty-game")) == []


def test_version_service_rejects_invalid_path_payload() -> None:
    service = GameService(FakeAccounts(), search_factory=lambda _path: None)

    with pytest.raises(VersionScanError) as error:
        service.scan_versions({"path": "invalid"})

    assert error.value.error_code == "INVALID_GAME_PATH"


def test_loader_versions_returns_plain_version_strings(tmp_path, monkeypatch) -> None:
    service = GameService(FakeAccounts(), data_path=tmp_path)
    fake_games = SimpleNamespace(
        get_fabric_versions=lambda version: {
            "All": [
                {"LoaderVersion": "0.16.14", "GameVersion": version, "Stable": True},
                {"LoaderVersion": "0.16.13", "GameVersion": version, "Stable": False},
            ],
            "Stable": [{"LoaderVersion": "0.16.14", "GameVersion": version, "Stable": True}],
            "NotStable": [{"LoaderVersion": "0.16.13", "GameVersion": version, "Stable": False}],
        }
    )
    monkeypatch.setattr(service, "_query_context", lambda source="official": SimpleNamespace(games=fake_games))

    result = service.loader_versions("fabric", "26.1.2")

    assert result == ["0.16.14", "0.16.13"]
    assert all(isinstance(item, str) for item in result)


def test_loader_versions_handles_plain_list_and_empty_result(tmp_path, monkeypatch) -> None:
    service = GameService(FakeAccounts(), data_path=tmp_path)
    fake_games = SimpleNamespace(
        get_forge_versions=lambda version: [
            {"LoaderVersion": "55.0.9", "GameVersion": version},
            {"LoaderVersion": "55.0.8", "GameVersion": version},
        ],
        get_quilt_versions=lambda version: None,
    )
    monkeypatch.setattr(service, "_query_context", lambda source="official": SimpleNamespace(games=fake_games))

    assert service.loader_versions("forge", "1.20.1") == ["55.0.9", "55.0.8"]
    assert service.loader_versions("quilt", "1.20.1") == []


def test_bmcl_forge_versions_use_same_newest_first_order() -> None:
    """BMCLAPI 源应和官方源一样按版本降序返回，不受镜像原始顺序影响。"""
    from ECL.game.Core.NetLibs import BaseApiClient, BmclApiUrl

    client = BaseApiClient.__new__(BaseApiClient)
    client.config = BmclApiUrl()
    requested = []
    client._get_json_with_retry = lambda url: (
        requested.append(url),
        [{"version": "47.4.27"}, {"version": "47.4.28"}, {"version": "47.4.9"}],
    )[1]

    assert [item["LoaderVersion"] for item in client.get_forge_versions("1.20.1")] == [
        "47.4.28",
        "47.4.27",
        "47.4.9",
    ]
    assert requested == ["https://bmclapi2.bangbang93.com/forge/minecraft/1.20.1"]


def test_forge_versions_read_from_maven_index() -> None:
    """官方源应读 maven-metadata.json，剥掉 "{mc}-" 前缀并按最新在前返回。"""
    from ECL.game.Core.NetLibs import BaseApiClient

    payload = {
        "1.20.1": ["1.20.1-47.4.21", "1.20.1-47.4.22"],
        "1.21.1": ["1.21.1-52.1.20"],
    }
    client = BaseApiClient.__new__(BaseApiClient)
    client.config = SimpleNamespace()
    requested = []
    client._get_json_with_retry = lambda url: (requested.append(url), payload)[1]

    assert client.get_forge_versions("1.20.1") == [
        {"LoaderVersion": "47.4.22", "GameVersion": "1.20.1"},
        {"LoaderVersion": "47.4.21", "GameVersion": "1.20.1"},
    ]
    assert requested == ["https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json"]
    assert client.get_forge_versions("1.21.9") == []
