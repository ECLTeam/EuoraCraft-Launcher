# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 game_service 模块的自动化测试。
#
# 公开接口：
#   - class FakeAccounts
#       - current_account() -> Any
#       - get_launch_credentials() -> Any
#   - class EmptySearchMinecraft
#       - search_minecraft() -> Any
#   - class FakeProcess
#       - poll() -> Any
#   - class FakeInstances
#       - create_instance(**options) -> Any
#       - exit_instance(instance_id, exit_code=…) -> Any
#       - get_instances_info() -> Any
#       - stop_instance(instance_id, **_options) -> Any
#       - request_instance_exit(instance_id, **options) -> Any
#       - shutdown_all(**options) -> Any
#   - class FakeDownloader
#       - run() -> Any
#       - stop() -> Any
#   - test_scan_versions_is_owned_by_game_service(tmp_path) -> None
#   - test_version_directory_change_invalidates_cache_and_emits_event(tmp_path) -> None
#   - test_java_scanner_result_is_exposed_and_used_for_required_version(tmp_path) -> None
#   - test_automatic_java_selection_uses_nearest_compatible_higher_version(tmp_path) -> None
#   - test_install_version_builds_and_downloads_with_progress(tmp_path, monkeypatch) -> None
#   - test_install_version_reports_downloader_failures(tmp_path, monkeypatch) -> None
#   - test_install_version_returns_immediately_and_rejects_duplicate_task(tmp_path, monkeypatch) -> None
#   - test_install_version_rejects_incomplete_loader_options(tmp_path) -> None
#   - test_install_version_fabric_allows_empty_loader_version(tmp_path, monkeypatch) -> None
#   - test_launch_instance_checks_files_builds_command_and_tracks_process(tmp_path, monkeypatch) -> None
#   - test_launch_fullscreen_rewrites_existing_key_and_keeps_other_options(tmp_path, monkeypatch) -> None
#   - test_launch_fullscreen_off_creates_options_file_when_missing(tmp_path, monkeypatch) -> None
#   - test_launch_fullscreen_uses_versions_directory_when_isolated(tmp_path, monkeypatch) -> None
#   - test_close_keeps_running_game_instances_alive_and_settles_observed_duration(tmp_path, monkeypatch) -> None
#   - test_stop_instance_removes_runtime_record_and_settles_duration(tmp_path, monkeypatch) -> None
#   - test_clean_exit_after_startup_marker_does_not_trigger_crash(tmp_path, monkeypatch) -> None
#   - test_launch_handles_process_that_exits_before_instance_registration_finishes(tmp_path, monkeypatch) -> None
#   - test_launch_environment_rejects_java_below_minimum(tmp_path) -> None
#   - test_launch_environment_accepts_java_newer_than_minimum(tmp_path) -> None
#   - test_command_builder_rejects_missing_inherited_version_metadata(tmp_path) -> None
#   - test_command_builder_places_neoforge_game_arguments_after_main_class(tmp_path) -> None
#   - test_command_builder_lock_memory_pins_initial_heap_to_max(tmp_path) -> None
#   - test_launch_downloads_missing_inherited_version_metadata_before_file_check(tmp_path, monkeypatch) -> None
#   - test_launch_environment_rejects_large_memory_with_32_bit_java(tmp_path) -> None
#   - test_fallback_required_java_tracks_minecraft_runtime_generations(game_version, expected) -> None
#   - test_concurrent_runs_accumulate_independently(tmp_path, monkeypatch) -> None
#   - test_authlib_launch_passes_injector_to_game_backend(tmp_path, monkeypatch) -> None
#   - test_microsoft_launch_reports_token_refresh_progress(tmp_path, monkeypatch) -> None
#   - test_cancel_launch_stops_active_file_download(tmp_path, monkeypatch) -> None
#   - test_launch_retries_only_failed_game_file_from_alternate_source(tmp_path, monkeypatch) -> None
#   - test_launch_instance_reports_core_error_details(tmp_path, monkeypatch) -> None
#   - test_uninstall_version_only_removes_selected_version(tmp_path) -> None
#   - test_ecl_config_read_write_and_patch(tmp_path) -> None
#   - test_ecl_config_skips_unchanged_writes(tmp_path, monkeypatch) -> None
#   - test_ecl_config_serializes_concurrent_identical_patches(tmp_path, monkeypatch) -> None
#   - test_ecl_active_version_get_and_set(tmp_path) -> None
#   - test_ecl_config_rejects_invalid_data(tmp_path) -> None
#   - test_ecl_config_handles_corrupted_file(tmp_path) -> None
#   - test_version_settings_stored_in_versions_own_directory(tmp_path) -> None
#   - test_version_settings_skips_unchanged_writes_and_rejects_invalid(tmp_path, monkeypatch) -> None
#   - test_local_mod_lifecycle_stays_inside_mods_directory(tmp_path) -> None
#   - test_download_resource_to_path_saves_to_target(tmp_path, monkeypatch) -> None
#   - test_download_resource_to_path_rejects_unsupported_source_and_missing_dir(tmp_path) -> None
# ============================================================

from __future__ import annotations

import asyncio
import json
import shlex
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ECL.foundation import EventBus
from ECL.game import GetGames, LaunchConfig, SearchMinecraft, build_minecraft_cmd
from ECL.services.game import GameService, GameServiceError


class FakeAccounts:
    def current_account(self):
        return {"id": "offline", "type": "offline"}

    async def get_launch_credentials(self):
        return {
            "player_name": "Steve",
            "uuid": "0123456789abcdef0123456789abcdef",
            "user_type": "legacy",
            "access_token": "None",
        }


class EmptySearchMinecraft:
    def __init__(self, _path):
        pass

    def search_minecraft(self):
        return {}


class FakeProcess:
    def __init__(self):
        self.running = True
        self.pid = 24680

    def poll(self):
        return None if self.running else 0


class FakeInstances:
    def __init__(self):
        self.items = []
        self.created = None
        self.exit_requests = []
        self.shutdown_calls = []
        self._create_count = 0

    def create_instance(self, **options):
        self._create_count += 1
        instance_id = "minecraft-instance" if self._create_count == 1 else f"minecraft-instance-{self._create_count}"
        self.created = options
        process = FakeProcess()
        self.items.append(
            {
                "ID": instance_id,
                "Name": options["instance_name"],
                "Type": options["instance_type"],
                "Instance": process,
                "ExitCallback": options["exit_callback"],
            }
        )
        return instance_id, process

    def exit_instance(self, instance_id, exit_code=0):
        for item in list(self.items):
            if item["ID"] != instance_id:
                continue
            item["Instance"].running = False
            self.items.remove(item)
            item["ExitCallback"](exit_code, item["Name"])
            return

    def get_instances_info(self):
        return list(self.items)

    def stop_instance(self, instance_id, **_options):
        self.exit_requests.append((instance_id, _options))
        for item in self.items:
            if item["ID"] == instance_id:
                item["Instance"].running = False
        return True

    def request_instance_exit(self, instance_id, **options):
        self.exit_requests.append((instance_id, options))
        return self.stop_instance(instance_id, **options)

    def shutdown_all(self, **options):
        self.shutdown_calls.append(options)


class FakeDownloader:
    def __init__(self, download_list, progress_callback=None, **_options):
        self.download_list = download_list
        self.progress_callback = progress_callback
        self.failed_entries = set()
        self.client = None
        self.concurrency = 0
        self.semaphore = None
        self.downloaded_bytes = 0
        self.total_bytes = len(download_list)
        self.use_byte_progress = False
        self.total_files = len(download_list)
        self.completed_entries: set[tuple[str, str]] = set()

    async def run(self):
        if self.progress_callback:
            self.completed_entries.update((url, name) for url, name in self.download_list)
            self.downloaded_bytes = self.total_bytes
            self.progress_callback(len(self.download_list), len(self.download_list))

    def stop(self):
        return None


def _build_service(**options) -> GameService:
    return GameService(
        FakeAccounts(),
        search_factory=options.pop("search_factory", EmptySearchMinecraft),
        instances_manager=options.pop("instances_manager", FakeInstances()),
        downloader_factory=options.pop("downloader_factory", FakeDownloader),
        **options,
    )


@pytest.mark.parametrize(
    ("policy", "mode", "isolated"),
    [
        ("all", "inherit", True),
        ("disabled", "inherit", False),
        ("modded_only", "inherit", True),
        ("disabled", "enabled", True),
        ("all", "disabled", False),
    ],
)
def test_fabric_api_download_follows_effective_isolation(tmp_path, monkeypatch, policy, mode, isolated):
    game_path = tmp_path / ".minecraft"
    save_name = "26.3-fabric"
    api_url = "https://example.test/fabric-api.jar"

    class WritingDownloader(FakeDownloader):
        async def run(self):
            for _url, filename in self.download_list:
                target = Path(filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"api")
            await super().run()

    client = SimpleNamespace(
        get_minecraft_manifest=lambda: {"latest": {}, "versions": [{"id": "26.3", "type": "release", "sha1": "hash"}]},
        get_minecraft_json=lambda *_: {"id": "26.3"},
        get_fabric_profile=lambda **_: {
            "id": save_name,
            "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
        },
    )
    checker = SimpleNamespace(config=None, api_client=client, check_files=lambda *_: [])
    games = GetGames(checker, None, game_path)
    service = _build_service(
        search_factory=SearchMinecraft,
        downloader_factory=WritingDownloader,
        isolation_policy_provider=lambda: policy,
    )
    service.write_version_settings(game_path, save_name, {"isolationMode": mode})
    # 新版本尚未在已有扫描缓存内，不能因其他版本的缓存非空而误判成原版。
    service._version_scan_cache[service._version_path_key(game_path)] = [
        {"versionId": "Other", "primaryLoader": "Vanilla"}
    ]
    monkeypatch.setattr(service, "_context", lambda *_: SimpleNamespace(games=games))
    monkeypatch.setattr(service, "_resolve_fabric_api", lambda *_: (api_url, "fabric-api.jar"))
    events = []
    try:
        service.install_blocking(
            "fabric-test",
            "26.3",
            save_name,
            "fabric",
            "0.16",
            None,
            game_path,
            "official",
            None,
            lambda phase, message, **details: events.append({"phase": phase, **details}),
        )
        data_path = game_path / "versions" / save_name if isolated else game_path / "versions"
        assert (data_path / "mods/fabric-api.jar").read_bytes() == b"api"
        assert not (game_path / "mods").exists()
        other_path = game_path / "versions" if isolated else game_path / "versions" / save_name
        assert not (other_path / "mods/fabric-api.jar").exists()
        assert events[-1]["phase"] == "done"
    finally:
        service.close()


@pytest.mark.parametrize("filename", ["../escape.jar", "..\\escape.jar", "/escape.jar"])
def test_fabric_api_rejects_unsafe_filename(tmp_path, monkeypatch, filename):
    service = _build_service(isolation_policy_provider=lambda: "all")
    monkeypatch.setattr(
        service, "_context", lambda *_: SimpleNamespace(games=SimpleNamespace(build_fabric_download_list=lambda *_: []))
    )
    monkeypatch.setattr(service, "_resolve_fabric_api", lambda *_: ("https://example.test/api.jar", filename))
    with pytest.raises(GameServiceError, match="资源"):
        service.install_blocking(
            "unsafe", "26.3", "Fabric", "fabric", "0.16", None, tmp_path, "official", None, lambda *_a, **_k: None
        )


@pytest.mark.parametrize(("override", "isolated"), [(None, True), (False, False)])
def test_online_service_uses_saved_isolation_and_keeps_download_target(tmp_path, monkeypatch, override, isolated):
    service = _build_service(isolation_policy_provider=lambda: "all")
    downloaded = []

    def select_file(*_args):
        # 目标在安装开始时确定；远端查询期间用户改变设置不会改写本次下载的目标。
        service.write_version_settings(tmp_path, "Fabric", {"isolationMode": "disabled"})
        return {"filename": "mod.jar", "url": "https://example.test/mod.jar", "hashes": {}}

    def download(_url, target, *_args):
        downloaded.append(target)
        target.write_bytes(b"mod")

    monkeypatch.setattr(service, "_select_online_file", select_file)
    monkeypatch.setattr(service, "_download_online_file", download)
    try:
        result = service.install_online_resource(tmp_path, "Fabric", "mod", "modrinth", "project", "file", override)
        data_path = tmp_path / "versions/Fabric" if isolated else tmp_path / "versions"
        assert (data_path / "mods/mod.jar").read_bytes() == b"mod"
        assert downloaded == [data_path / "mods/.mod.jar.ecl-download"]
        assert result["filename"] == "mod.jar"
    finally:
        service.close()


def test_online_hash_failure_preserves_existing_files_and_reports_failure(tmp_path, monkeypatch):
    service = _build_service(isolation_policy_provider=lambda: "all")
    root = tmp_path / "versions/Fabric/mods"
    root.mkdir(parents=True)
    (root / "keep.jar").write_bytes(b"keep")
    monkeypatch.setattr(
        service,
        "_select_online_file",
        lambda *_: {"filename": "new.jar", "url": "https://example.test/new.jar", "hashes": {"sha512": "wrong"}},
    )
    monkeypatch.setattr(service, "_download_online_file", lambda _url, target, *_: target.write_bytes(b"bad"))
    events = []
    service.events.subscribe("game:install_progress", events.append)
    try:
        with pytest.raises(GameServiceError) as error:
            service.install_online_resource(
                tmp_path, "Fabric", "mod", "modrinth", "project", "file", task_id="hash-test"
            )
        assert error.value.error_code == "RESOURCE_HASH_MISMATCH"
        assert (root / "keep.jar").read_bytes() == b"keep"
        assert not (root / "new.jar").exists()
        assert not (root / ".new.jar.ecl-download").exists()
        assert events[-1]["phase"] == "error"
        assert not any(event["phase"] == "done" for event in events)
    finally:
        service.close()


def _launch_lifecycle_fixture(tmp_path, monkeypatch, clock):
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    instances = FakeInstances()
    service = _build_service(
        instances_manager=instances,
        command_builder=lambda _config: '"java.exe" game.Main',
    )
    monkeypatch.setattr("ECL.services.game.launch.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )
    result = asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
        )
    )
    instances.created["log_callback"]("[Render thread/INFO]: Sound engine started", result["instanceId"])
    return service, instances, game_path, java_path, result


def test_scan_versions_is_owned_by_game_service(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    (game_path / "versions").mkdir(parents=True)
    requested_paths = []

    class FakeSearchMinecraft:
        def __init__(self, path):
            requested_paths.append(path)

        def search_minecraft(self):
            return {}

    service = _build_service(search_factory=FakeSearchMinecraft)

    assert service.scan_versions([str(game_path)]) == []
    assert service.scan_versions([str(game_path)]) == []
    assert requested_paths == [game_path]

    assert service.scan_versions([str(game_path)], force=True) == []
    assert requested_paths == [game_path, game_path]


def test_version_directory_change_invalidates_cache_and_emits_event(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    versions_path = game_path / "versions"
    versions_path.mkdir(parents=True)
    scan_count = 0

    class FakeSearchMinecraft:
        def __init__(self, _path):
            pass

        def search_minecraft(self):
            nonlocal scan_count
            scan_count += 1
            return {}

    service = _build_service(
        search_factory=FakeSearchMinecraft,
        enable_version_watcher=True,
        version_watch_interval=60,
        version_watch_debounce=0,
        event_bus=(event_bus := EventBus()),
    )
    events = []
    event_bus.subscribe("game:versions_changed", events.append)
    try:
        service.scan_versions([str(game_path)])
        (versions_path / "1.21.1").mkdir()

        changed_paths = service._poll_version_changes(now=1)

        assert changed_paths == [str(game_path.resolve())]
        assert events == [{"gamePath": str(game_path.resolve())}]
        service.scan_versions([str(game_path)])
        assert scan_count == 2
    finally:
        service.close()


def test_java_scanner_result_is_exposed_and_used_for_required_version(tmp_path) -> None:
    java8 = SimpleNamespace(
        path=tmp_path / "java8.exe",
        version="1.8.0_451",
        vendor="Eclipse Adoptium",
        architecture="amd64",
        is_jdk=False,
    )
    java21 = SimpleNamespace(
        path=tmp_path / "java21.exe",
        version="21.0.7",
        vendor="Microsoft",
        architecture="aarch64",
        is_jdk=True,
    )
    scanner_options = {}

    class FakeJavaScanner:
        def __init__(self, **options):
            scanner_options.update(options)

        def scan(self):
            return [java8, java21]

    service = _build_service(java_scanner_factory=FakeJavaScanner, data_path=tmp_path)

    installations = service.scan_java([str(java8.path)])

    assert [item["major_version"] for item in installations] == [21, 8]
    assert installations[0]["arch"] == "arm64"
    assert installations[1]["sources"] == ["user"]
    assert scanner_options["cache_file"] == tmp_path / "java_cache.json"
    assert service._resolve_java_path(None, 8) == str(java8.path)


def test_automatic_java_selection_uses_nearest_compatible_higher_version(tmp_path) -> None:
    java21 = SimpleNamespace(
        path=tmp_path / "java21.exe",
        version="21.0.7",
        vendor="Microsoft",
        architecture="amd64",
        is_jdk=True,
    )
    java25 = SimpleNamespace(
        path=tmp_path / "java25.exe",
        version="25.0.1",
        vendor="Eclipse Adoptium",
        architecture="amd64",
        is_jdk=True,
    )

    class FakeJavaScanner:
        def __init__(self, **_options):
            pass

        def scan(self):
            return [java25, java21]

    service = _build_service(java_scanner_factory=FakeJavaScanner)

    assert service._resolve_java_path(None, 17) == str(java21.path)
    assert service._resolve_java_path(None, 21) == str(java21.path)


def test_install_version_builds_and_downloads_with_progress(tmp_path, monkeypatch) -> None:
    events = []
    event_bus = EventBus()
    event_bus.subscribe("game:install_progress", events.append)
    built = []

    class FakeGames:
        def build_minecraft_download_list(self, version_id, save_name):
            built.append((version_id, save_name))
            return [("https://example.com/client.jar", str(tmp_path / "client.jar"))]

    service = _build_service(event_bus=event_bus)
    service.logger = Mock()
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(games=FakeGames()),
    )

    async def install():
        result = service.install_version(
            {
                "version_id": "1.21.8",
                "version_name": "My 1.21.8",
                "task_id": "install-task",
            },
            game_path=tmp_path,
        )
        await service._install_tasks[result["taskId"]]
        return result

    result = asyncio.run(install())

    assert result == {
        "taskId": "install-task",
        "versionId": "1.21.8",
        "versionName": "My 1.21.8",
    }
    assert built == [("1.21.8", "My 1.21.8")]
    assert [event["phase"] for event in events] == ["install", "download", "download", "done"]
    assert events[-1]["task_id"] == "install-task"
    log_messages = [call.args[0] for call in service.logger.info.call_args_list]
    assert any("开始安装" in message for message in log_messages)
    assert any("版本安装完成" in message for message in log_messages)


def test_install_version_reports_downloader_failures(tmp_path, monkeypatch) -> None:
    events = []
    event_bus = EventBus()
    event_bus.subscribe("game:install_progress", events.append)

    class FailedDownloader(FakeDownloader):
        async def run(self):
            self.failed_entries.add(("https://example.com/client.jar", "client.jar"))

    service = _build_service(downloader_factory=FailedDownloader, event_bus=event_bus)
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(
            games=SimpleNamespace(
                build_minecraft_download_list=lambda *_args: [
                    ("https://example.com/client.jar", str(tmp_path / "client.jar"))
                ]
            )
        ),
    )

    async def install():
        result = service.install_version(
            {"version_id": "1.21.8", "task_id": "failed-task"},
            game_path=tmp_path,
        )
        await service._install_tasks[result["taskId"]]

    asyncio.run(install())

    assert events[-1]["phase"] == "error"
    assert events[-1]["errorCode"] == "GAME_DOWNLOAD_FAILED"
    assert "client.jar" in events[-1]["message"]


def test_install_version_returns_immediately_and_rejects_duplicate_task(tmp_path, monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingDownloader(FakeDownloader):
        async def run(self):
            started.set()
            await release.wait()
            await super().run()

    service = _build_service(downloader_factory=BlockingDownloader)
    service.logger = Mock()
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(
            games=SimpleNamespace(
                build_minecraft_download_list=lambda *_args: [
                    ("https://example.com/client.jar", str(tmp_path / "client.jar"))
                ]
            )
        ),
    )

    async def scenario():
        result = service.install_version(
            {"version_id": "1.21.8", "task_id": "background-install"},
            game_path=tmp_path,
        )
        task_id = result["taskId"]
        assert result == {
            "taskId": "background-install",
            "versionId": "1.21.8",
            "versionName": "1.21.8",
        }
        assert started.is_set() is False
        assert any("开始安装" in call.args[0] for call in service.logger.info.call_args_list)

        await asyncio.wait_for(started.wait(), timeout=1)
        install_task = service._install_tasks[task_id]
        assert install_task.done() is False
        with pytest.raises(GameServiceError) as error:
            service.install_version(
                {"version_id": "1.21.8", "task_id": task_id},
                game_path=tmp_path,
            )
        assert error.value.error_code == "INSTALL_ALREADY_RUNNING"

        release.set()
        await install_task
        await asyncio.sleep(0)
        assert task_id not in service._install_tasks

    asyncio.run(scenario())


def test_install_version_rejects_incomplete_loader_options(tmp_path) -> None:
    service = _build_service()

    with pytest.raises(GameServiceError) as error:
        service.install_version(
            {"version_id": "1.21.8", "loader_type": "forge"},
            game_path=tmp_path,
        )

    assert error.value.error_code == "LOADER_VERSION_REQUIRED"
    assert service._install_tasks == {}


def test_install_version_fabric_allows_empty_loader_version(tmp_path, monkeypatch) -> None:
    service = _build_service()
    service.logger = Mock()
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(
            games=SimpleNamespace(
                build_fabric_download_list=lambda *_args: [
                    ("https://example.com/client.jar", str(tmp_path / "client.jar"))
                ]
            )
        ),
    )

    async def install():
        result = service.install_version(
            {"version_id": "1.21.8", "loader_type": "fabric", "task_id": "fabric-empty"},
            game_path=tmp_path,
        )
        await service._install_tasks[result["taskId"]]

    asyncio.run(install())

    assert service._install_tasks == {}


def test_launch_instance_checks_files_builds_command_and_tracks_process(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    instances = FakeInstances()
    captured_configs = []
    clock = [100.0]
    monkeypatch.setattr("ECL.services.game.launch.monotonic", lambda: clock[0])
    service = _build_service(
        instances_manager=instances,
        command_builder=lambda config: captured_configs.append(config) or '"java.exe" game.Main',
    )
    service._search_factory = lambda _path: SimpleNamespace(
        search_minecraft=lambda: {"1.21.8": {"LoaderType": "NeoForge"}}
    )
    crashes = []
    service.events.subscribe("launcher:error", crashes.append)
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(
            files_checker=SimpleNamespace(check_files=lambda *_args: []),
        ),
    )

    instance_id = asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            memory=6144,
            width=1280,
            height=720,
            jvm_args=["-Dexample=true"],
            game_args=["--demo", "hello world"],
        )
    )

    assert instance_id == {
        "instanceId": "minecraft-instance",
        "versionId": "1.21.8",
        "gamePath": str(game_path.resolve()),
    }
    assert captured_configs[0].player_name == "Steve"
    assert captured_configs[0].use_ram == 6144
    assert shlex.split(instances.created["args"])[-2:] == ["--demo", "hello world"]
    assert instances.created["cwd"] == version_path
    assert instances.created["new_session"] is True
    assert service.list_instances() == [
        {
            "id": "minecraft-instance",
            "name": "1.21.8",
            "type": "Minecraft",
            "isRunning": True,
            "pid": 24680,
            "version": "1.21.8",
            "versionId": "1.21.8",
            "loader": "NeoForge",
            "gamePath": str(game_path.resolve()),
        }
    ]
    stats_file = version_path / "eclversion.json"
    launch_stats = json.loads(stats_file.read_text(encoding="utf-8"))
    assert launch_stats["launchCount"] == 1
    assert launch_stats["lastRunDurationSeconds"] == 0
    assert launch_stats["totalRunDurationSeconds"] == 0
    assert launch_stats["lastLaunchedAt"]

    exit_callback = instances.created["exit_callback"]
    clock[0] = 142.9
    instances.exit_instance("minecraft-instance", exit_code=1)
    exit_callback(1, "1.21.8")

    assert service.list_instances() == []
    settled_stats = service.get_version_stats(game_path, "1.21.8")
    assert settled_stats["launchCount"] == 1
    assert settled_stats["lastRunDurationSeconds"] == 42
    assert settled_stats["totalRunDurationSeconds"] == 42
    deadline = time.monotonic() + 3
    while not crashes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(crashes) == 1
    assert crashes[0]["title"] == "Minecraft 实例崩溃"
    assert crashes[0]["message"] == "实例“1.21.8”异常退出，退出码：1"
    assert crashes[0]["error_id"]
    assert crashes[0]["kind"] == "game_crash"
    assert crashes[0]["crash"]["reportId"] == crashes[0]["error_id"]
    service.close()


def _fullscreen_launch_fixture(tmp_path, monkeypatch):
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text('{"id":"1.21.8"}', encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    service = _build_service(
        instances_manager=FakeInstances(),
        command_builder=lambda _config: '"java.exe" game.Main',
    )
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )
    return service, game_path, java_path


def test_launch_runs_pre_launch_command_in_final_game_directory(tmp_path, monkeypatch) -> None:
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    executed: list[tuple[str, object]] = []
    monkeypatch.setattr(service, "_run_pre_launch_command", lambda command, cwd: executed.append((command, cwd)))

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            pre_launch_command="prepare-world.cmd",
        )
    )

    assert executed == [("prepare-world.cmd", game_path / "versions" / "1.21.8")]
    service.close()


def test_pre_launch_command_rejects_nonzero_exit_code(tmp_path, monkeypatch) -> None:
    service = _build_service()
    monkeypatch.setattr(
        "ECL.services.game.launch.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="", stderr="failed", returncode=3),
    )

    with pytest.raises(GameServiceError, match="退出码: 3") as raised:
        service._run_pre_launch_command("exit 3", tmp_path)

    assert raised.value.error_code == "PRE_LAUNCH_COMMAND_FAILED"
    service.close()


def test_javaw_is_replaced_with_java_exe_when_enabled_on_windows(tmp_path, monkeypatch) -> None:
    from ECL.services.game import launch as launch_module

    service = _build_service()
    javaw_path = tmp_path / "javaw.exe"
    java_path = tmp_path / "java.exe"
    javaw_path.write_bytes(b"")
    java_path.write_bytes(b"")
    monkeypatch.setattr(launch_module.sys, "platform", "win32")

    assert service._prefer_java_executable(str(javaw_path), True) == str(java_path.resolve())
    service.close()


def test_high_performance_gpu_preference_uses_current_user_registry(tmp_path, monkeypatch) -> None:
    from ECL.services.game import launch as launch_module

    class FakeRegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1

        def __init__(self):
            self.values: dict[str, str] = {}

        def CreateKey(self, *_args):  # noqa: N802 - mirror winreg's public API.
            return FakeRegistryKey()

        def QueryValueEx(self, _key, name):  # noqa: N802 - mirror winreg's public API.
            if name not in self.values:
                raise FileNotFoundError
            return self.values[name], self.REG_SZ

        def SetValueEx(self, _key, name, _reserved, _kind, value):  # noqa: N802 - mirror winreg's public API.
            self.values[name] = value

    service = _build_service()
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    registry = FakeWinreg()
    monkeypatch.setattr(launch_module.sys, "platform", "win32")
    monkeypatch.setattr(launch_module, "winreg", registry)

    service._set_high_performance_gpu_preference(str(java_path))

    assert registry.values[str(java_path.resolve())] == "GpuPreference=2;"
    service.close()


def test_disabled_crash_analysis_skips_automatic_report(tmp_path, monkeypatch) -> None:
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    errors: list[dict] = []
    service.events.subscribe("launcher:error", errors.append)

    result = asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            disable_crash_analysis=True,
        )
    )
    service.instances.exit_instance(result["instanceId"], exit_code=1)

    assert errors == []
    service.close()


def test_renderer_agent_downloads_matching_windows_artifact(tmp_path, monkeypatch) -> None:
    downloads: list[list[tuple[str, object]]] = []

    class RendererDownloader(FakeDownloader):
        def __init__(self, download_list, **options):
            super().__init__(download_list, **options)
            downloads.append(download_list)

        async def run(self):
            for _url, target in self.download_list:
                target_path = Path(target)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(b"mesa-loader")

    service = _build_service(data_path=tmp_path, downloader_factory=RendererDownloader)
    monkeypatch.setattr("ECL.services.game.launch.sys.platform", "win32")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "ARM64")

    agent = asyncio.run(service._ensure_renderer_agent("vulkan"))

    assert agent == f'-javaagent:"{tmp_path / "mesa-loader-windows" / "26.0.4" / "Loader.jar"}"=zink'
    assert downloads[0][0][0].endswith("mesa-loader-windows-26.0.4-arm64.jar")
    service.close()


def test_renderer_agent_is_not_enabled_outside_windows(tmp_path, monkeypatch) -> None:
    service = _build_service(data_path=tmp_path)
    monkeypatch.setattr("ECL.services.game.launch.sys.platform", "linux")

    assert asyncio.run(service._ensure_renderer_agent("software")) is None
    service.close()


def test_launch_injects_renderer_agent_before_custom_jvm_args(tmp_path, monkeypatch) -> None:
    captured_configs = []
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    service._command_builder = lambda config: captured_configs.append(config) or '"java.exe" game.Main'

    async def renderer_agent(_renderer):
        return '-javaagent:"C:\\ECL\\Loader.jar"=d3d12'

    monkeypatch.setattr(service, "_ensure_renderer_agent", renderer_agent)
    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            renderer="directx12",
            jvm_args=["-XX:+UseG1GC"],
        )
    )

    assert captured_configs[0].custom_jvm_params == [
        '-javaagent:"C:\\ECL\\Loader.jar"=d3d12',
        "-XX:+UseG1GC",
    ]
    service.close()


def test_launch_fullscreen_rewrites_existing_key_and_keeps_other_options(tmp_path, monkeypatch) -> None:
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    options_path = game_path / "versions" / "options.txt"
    options_path.write_text("lang:zh_CN\nfullscreen:false\nmusicVolume:0.8\n", encoding="utf-8")

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            fullscreen=True,
            version_isolation=False,
        )
    )

    assert options_path.read_text(encoding="utf-8") == "lang:zh_CN\nfullscreen:true\nmusicVolume:0.8\n"
    service.close()


def test_launch_fullscreen_off_creates_options_file_when_missing(tmp_path, monkeypatch) -> None:
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            version_isolation=False,
        )
    )

    options_path = game_path / "versions" / "options.txt"
    assert options_path.read_text(encoding="utf-8") == "fullscreen:false\nlang:zh_cn\n"
    service.close()


def test_launch_fullscreen_uses_versions_directory_when_isolated(tmp_path, monkeypatch) -> None:
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
            fullscreen=True,
            version_isolation=True,
        )
    )

    assert (game_path / "versions" / "1.21.8" / "options.txt").read_text(
        encoding="utf-8"
    ) == "fullscreen:true\nlang:zh_cn\n"
    assert not (game_path / "versions" / "options.txt").exists()
    service.close()


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("initial", [None, "renderDistance:12\n", "lang:  \nrenderDistance:12\n"])
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("zh-CN", "zh_cn"),
        ("zh-TW", "zh_tw"),
        ("en-US", "en_us"),
        ("ja-JP", "ja_jp"),
        ("ru-RU", "ru_ru"),
        ("de-DE", "de_de"),
    ],
)
def test_launch_initializes_only_unconfigured_language(tmp_path, monkeypatch, platform, initial, locale, expected):
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("ECL.services.game.launch.sys.platform", platform)
    service._search_factory = lambda _: SimpleNamespace(
        search_minecraft=lambda: {"1.21.8": {"VanillaVersion": "1.21.8"}}
    )
    monkeypatch.setattr(service, "_probe_java_runtime", lambda _: {"version": "21", "architecture": "amd64"})
    options_path = game_path / "versions" / "1.21.8" / "options.txt"
    if initial is not None:
        options_path.write_text(initial, encoding="utf-8")
    try:
        asyncio.run(
            service.launch_instance(
                {"version_id": "1.21.8"},
                game_path=game_path,
                java_path=java_path,
                launcher_locale=locale,
                fullscreen=True,
                version_isolation=True,
            )
        )
        lines = options_path.read_text(encoding="utf-8").splitlines()
        assert f"lang:{expected}" in lines
        assert "fullscreen:true" in lines
        if initial is not None:
            assert "renderDistance:12" in lines
    finally:
        service.close()


@pytest.mark.parametrize("existing", ["en_us", "zh_CN", "custom_value", "none"])
@pytest.mark.parametrize("has_saves", [False, True])
def test_launch_preserves_language_across_launcher_language_changes(tmp_path, monkeypatch, existing, has_saves):
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    options_path = game_path / "versions" / "1.21.8" / "options.txt"
    original = f"# settings\r\nlang:{existing}\r\ncustom:keep\r\nfullscreen:false\r\n"
    options_path.write_bytes(original.encode())
    if has_saves:
        (options_path.parent / "saves").mkdir()
    try:
        for locale in ("ja-JP", "de-DE"):
            asyncio.run(
                service.launch_instance(
                    {"version_id": "1.21.8"},
                    game_path=game_path,
                    java_path=java_path,
                    launcher_locale=locale,
                    version_isolation=True,
                )
            )
            assert options_path.read_bytes() == original.encode()
    finally:
        service.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_launch_language_uses_final_directory_and_version_metadata(tmp_path, monkeypatch, isolated):
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    version_dir = game_path / "versions" / "1.21.8"
    renamed_dir = version_dir.with_name("My instance")
    version_dir.rename(renamed_dir)
    (renamed_dir / "1.21.8.json").rename(renamed_dir / "My instance.json")
    (renamed_dir / "My instance.json").write_text('{"id":"1.10.2"}', encoding="utf-8")
    other_dir = game_path / "versions" if isolated else renamed_dir
    (other_dir / "options.txt").write_text("lang:de_DE\n", encoding="utf-8")
    try:
        asyncio.run(
            service.launch_instance(
                {"version_id": "My instance"},
                game_path=game_path,
                java_path=java_path,
                launcher_locale="zh-TW",
                version_isolation=isolated,
            )
        )
        target_dir = renamed_dir if isolated else game_path / "versions"
        assert "lang:zh_TW" in (target_dir / "options.txt").read_text(encoding="utf-8")
        assert (other_dir / "options.txt").read_text(encoding="utf-8") == "lang:de_DE\n"
    finally:
        service.close()


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0", None),
        ("2.0", None),
        ("1.1", "zh_CN"),
        ("1.5.2", "zh_CN"),
        ("1.10.2", "zh_CN"),
        ("1.11", "zh_cn"),
        ("1.21.8", "zh_cn"),
        ("1.11-pre1", "zh_cn"),
        ("26.3", "zh_cn"),
        ("26.4-snapshot-5", "zh_cn"),
        ("26.1-pre-1", "zh_cn"),
        ("26.1-rc-1", "zh_cn"),
        ("11w48a", None),
        ("11w49a", "zh_CN"),
        ("16w29b", "zh_CN"),
        ("16w32a", "zh_cn"),
        ("25w14craftmine", None),
        ("unrecognized", None),
        ("16w99a", None),
    ],
)
def test_launch_language_handles_actual_vanilla_versions(tmp_path, version, expected):
    service = _build_service()
    try:
        service._apply_launch_options(tmp_path, False, "zh-CN", {"VanillaVersion": version}, {})
        text = (tmp_path / "options.txt").read_text(encoding="utf-8")
        if expected is None:
            assert "lang:" not in text
        else:
            assert f"lang:{expected}\n" in text
        assert "fullscreen:false\n" in text
    finally:
        service.close()


@pytest.mark.parametrize("locale", ["", "invalid\nfullscreen:true", 123, ["zh-CN"]])
def test_launch_skips_invalid_language_configuration_without_writing_input(tmp_path, locale):
    service = _build_service()
    service.logger = Mock()
    try:
        service._apply_launch_options(tmp_path, False, locale, {"VanillaVersion": "1.21.8"}, {})
        assert (tmp_path / "options.txt").read_text(encoding="utf-8") == "fullscreen:false\n"
        assert service.logger.warning.call_count == 1
        assert "配置无效" in service.logger.warning.call_args.args[0]
    finally:
        service.close()


@pytest.mark.parametrize(
    ("info", "document", "expected"),
    [
        ({"VanillaVersion": "1.10.2"}, {"id": "1.21.8"}, "zh_CN"),
        ({"VanillaVersion": "Unknown"}, {"id": "renamed", "inheritsFrom": "1.11"}, "zh_cn"),
        ({}, {"id": "1.5.2"}, "zh_CN"),
    ],
)
def test_launch_language_respects_base_version_identity(tmp_path, info, document, expected):
    service = _build_service()
    try:
        service._apply_launch_options(tmp_path, False, None, info, document)
        assert f"lang:{expected}\n" in (tmp_path / "options.txt").read_text(encoding="utf-8")
    finally:
        service.close()


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("failure", ["read", "encoding", "replace"])
def test_launch_options_failure_preserves_file_and_continues_launch(tmp_path, monkeypatch, platform, failure):
    from ECL.services.game import launch as launch_module

    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    service.logger = Mock()
    monkeypatch.setattr(launch_module.sys, "platform", platform)
    options_path = game_path / "versions" / "1.21.8" / "options.txt"
    original = b"custom:keep\nfullscreen:false\n" if failure != "encoding" else b"custom:\xff\n"
    options_path.write_bytes(original)
    read_bytes = Path.read_bytes
    replace = Path.replace
    writes = Mock(wraps=launch_module.atomic_write_text)
    monkeypatch.setattr(launch_module, "atomic_write_text", writes)

    def failing_read(target):
        if target == options_path:
            raise PermissionError(f"private-path:{options_path}")
        return read_bytes(target)

    def failing_replace(source, target):
        if target == options_path:
            raise PermissionError(f"private-path:{options_path}")
        return replace(source, target)

    try:
        with monkeypatch.context() as patch:
            if failure == "read":
                patch.setattr(Path, "read_bytes", failing_read)
            if failure == "replace":
                patch.setattr(Path, "replace", failing_replace)
                patch.setattr("ECL.foundation.files.sleep", lambda _: None)
            result = asyncio.run(
                service.launch_instance(
                    {"version_id": "1.21.8"},
                    game_path=game_path,
                    java_path=java_path,
                    launcher_locale="ja-JP",
                    fullscreen=True,
                    version_isolation=True,
                )
            )
        assert result["instanceId"] == "minecraft-instance"
        assert options_path.read_bytes() == original
        assert writes.call_count == (1 if failure == "replace" else 0)
        assert not list(options_path.parent.glob(".options.txt.*.tmp"))
        assert service.logger.warning.called
        assert str(options_path) not in str(service.logger.warning.call_args_list)
    finally:
        service.close()


@pytest.mark.parametrize(
    "initial",
    [
        b"\xef\xbb\xbflang: \r\n# comment\r\nunknown:a:b",
        b"lang:\nunknown:a:b\nlang: \nfullscreen:false\nfullscreen:false\n",
    ],
)
def test_launch_options_preserves_bom_lines_and_avoids_repeated_writes(tmp_path, monkeypatch, initial):
    from ECL.services.game import launch as launch_module

    service = _build_service()
    options_path = tmp_path / "options.txt"
    options_path.write_bytes(initial)
    writes = Mock(wraps=launch_module.atomic_write_text)
    monkeypatch.setattr(launch_module, "atomic_write_text", writes)
    try:
        service._apply_launch_options(tmp_path, True, "zh-CN", {}, {"id": "1.21.8"})
        content = options_path.read_bytes()
        assert b"lang:zh_cn" in content
        assert b"unknown:a:b" in content
        assert content.startswith(b"\xef\xbb\xbf") == initial.startswith(b"\xef\xbb\xbf")
        assert b"fullscreen:false" not in content
        if b"\r\n" in initial:
            assert b"# comment\r\nunknown:a:b\r\n" in content
        service._apply_launch_options(tmp_path, True, "de-DE", {}, {"id": "1.21.8"})
        assert options_path.read_bytes() == content
        assert writes.call_count == 1
    finally:
        service.close()


@pytest.mark.parametrize("document", [[], None, 123, "invalid"])
def test_launch_rejects_non_object_version_metadata(tmp_path, monkeypatch, document):
    service, game_path, java_path = _fullscreen_launch_fixture(tmp_path, monkeypatch)
    version_dir = game_path / "versions" / "1.21.8"
    (version_dir / "1.21.8.json").write_text(json.dumps(document), encoding="utf-8")
    try:
        with pytest.raises(GameServiceError) as raised:
            asyncio.run(
                service.launch_instance(
                    {"version_id": "1.21.8"},
                    game_path=game_path,
                    java_path=java_path,
                )
            )
        assert raised.value.error_code == "VERSION_JSON_INVALID"
        assert not (version_dir / "options.txt").exists()
    finally:
        service.close()


def test_close_keeps_running_game_instances_alive_and_settles_observed_duration(tmp_path, monkeypatch) -> None:
    clock = [10.0]
    service, instances, game_path, _java_path, _result = _launch_lifecycle_fixture(tmp_path, monkeypatch, clock)

    clock[0] = 25.8

    service.close()

    assert instances.shutdown_calls == []
    assert instances.items[0]["Instance"].running is True
    stats = service.get_version_stats(game_path, "1.21.8")
    assert stats["launchCount"] == 1
    assert stats["lastRunDurationSeconds"] == 15
    assert stats["totalRunDurationSeconds"] == 15


def test_stop_instance_removes_runtime_record_and_settles_duration(tmp_path, monkeypatch) -> None:
    clock = [50.0]
    service, instances, game_path, _java_path, result = _launch_lifecycle_fixture(tmp_path, monkeypatch, clock)
    crashes = []
    service.events.subscribe("launcher:error", crashes.append)

    clock[0] = 58.2
    service.stop_instance(result["instanceId"])

    assert service.list_instances() == []
    assert instances.items[0]["Instance"].running is False
    assert instances.exit_requests == [(result["instanceId"], {"wait_timeout": 3.0})]
    assert crashes == []
    stats = service.get_version_stats(game_path, "1.21.8")
    assert stats["launchCount"] == 1
    assert stats["lastRunDurationSeconds"] == 8
    assert stats["totalRunDurationSeconds"] == 8


def test_clean_exit_after_startup_marker_does_not_trigger_crash(tmp_path, monkeypatch) -> None:
    clock = [50.0]
    service, instances, _game_path, _java_path, result = _launch_lifecycle_fixture(tmp_path, monkeypatch, clock)
    crashes = []
    service.events.subscribe("launcher:error", crashes.append)

    instances.created["log_callback"]("[Render thread/INFO]: Sound engine started", result["instanceId"])
    instances.exit_instance(result["instanceId"], exit_code=0)

    assert crashes == []
    service.close()


def test_launch_handles_process_that_exits_before_instance_registration_finishes(tmp_path, monkeypatch) -> None:
    class ImmediateExitInstances(FakeInstances):
        def create_instance(self, **options):
            instance_id, process = super().create_instance(**options)
            options["exit_callback"](1, options["instance_name"])
            process.running = False
            return instance_id, process

    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "broken"
    version_path.mkdir(parents=True)
    (version_path / "broken.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    events = []
    service = _build_service(
        instances_manager=ImmediateExitInstances(),
        command_builder=lambda _config: '"java.exe" broken.Main',
    )
    service.events.subscribe("launcher:error", events.append)
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )
    monkeypatch.setattr(
        service._crash_analyzer,
        "analyze_runtime",
        lambda **_kwargs: {
            "reportId": "instant-crash",
            "versionId": "broken",
            "exitCode": 1,
            "detectedBy": ["exit_code", "startup_incomplete"],
            "reasons": [],
            "sourceFiles": [],
            "hasOutput": False,
        },
    )

    asyncio.run(service.launch_instance({"version_id": "broken"}, game_path=game_path, java_path=java_path))

    deadline = time.monotonic() + 1
    while not events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events[0]["kind"] == "game_crash"
    assert events[0]["error_id"] == "instant-crash"
    assert service.list_instances() == []
    service.close()


def test_launch_environment_rejects_java_below_minimum(tmp_path) -> None:
    service = _build_service()
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"java")
    service._java_runtimes = [
        SimpleNamespace(path=java_path, version="21.0.8", architecture="amd64", vendor="OpenJDK", is_jdk=True)
    ]

    with pytest.raises(GameServiceError) as error:
        service._validate_launch_environment(str(java_path), 25, 4096)

    assert error.value.error_code == "JAVA_VERSION_INCOMPATIBLE"
    assert "Java 25" in str(error.value)


def test_launch_environment_accepts_java_newer_than_minimum(tmp_path) -> None:
    service = _build_service()
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"java")
    service._java_runtimes = [
        SimpleNamespace(path=java_path, version="26-ea", architecture="amd64", vendor="OpenJDK", is_jdk=True)
    ]

    service._validate_launch_environment(str(java_path), 25, 4096)


def test_command_builder_rejects_missing_inherited_version_metadata(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "neoforge"
    version_path.mkdir(parents=True)
    (version_path / "neoforge.json").write_text(
        json.dumps(
            {
                "id": "neoforge",
                "inheritsFrom": "26.2",
                "mainClass": "net.neoforged.fml.startup.Client",
                "arguments": {"game": ["--fml.neoForgeVersion", "26.2.0.25-beta"]},
                "libraries": [],
            }
        ),
        encoding="utf-8",
    )
    config = LaunchConfig(
        java_path="java.exe",
        game_path=game_path,
        version_name="neoforge",
        use_ram=4096,
        player_name="Player",
        auth_uuid="0" * 32,
    )

    with pytest.raises(FileNotFoundError, match=r"缺少基础版本 26\.2"):
        build_minecraft_cmd(config)


def test_command_builder_places_neoforge_game_arguments_after_main_class(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "neoforge"
    version_path.mkdir(parents=True)
    (version_path / "neoforge.json").write_text(
        json.dumps(
            {
                "id": "neoforge",
                "inheritsFrom": "26.2",
                "mainClass": "net.neoforged.fml.startup.Client",
                "arguments": {"game": ["--fml.neoForgeVersion", "26.2.0.25-beta"]},
                "libraries": [],
            }
        ),
        encoding="utf-8",
    )
    (version_path / "26.2.json").write_text(
        json.dumps(
            {
                "id": "26.2",
                "arguments": {
                    "jvm": ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"],
                    "game": ["--username", "${auth_player_name}"],
                },
                "libraries": [],
            }
        ),
        encoding="utf-8",
    )
    (version_path / "26.2.jar").write_bytes(b"jar")
    config = LaunchConfig(
        java_path="java.exe",
        game_path=game_path,
        version_name="neoforge",
        use_ram=4096,
        player_name="Player",
        auth_uuid="0" * 32,
    )

    command = build_minecraft_cmd(config)

    assert command.index('"net.neoforged.fml.startup.Client"') < command.index("--fml.neoForgeVersion")


def test_command_builder_lock_memory_pins_initial_heap_to_max(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21"
    version_path.mkdir(parents=True)
    (version_path / "1.21.json").write_text(
        json.dumps(
            {
                "id": "1.21",
                "mainClass": "net.minecraft.client.main.Main",
                "arguments": {"jvm": [], "game": []},
                "libraries": [],
            }
        ),
        encoding="utf-8",
    )
    (version_path / "1.21.jar").write_bytes(b"jar")

    unlocked = build_minecraft_cmd(
        LaunchConfig(
            java_path="java.exe",
            game_path=game_path,
            version_name="1.21",
            use_ram=4096,
            lock_memory=False,
            player_name="Player",
            auth_uuid="0" * 32,
        )
    )
    locked = build_minecraft_cmd(
        LaunchConfig(
            java_path="java.exe",
            game_path=game_path,
            version_name="1.21",
            use_ram=4096,
            lock_memory=True,
            player_name="Player",
            auth_uuid="0" * 32,
        )
    )

    # 未锁定时初始堆固定为 256M；锁定时与最大堆一致。
    assert "-Xms256M" in unlocked and "-Xmx4096M" in unlocked
    assert "-Xms4096M" in locked and "-Xmx4096M" in locked
    assert "-Xms256M" not in locked


def test_launch_downloads_missing_inherited_version_metadata_before_file_check(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "neoforge"
    version_path.mkdir(parents=True)
    (version_path / "neoforge.json").write_text(
        json.dumps(
            {
                "id": "neoforge",
                "inheritsFrom": "26.2",
                "mainClass": "net.neoforged.fml.startup.Client",
                "arguments": {"game": ["--fml.neoForgeVersion", "26.2.0.25-beta"]},
                "libraries": [],
            }
        ),
        encoding="utf-8",
    )
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    calls = []

    class FakeGames:
        def build_minecraft_download_list(self, version_id, save_name, save_version_info):
            calls.append((version_id, save_name, save_version_info))
            (version_path / "26.2.json").write_text('{"id":"26.2"}', encoding="utf-8")
            return "release", {"id": "26.2"}

    class InheritanceCheckingFiles:
        def check_files(self, *_args):
            assert (version_path / "26.2.json").is_file()
            return []

    service = _build_service(
        instances_manager=FakeInstances(),
        command_builder=lambda _config: '"java.exe" game.Main',
    )
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=InheritanceCheckingFiles(), games=FakeGames()),
    )

    asyncio.run(service.launch_instance({"version_id": "neoforge"}, game_path=game_path, java_path=java_path))

    assert calls == [("26.2", "neoforge", False)]
    service.close()


def test_launch_environment_rejects_large_memory_with_32_bit_java(tmp_path) -> None:
    service = _build_service()
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"java")
    service._java_runtimes = [
        SimpleNamespace(path=java_path, version="8.0.451", architecture="x86", vendor="OpenJDK", is_jdk=True)
    ]

    with pytest.raises(GameServiceError) as error:
        service._validate_launch_environment(str(java_path), 8, 4096)

    assert error.value.error_code == "JAVA_ARCH_MEMORY_LIMIT"


@pytest.mark.parametrize(
    ("game_version", "expected"),
    [("1.16.5", 8), ("1.17.1", 16), ("1.20.4", 17), ("1.20.5", 21), ("26.2", 25)],
)
def test_fallback_required_java_tracks_minecraft_runtime_generations(game_version, expected) -> None:
    assert GameService._fallback_required_java(game_version) == expected


def test_concurrent_runs_accumulate_independently(tmp_path, monkeypatch) -> None:
    clock = [100.0]
    service, instances, game_path, java_path, first = _launch_lifecycle_fixture(tmp_path, monkeypatch, clock)
    clock[0] = 110.0
    second = asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
        )
    )
    instances.created["log_callback"]("[Render thread/INFO]: Sound engine started", second["instanceId"])

    clock[0] = 125.0
    instances.exit_instance(second["instanceId"])
    clock[0] = 140.0
    instances.exit_instance(first["instanceId"])

    stats = service.get_version_stats(game_path, "1.21.8")
    assert stats["launchCount"] == 2
    assert stats["lastRunDurationSeconds"] == 40
    assert stats["totalRunDurationSeconds"] == 55
    service.close()


def test_authlib_launch_passes_injector_to_game_backend(tmp_path, monkeypatch) -> None:
    class AuthlibAccounts:
        def current_account(self):
            return {"id": "authlib", "type": "authlib"}

        async def get_launch_credentials(self):
            return {
                "player_name": "Player",
                "uuid": "0123456789abcdef0123456789abcdef",
                "user_type": "yggdrasil",
                "access_token": "access-token",
                "auth_server": "https://skin.example.com/api/yggdrasil",
            }

    class FakeInjector:
        def __init__(self, path):
            self.path = path

        def needs_download(self):
            return False

        def ensure(self):
            return self.path

        def close(self):
            pass

    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    injector_path = tmp_path / "authlib-injector.jar"
    injector_path.write_bytes(b"jar")
    captured_configs = []
    events = []
    event_bus = EventBus()
    event_bus.subscribe("game:launch_progress", events.append)
    service = GameService(
        AuthlibAccounts(),
        search_factory=EmptySearchMinecraft,
        instances_manager=FakeInstances(),
        downloader_factory=FakeDownloader,
        command_builder=lambda config: captured_configs.append(config) or '"java.exe" game.Main',
        authlib_injector=FakeInjector(injector_path),
        event_bus=event_bus,
    )
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
        )
    )

    config = captured_configs[0]
    assert config.user_type == "yggdrasil"
    assert config.authlib_path == injector_path
    assert config.yggdrasil_api == "https://skin.example.com/api/yggdrasil"
    assert [event["phase"] for event in events[:4]] == [
        "preparing",
        "authlib_token",
        "account_ready",
        "authlib",
    ]
    assert events[1]["message"] == "正在验证外置登录令牌，过期时将自动刷新"


def test_microsoft_launch_reports_token_refresh_progress(tmp_path, monkeypatch) -> None:
    class MicrosoftAccounts:
        def current_account(self):
            return {"id": "microsoft", "type": "microsoft"}

        async def get_launch_credentials(self):
            return {
                "player_name": "Player",
                "uuid": "0123456789abcdef0123456789abcdef",
                "user_type": "msa",
                "access_token": "access-token",
            }

    events = []
    event_bus = EventBus()
    event_bus.subscribe("game:launch_progress", events.append)
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    service = GameService(
        MicrosoftAccounts(),
        search_factory=EmptySearchMinecraft,
        instances_manager=FakeInstances(),
        downloader_factory=FakeDownloader,
        command_builder=lambda _config: '"java.exe" game.Main',
        event_bus=event_bus,
    )
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )

    asyncio.run(
        service.launch_instance(
            {"version_id": "1.21.8"},
            game_path=game_path,
            java_path=java_path,
        )
    )

    phases = [event["phase"] for event in events]
    assert phases[:3] == ["preparing", "microsoft_token", "account_ready"]
    assert events[1]["message"] == "正在检查正版登录令牌，过期时将自动刷新"
    assert events[2]["message"] == "正版登录令牌已就绪"


def test_cancel_launch_stops_active_file_download(tmp_path, monkeypatch) -> None:
    class BlockingDownloader(FakeDownloader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stopped = False

        async def run(self):
            while not self.stopped:
                await asyncio.sleep(0.005)
            raise RuntimeError("download stopped")

        def stop(self):
            self.stopped = True

    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    service = _build_service(downloader_factory=BlockingDownloader)
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(
            files_checker=SimpleNamespace(
                check_files=lambda *_args: [("https://example.com/client.jar", str(tmp_path / "client.jar"))]
            ),
        ),
    )

    async def launch_then_cancel():
        launch_task = asyncio.create_task(
            service.launch_instance(
                {"version_id": "1.21.8"},
                game_path=game_path,
                java_path=java_path,
            )
        )
        for _ in range(100):
            if "__launch__" in service._active_downloads:
                break
            await asyncio.sleep(0.005)
        assert service.cancel_launch() is True
        with pytest.raises(GameServiceError) as error:
            await launch_task
        return error.value

    error = asyncio.run(launch_then_cancel())

    assert error.error_code == "LAUNCH_CANCELLED"
    assert str(error) == "启动已取消"


def test_launch_retries_only_failed_game_file_from_alternate_source(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    (version_path / "1.21.8.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")
    first_path = str(tmp_path / "first.jar")
    failed_path = str(tmp_path / "second.jar")
    primary_files = [
        ("https://bmclapi.example/first.jar", first_path),
        ("https://bmclapi.example/second.jar", failed_path),
    ]
    alternate_files = [
        ("https://official.example/first.jar", first_path),
        ("https://official.example/second.jar", failed_path),
    ]
    download_lists = []

    class PartialDownloader(FakeDownloader):
        def __init__(self, download_list, **kwargs):
            super().__init__(download_list, **kwargs)
            download_lists.append(download_list)

        async def run(self):
            if len(download_lists) == 1:
                self.completed_entries.add(primary_files[0])
                self.failed_entries.add(primary_files[1])
            else:
                self.completed_entries.add(alternate_files[1])
            if self.progress_callback:
                self.progress_callback(len(self.completed_entries), len(self.download_list))

    service = _build_service(
        downloader_factory=PartialDownloader,
        instances_manager=FakeInstances(),
        command_builder=lambda _config: '"java.exe" game.Main',
    )
    monkeypatch.setattr(
        service,
        "_context",
        lambda _path, source: SimpleNamespace(
            files_checker=SimpleNamespace(
                check_files=lambda *_: primary_files if source == "bmclapi" else alternate_files
            )
        ),
    )

    result = asyncio.run(
        service.launch_instance({"version_id": "1.21.8"}, game_path=game_path, java_path=java_path, source="bmclapi")
    )

    assert result["instanceId"]
    assert download_lists == [primary_files, [alternate_files[1]]]
    service.close()


def test_launch_instance_reports_core_error_details(tmp_path, monkeypatch) -> None:
    events = []
    event_bus = EventBus()
    event_bus.subscribe("game:launch_progress", events.append)
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "broken"
    version_path.mkdir(parents=True)
    (version_path / "broken.json").write_text("{}", encoding="utf-8")
    java_path = tmp_path / "java.exe"
    java_path.write_bytes(b"")

    def fail_to_build(_config):
        raise KeyError("mainClass")

    service = _build_service(command_builder=fail_to_build, event_bus=event_bus)
    monkeypatch.setattr(
        service,
        "_context",
        lambda *_args: SimpleNamespace(files_checker=SimpleNamespace(check_files=lambda *_args: [])),
    )

    with pytest.raises(GameServiceError) as error:
        asyncio.run(
            service.launch_instance(
                {"version_id": "broken"},
                game_path=game_path,
                java_path=java_path,
            )
        )

    assert error.value.error_code == "GAME_LAUNCH_FAILED"
    assert "mainClass" in str(error.value)
    assert events[-1]["phase"] == "error"
    assert events[-1]["errorCode"] == "GAME_LAUNCH_FAILED"
    assert "mainClass" in events[-1]["message"]


def test_uninstall_version_only_removes_selected_version(tmp_path) -> None:
    service = _build_service()
    selected = tmp_path / "versions" / "1.20.1"
    other = tmp_path / "versions" / "1.21.8"
    selected.mkdir(parents=True)
    other.mkdir(parents=True)

    service.uninstall_version("1.20.1", tmp_path)

    assert not selected.exists()
    assert other.is_dir()
    with pytest.raises(GameServiceError) as error:
        service.uninstall_version("../outside", tmp_path)
    assert error.value.error_code == "INVALID_VERSION_NAME"


def test_ecl_config_read_write_and_patch(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()

    # 文件不存在时返回空字典
    assert service.read_ecl_config(game_path) == {}

    # 写入完整配置
    service.write_ecl_config(game_path, {"activeVersion": "1.21.1", "customField": 42})
    ecl_file = game_path / "ecl.json"
    assert ecl_file.is_file()
    assert service.read_ecl_config(game_path) == {"activeVersion": "1.21.1", "customField": 42}

    # 增量更新
    updated = service.patch_ecl_config(game_path, {"activeVersion": "1.20.1", "newKey": "hello"})
    assert updated == {"activeVersion": "1.20.1", "customField": 42, "newKey": "hello"}
    assert service.read_ecl_config(game_path) == updated


def test_ecl_config_skips_unchanged_writes(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()
    writes = []
    original_write = __import__("ECL.services.game.scan", fromlist=["atomic_write_text"]).atomic_write_text

    def tracked_write(path, data):
        writes.append((path, data))
        original_write(path, data)

    monkeypatch.setattr("ECL.services.game.scan.atomic_write_text", tracked_write)
    config = {"activeVersion": "1.21.1", "customField": 42}

    service.write_ecl_config(game_path, config)
    service.write_ecl_config(game_path, dict(config))
    patched = service.patch_ecl_config(game_path, {"activeVersion": "1.21.1"})

    assert patched == config
    assert len(writes) == 1


def test_ecl_config_serializes_concurrent_identical_patches(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()
    service.write_ecl_config(game_path, {"activeVersion": "old", "customField": 42})
    writes = []
    original_write = __import__("ECL.services.game.scan", fromlist=["atomic_write_text"]).atomic_write_text

    def tracked_write(path, data):
        writes.append((path, data))
        time.sleep(0.02)
        original_write(path, data)

    monkeypatch.setattr("ECL.services.game.scan.atomic_write_text", tracked_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: service.patch_ecl_config(game_path, {"activeVersion": "new"}),
                range(2),
            )
        )

    assert results == [
        {"activeVersion": "new", "customField": 42},
        {"activeVersion": "new", "customField": 42},
    ]
    assert len(writes) == 1
    assert service.read_ecl_config(game_path) == results[0]


def test_ecl_active_version_get_and_set(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()

    # 没有 activeVersion 时返回 None
    assert service.get_active_version(game_path) is None

    # 设置后能读回
    service.set_active_version(game_path, "1.21.1")
    assert service.get_active_version(game_path) == "1.21.1"

    # 兼容 snake_case 别名
    service.write_ecl_config(game_path, {"active_version": "1.19.4"})
    assert service.get_active_version(game_path) == "1.19.4"


def test_ecl_config_rejects_invalid_data(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()

    with pytest.raises(GameServiceError) as error:
        service.write_ecl_config(game_path, ["not", "a", "dict"])  # type: ignore[arg-type]
    assert error.value.error_code == "INVALID_ECL_CONFIG"

    with pytest.raises(GameServiceError) as error:
        service.patch_ecl_config(game_path, "not-a-dict")  # type: ignore[arg-type]
    assert error.value.error_code == "INVALID_ECL_CONFIG"


def test_ecl_config_handles_corrupted_file(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    game_path.mkdir(parents=True)
    (game_path / "ecl.json").write_text("{invalid json", encoding="utf-8")
    service = _build_service()

    # 损坏的文件返回空字典，不抛异常
    assert service.read_ecl_config(game_path) == {}


def test_version_settings_stored_in_versions_own_directory(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()

    # 文件不存在时返回空字典
    assert service.read_version_settings(game_path, "1.21.1") == {}

    # 写入到版本自己的目录，而不是实例根目录
    settings = {"customMemory": True, "memory": 6144, "jvmArgs": "-XX:+UseG1GC"}
    saved = service.write_version_settings(game_path, "1.21.1", settings)
    settings_file = game_path / "versions" / "1.21.1" / ".ecl" / "settings.json"
    assert all(saved[key] == value for key, value in settings.items())
    assert saved["schemaVersion"] == 2
    assert saved["memoryMode"] == "manual"
    assert settings_file.is_file()
    assert json.loads(settings_file.read_text(encoding="utf-8")) == saved
    assert not (game_path / "ecl.json").is_file()
    assert service.read_version_settings(game_path, "1.21.1") == saved


def test_version_settings_skips_unchanged_writes_and_rejects_invalid(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service()
    writes = []
    original_write = __import__("ECL.services.game.scan", fromlist=["atomic_write_text"]).atomic_write_text

    def tracked_write(path, data):
        writes.append((path, data))
        original_write(path, data)

    monkeypatch.setattr("ECL.services.game.scan.atomic_write_text", tracked_write)
    settings = {"customMemory": True, "memory": 6144}

    service.write_version_settings(game_path, "1.21.1", settings)
    service.write_version_settings(game_path, "1.21.1", dict(settings))
    assert len(writes) == 1

    with pytest.raises(GameServiceError) as error:
        service.write_version_settings(game_path, "1.21.1", ["not", "a", "dict"])  # type: ignore[arg-type]
    assert error.value.error_code == "INVALID_ECL_CONFIG"

    with pytest.raises(GameServiceError) as error:
        service.write_version_settings(game_path, "../escape", {})
    assert error.value.error_code == "INVALID_VERSION_NAME"


def test_local_mod_lifecycle_stays_inside_mods_directory(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    source = tmp_path / "example.jar"
    source.write_bytes(b"safe-mod")
    service = _build_service()

    filename = service.add_mod(game_path, source)
    mods = service.list_mods(game_path)
    disabled = service.toggle_mod(game_path, filename)
    service.remove_mod(game_path, f"{filename}.disabled")

    assert filename == "example.jar"
    assert mods[0]["enabled"] is True
    assert disabled is False
    assert service.list_mods(game_path) == []
    with pytest.raises(GameServiceError, match="路径超出允许范围"):
        service.remove_mod(game_path, "../outside.jar")


def test_instance_mods_follow_saved_isolation_setting(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    source = tmp_path / "example.jar"
    source.write_bytes(b"safe-mod")
    service = _build_service()

    service.write_version_settings(game_path, "isolated", {"isolated": True})
    service.add_instance_mod(game_path, "isolated", source)

    assert (game_path / "versions" / "isolated" / "mods" / "example.jar").is_file()
    assert not (game_path / "versions" / "mods" / "example.jar").exists()
    assert service.list_instance_mods(game_path, "isolated")[0]["filename"] == "example.jar"

    service.write_version_settings(game_path, "shared", {"isolationMode": "disabled"})
    service.add_instance_mod(game_path, "shared", source)
    assert (game_path / "versions" / "mods" / "example.jar").is_file()
    assert service.resolve_version_isolation(game_path, "isolated") is True
    assert service.resolve_version_isolation(game_path, "isolated", False) is False


@pytest.mark.parametrize(
    ("policy", "metadata", "expected"),
    [
        ("disabled", {"primaryLoader": "Fabric", "versionType": "snapshot"}, False),
        ("modded_only", {"primaryLoader": "Fabric", "versionType": "release"}, True),
        ("modded_only", {"primaryLoader": "Vanilla", "versionType": "snapshot"}, False),
        ("non_release_only", {"primaryLoader": "Vanilla", "versionType": "snapshot"}, True),
        ("modded_or_non_release", {"primaryLoader": "Fabric", "versionType": "release"}, True),
        ("all", {"primaryLoader": "Vanilla", "versionType": "release"}, True),
    ],
)
def test_instance_isolation_uses_global_policy(tmp_path, monkeypatch, policy, metadata, expected) -> None:
    service = _build_service(isolation_policy_provider=lambda: policy)
    monkeypatch.setattr(service, "_version_isolation_metadata", lambda *_args: metadata)

    assert service.resolve_version_isolation(tmp_path / ".minecraft", "example") is expected


def test_instance_isolation_override_and_legacy_setting_take_priority(tmp_path, monkeypatch) -> None:
    game_path = tmp_path / ".minecraft"
    service = _build_service(isolation_policy_provider=lambda: "all")
    monkeypatch.setattr(service, "_version_isolation_metadata", lambda *_args: {"primaryLoader": "Vanilla"})

    service.write_version_settings(game_path, "enabled", {"isolationMode": "enabled"})
    service.write_version_settings(game_path, "disabled", {"isolationMode": "disabled"})
    service.write_version_settings(game_path, "legacy", {"isolated": False})

    assert service.resolve_version_isolation(game_path, "enabled") is True
    assert service.resolve_version_isolation(game_path, "disabled") is False
    assert service.resolve_version_isolation(game_path, "legacy") is False
    assert service.resolve_version_isolation(game_path, "enabled", False) is False


def test_download_resource_to_path_saves_to_target(tmp_path, monkeypatch) -> None:
    service = _build_service()
    destination = tmp_path / "sodium.jar"
    downloaded = []

    def fake_fetch(_version_id: str, _mod_source: str = "") -> dict:
        return {"filename": "sodium.jar", "url": "https://example.com/sodium.jar", "hashes": {}}

    def fake_download(url: str, temp, filename: str, _task_id) -> None:
        downloaded.append((url, filename))
        temp.write_bytes(b"JAR-CONTENT")

    monkeypatch.setattr(service, "_fetch_online_version", fake_fetch)
    monkeypatch.setattr(service, "_download_online_file", fake_download)

    result = service.download_resource_to_path("modrinth", "project-1", "version-1", str(destination))

    assert result == {"filename": "sodium.jar", "source": "modrinth", "skipped": False}
    assert downloaded == [("https://example.com/sodium.jar", "sodium.jar")]
    assert destination.read_bytes() == b"JAR-CONTENT"
    assert not list(tmp_path.glob("*.ecl-download"))


def test_download_resource_to_path_rejects_unsupported_source_and_missing_dir(tmp_path) -> None:
    service = _build_service()
    with pytest.raises(GameServiceError) as error:
        # curseforge 已是受支持来源（需 API Key），这里用真正未知的来源验证拒绝逻辑
        service.download_resource_to_path("unknown-source", "p", "v", str(tmp_path / "mod.jar"))
    assert error.value.error_code == "INVALID_RESOURCE_SOURCE"

    missing_dir = tmp_path / "not-exists" / "mod.jar"
    with pytest.raises(GameServiceError) as error:
        service.download_resource_to_path("modrinth", "p", "v", str(missing_dir))
    assert error.value.error_code == "INVALID_SAVE_PATH"


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_silent_user_commands_log_start_and_exit(tmp_path, monkeypatch, phase) -> None:
    service = _build_service()
    messages = []
    monkeypatch.setattr(service.logger, "info", lambda message, *args: messages.append(message % args))
    monkeypatch.setattr(
        "ECL.services.game.launch.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="", returncode=0),
    )
    try:
        if phase == "pre":
            service._run_pre_launch_command("secret-command", tmp_path)
        else:
            service._run_post_exit_command("secret-command", tmp_path, 0)
        assert len(messages) == 2
        assert "开始执行" in messages[0]
        assert "退出码：0" in messages[1]
        assert "secret-command" not in str(messages)
    finally:
        service.close()


def test_profile_write_is_not_republished_by_watcher_but_external_write_is(tmp_path: Path) -> None:
    game_path = tmp_path / "minecraft"
    instance_path = game_path / "versions" / "demo"
    instance_path.mkdir(parents=True)
    (instance_path / "demo.json").write_text('{"id":"demo"}', encoding="utf-8")
    bus = EventBus()
    changes: list[object] = []
    bus.subscribe("game:versions_changed", changes.append)
    service = _build_service(
        event_bus=bus, enable_version_watcher=False, version_watch_debounce=0, data_path=tmp_path / "data"
    )
    try:
        service.scan_versions([str(game_path)])
        service.patch_instance_profile(str(game_path), "demo", {"alias": "新名称"})
        assert len(changes) == 1
        assert service._poll_version_changes(now=100) == []
        profile_path = instance_path / ".ecl" / "instance.json"
        profile_path.write_text('{"alias":"来自外部程序的另一名称"}', encoding="utf-8")
        assert service._poll_version_changes(now=101) == [str(game_path.resolve())]
        assert len(changes) == 2
    finally:
        service.close()
