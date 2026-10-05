# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证游戏元数据请求与批量文件地址的双源回退边界。
# ============================================================

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ECL.events import EventBus
from ECL.game import ApiUrlConfig, BmclApiUrl
from ECL.services.game import GameService
from ECL.services.game.download_sources import PreferredApiClient


def _clients(preferred_source: str) -> tuple[PreferredApiClient, Mock, Mock]:
    preferred_config = ApiUrlConfig() if preferred_source == "official" else BmclApiUrl()
    alternate_config = BmclApiUrl() if preferred_source == "official" else ApiUrlConfig()
    preferred = Mock(config=preferred_config)
    alternate = Mock(config=alternate_config)
    client = PreferredApiClient(preferred, alternate, preferred_source, Mock())
    return client, preferred, alternate


@pytest.mark.parametrize("preferred_source", ["official", "bmclapi"])
def test_metadata_falls_back_only_after_preferred_failure(preferred_source: str) -> None:
    client, preferred, alternate = _clients(preferred_source)
    preferred.get_minecraft_manifest.side_effect = httpx.ConnectError("first source unavailable")
    alternate.get_minecraft_manifest.return_value = {"latest": {}, "versions": []}

    assert client.get_minecraft_manifest() == {"latest": {}, "versions": []}
    assert client.config is preferred.config
    preferred.get_minecraft_manifest.assert_called_once_with()
    alternate.get_minecraft_manifest.assert_called_once_with()


def test_preferred_success_does_not_request_alternate() -> None:
    client, preferred, alternate = _clients("official")
    preferred.get_minecraft_manifest.return_value = {"latest": {}, "versions": ["1.21"]}

    assert client.get_minecraft_manifest() == {"latest": {}, "versions": ["1.21"]}
    alternate.get_minecraft_manifest.assert_not_called()


def test_invalid_preferred_manifest_uses_valid_alternate() -> None:
    client, preferred, alternate = _clients("official")
    preferred.get_minecraft_manifest.return_value = {"versions": []}
    alternate.get_minecraft_manifest.return_value = {"latest": {}, "versions": []}

    assert client.get_minecraft_manifest() == {"latest": {}, "versions": []}
    alternate.get_minecraft_manifest.assert_called_once_with()


def test_non_network_error_and_shared_quilt_endpoint_do_not_switch_sources() -> None:
    client, preferred, alternate = _clients("official")
    preferred.get_minecraft_manifest.side_effect = OSError("local disk failed")
    preferred.get_quilt_versions.side_effect = httpx.ConnectError("shared endpoint unavailable")

    with pytest.raises(OSError, match="local disk failed"):
        client.get_minecraft_manifest()
    with pytest.raises(httpx.ConnectError, match="shared endpoint unavailable"):
        client.get_quilt_versions()
    alternate.get_minecraft_manifest.assert_not_called()
    alternate.get_quilt_versions.assert_not_called()


def test_both_source_failures_keep_original_cause() -> None:
    client, preferred, alternate = _clients("bmclapi")
    preferred.get_asset_index.side_effect = httpx.ReadTimeout("mirror timed out")
    alternate.get_asset_index.side_effect = httpx.ConnectError("official unavailable")

    with pytest.raises(httpx.ConnectError, match="official unavailable") as error:
        client.get_asset_index("1.21", "sha1")

    assert isinstance(error.value.__cause__, httpx.ReadTimeout)


def test_direct_installer_download_retries_with_other_client(tmp_path: Path) -> None:
    client, preferred, alternate = _clients("official")
    preferred.download_forge_installer.side_effect = httpx.ConnectError("official unavailable")
    expected_path = tmp_path / "forge-installer.jar"
    alternate.download_forge_installer.return_value = expected_path

    assert client.download_forge_installer("1.21", "54.0.0", tmp_path) == expected_path
    alternate.download_forge_installer.assert_called_once_with("1.21", "54.0.0", tmp_path)
    client.close()
    preferred.close.assert_called_once_with()
    alternate.close.assert_called_once_with()


def test_catalog_uses_preferred_context_and_falls_back_to_other_client(tmp_path: Path) -> None:
    constructed_sources: list[str] = []

    class CatalogClient:
        def __init__(self, config):
            self.config = config
            self.source = "bmclapi" if isinstance(config, BmclApiUrl) else "official"
            constructed_sources.append(self.source)

        def get_minecraft_manifest(self):
            if self.source == "bmclapi":
                raise httpx.ConnectError("mirror unavailable")
            return {
                "latest": {"release": "1.21"},
                "versions": [{"id": "1.21", "type": "release", "releaseTime": "2026-01-01", "sha1": "a" * 40}],
            }

        def close(self):
            pass

    service = GameService(Mock(), api_client_factory=CatalogClient, data_path=tmp_path, enable_version_watcher=False)

    catalog = service.minecraft_versions_classified("bmclapi")

    assert constructed_sources == ["bmclapi", "official"]
    assert catalog["release"][0]["id"] == "1.21"
    service.close()


@pytest.mark.parametrize("preferred_source", ["official", "bmclapi"])
def test_failed_file_mapping_uses_only_distinct_alternate_urls(tmp_path: Path, preferred_source: str) -> None:
    service = GameService(Mock(), enable_version_watcher=False)
    failed_file = tmp_path / "libraries" / "example.jar"
    third_party_file = tmp_path / "mods" / "third-party.jar"
    local_failure_file = tmp_path / "libraries" / "unwritable.jar"
    preferred_url = f"https://{preferred_source}.example/example.jar"
    alternate_url = "https://other.example/example.jar"
    shared_url = "https://modrinth.example/third-party.jar"
    candidates = [
        (alternate_url, str(failed_file)),
        (shared_url, str(third_party_file)),
        ("https://other.example/unwritable.jar", str(local_failure_file)),
    ]
    service._context = Mock(
        return_value=SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_: candidates))
    )

    result = service._fallback_download_entries(
        tmp_path,
        "1.21",
        preferred_source,
        {
            (preferred_url, str(failed_file)),
            (shared_url, str(third_party_file)),
            ("https://primary.example/unwritable.jar", str(local_failure_file)),
        },
        {str(local_failure_file)},
    )

    assert result == [(alternate_url, str(failed_file))]
    service._context.assert_called_once()
    assert service._context.call_args.args[1] != preferred_source
    service.close()


@pytest.mark.parametrize("preferred_source", ["official", "bmclapi"])
@pytest.mark.parametrize("alternate_fails", [False, True])
def test_install_retries_only_failed_file_with_alternate_source(
    tmp_path: Path, preferred_source: str, alternate_fails: bool
) -> None:
    first_path = str(tmp_path / "libraries" / "first.jar")
    failed_path = str(tmp_path / "libraries" / "second.jar")
    primary_files = [("https://primary/first.jar", first_path), ("https://primary/second.jar", failed_path)]
    alternate_files = [("https://alternate/first.jar", first_path), ("https://alternate/second.jar", failed_path)]
    download_lists: list[list[tuple[str, str]]] = []
    events: list[dict] = []
    bus = EventBus()
    bus.subscribe("game:install_progress", events.append)

    class PartialDownloader:
        def __init__(self, download_list, progress_callback=None, **_options):
            self.download_list = download_list
            self.progress_callback = progress_callback
            self.completed_entries: set[tuple[str, str]] = set()
            self.failed_entries: set[tuple[str, str]] = set()
            self.downloaded_bytes = 0
            self.total_bytes = len(download_list)
            self.total_files = len(download_list)
            self.use_byte_progress = False
            download_lists.append(download_list)

        async def run(self):
            if len(download_lists) == 1:
                self.completed_entries.add(primary_files[0])
                self.failed_entries.add(primary_files[1])
            elif alternate_fails:
                self.failed_entries.add(alternate_files[1])
            else:
                self.completed_entries.add(alternate_files[1])
            self.downloaded_bytes = len(self.completed_entries)
            if self.progress_callback:
                self.progress_callback(self.downloaded_bytes, self.total_bytes)

        def stop(self):
            pass

    service = GameService(Mock(), downloader_factory=PartialDownloader, event_bus=bus, enable_version_watcher=False)

    def context(_game_path, source):
        if source == preferred_source:
            return SimpleNamespace(games=SimpleNamespace(build_minecraft_download_list=lambda *_: primary_files))
        return SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_: alternate_files))

    service._context = Mock(side_effect=context)

    async def install():
        result = service.install_version(
            {"version_id": "1.21", "task_id": "fallback-task"}, game_path=tmp_path, source=preferred_source
        )
        await service._install_tasks[result["taskId"]]

    asyncio.run(install())

    assert download_lists == [primary_files, [alternate_files[1]]]
    assert events[-1]["phase"] == ("error" if alternate_fails else "done")
    if alternate_fails:
        assert events[-1]["errorCode"] == "GAME_DOWNLOAD_FAILED"
    service.close()


def test_instance_repair_uses_preferred_source_then_retries_failed_file(tmp_path: Path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21"
    version_path.mkdir(parents=True)
    (version_path / "1.21.json").write_text("{}", encoding="utf-8")
    file_path = str(tmp_path / "client.jar")
    primary_entry = ("https://bmclapi.example/client.jar", file_path)
    alternate_entry = ("https://official.example/client.jar", file_path)
    download_lists: list[list[tuple[str, str]]] = []
    checked_sources: list[str] = []

    class RepairDownloader:
        def __init__(self, download_list, progress_callback=None, **_options):
            self.download_list = download_list
            self.progress_callback = progress_callback
            self.failed_entries: set[tuple[str, str]] = set()
            self.completed_entries: set[tuple[str, str]] = set()
            download_lists.append(download_list)

        async def run(self):
            if len(download_lists) == 1:
                self.failed_entries.add(primary_entry)
            else:
                self.completed_entries.add(alternate_entry)
            if self.progress_callback:
                self.progress_callback(len(self.completed_entries), len(self.download_list))

        def stop(self):
            pass

    service = GameService(Mock(), downloader_factory=RepairDownloader, enable_version_watcher=False, data_path=tmp_path)

    def context(_game_path, source):
        checked_sources.append(source)
        entries = [primary_entry] if source == "bmclapi" else [alternate_entry]
        return SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_: entries))

    service._context = Mock(side_effect=context)
    operation = service.repair_instance_files(game_path, "1.21", source="bmclapi")
    service._application_operations._operations[operation["operationId"]].future.result(timeout=3)

    assert service.operation_get(operation["operationId"])["status"] == "completed"
    assert checked_sources == ["bmclapi", "official"]
    assert download_lists == [[primary_entry], [alternate_entry]]
    service.close()
