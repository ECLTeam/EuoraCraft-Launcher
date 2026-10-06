# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 game_workspace 模块的自动化测试。
#
# 公开接口：
#   - test_resolve_instance_target_matches_isolation_semantics(tmp_path) -> None
#   - test_resolve_relative_id_rejects_escape(tmp_path, relative_id) -> None
#   - test_safe_extract_zip_rejects_path_traversal(tmp_path) -> None
#   - test_safe_extract_zip_rejects_excessive_file_count(tmp_path) -> None
#   - test_world_patch_preserves_unknown_nbt_and_creates_backup(tmp_path) -> None
#   - test_instance_profile_cover_field_is_preserved(tmp_path) -> None
#   - test_world_patch_expanded_fields_updates_nbt(tmp_path) -> None
#   - test_world_patch_rejects_invalid_values(tmp_path, field, value, code) -> None
#   - test_options_read_structurizes_known_lines_and_counts_unknown(tmp_path) -> None
#   - test_options_patch_replaces_and_appends_keeping_unknown(tmp_path) -> None
#   - test_options_patch_rejects_invalid_keys_and_values(tmp_path, patch, code) -> None
# ============================================================

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

from ECL.game import Byte, Compound, File, Int, Long, String, load
from ECL.services.game.base import GameServiceError
from ECL.services.game.workspace import (
    WorkspaceCoordinator,
    resolve_instance_target,
    resolve_relative_id,
    safe_extract_zip,
)
from ECL.services.game.worlds import WorldCoordinator


def test_resolve_instance_target_matches_isolation_semantics(tmp_path: Path) -> None:
    shared = resolve_instance_target(tmp_path, "1.21.8", False)
    isolated = resolve_instance_target(tmp_path, "1.21.8", True)

    assert shared.data_path == tmp_path / "versions"
    assert isolated.data_path == tmp_path / "versions" / "1.21.8"


def test_instance_identity_distinguishes_roots_and_directory_from_game_version(tmp_path: Path) -> None:
    first = resolve_instance_target(tmp_path / "first", "my-pack", True)
    second = resolve_instance_target(tmp_path / "second", "my-pack", True)
    assert first.instance_key != second.instance_key
    assert first.instance_directory_name == "my-pack"
    assert first.minecraft_root_path == first.game_path
    assert first.game_data_path == first.instance_path == first.data_path


def test_scan_identity_does_not_fold_case_sensitive_paths(tmp_path: Path) -> None:
    from unittest.mock import patch

    from ECL.services.game.scan import ScanCoordinator

    # 即使在 Windows 运行，也验证 POSIX 路径策略；不依赖本机卷是否开启目录大小写敏感。
    with patch.object(Path, "resolve", lambda path, **_: path):
        roots = ScanCoordinator._normalize_scan_paths([tmp_path / "Pack", tmp_path / "pack"])
        assert len(roots) == 2
        assert ScanCoordinator._version_path_key(roots[0]) != ScanCoordinator._version_path_key(roots[1])


def test_running_guards_distinguish_resolved_case_sensitive_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "resolve", lambda path, **_: path)
    root = tmp_path / "Pack"
    other_root = tmp_path / "pack"
    service = WorkspaceCoordinator.__new__(WorkspaceCoordinator)
    service.events = SimpleNamespace(emit=lambda *_args: None)
    service.list_instances = lambda: [{"gamePath": str(other_root), "versionId": "same"}]
    deleted_paths = []
    monkeypatch.setattr("ECL.services.game.workspace.delete_path", deleted_paths.append)
    service.delete_instance(root, "same")
    assert deleted_paths == [root / "versions" / "same"]
    worlds = WorldCoordinator.__new__(WorldCoordinator)
    worlds.list_instances = service.list_instances
    worlds._assert_world_writable(resolve_instance_target(root, "same"), root / "world")


def test_core_context_does_not_reuse_a_case_folded_root(tmp_path: Path, monkeypatch) -> None:
    from ECL.services.game.base import _GameState

    root = tmp_path / "Pack"
    service = _GameState.__new__(_GameState)
    service._normalize_game_path = lambda _value: root
    service._lock = RLock()
    service._contexts = {(str(root).casefold(), "official"): SimpleNamespace(owner="other-root")}
    service.logger = SimpleNamespace(debug=lambda *_args: None)
    # 创建新核心时故意中断，以区分创建动作和错误复用，不访问网络。
    service._api_client_factory = lambda _config: (_ for _ in ()).throw(RuntimeError("create current root"))
    with pytest.raises(RuntimeError, match="create current root"):
        service._context(root)


def test_java_lookup_distinguishes_resolved_case_sensitive_paths(tmp_path: Path, monkeypatch) -> None:
    from ECL.services.game.launch import LaunchCoordinator

    monkeypatch.setattr(Path, "resolve", lambda path, **_: path)
    java_path = tmp_path / "JavaA" / "java"
    other_path = tmp_path / "javaa" / "java"
    runtime = SimpleNamespace(path=java_path)
    service = LaunchCoordinator.__new__(LaunchCoordinator)
    service._java_runtimes = [runtime]
    assert service._known_java_runtime(str(other_path)) is None
    assert service._known_java_runtime(str(java_path)) is runtime


def test_scan_reports_verified_root_aliases_without_scanning_twice(tmp_path: Path, monkeypatch) -> None:
    from ECL.services.game.scan import ScanCoordinator

    first_root = tmp_path / "Pack"
    second_root = tmp_path / "pack"
    monkeypatch.setattr(Path, "resolve", lambda path, **_: first_root if path in (first_root, second_root) else path)
    service = ScanCoordinator.__new__(ScanCoordinator)
    service._lock = RLock()
    service.logger = SimpleNamespace(debug=lambda *_args: None)
    service._version_scan_cache = {}
    service._version_watch_snapshots = {}
    service._version_watch_pending = {}
    service._watch_version_path = lambda path, _options: str(path.resolve(strict=False))
    service._ensure_ecl_config = lambda _path: None
    service._version_directory_snapshot = lambda *_args: ()
    scans = []

    def scan(path, _options):
        scans.append(path)
        return [
            {
                "versionId": "same",
                "path": str(path),
                "rootKey": str(first_root),
                "sourceName": "root",
                "displayName": "same",
            }
        ]

    service._scan_game_path = scan
    result = service.scan_versions([first_root, second_root])
    assert scans == [first_root]
    assert result[0]["rootAliases"] == [str(first_root), str(second_root)]


@pytest.mark.parametrize("relative_id", ["../secret", "/absolute", "a/../../b", ""])
def test_resolve_relative_id_rejects_escape(tmp_path: Path, relative_id: str) -> None:
    with pytest.raises(GameServiceError, match="资源 ID"):
        resolve_relative_id(tmp_path, relative_id, must_exist=False)


def test_safe_extract_zip_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escaped.txt", "bad")

    with pytest.raises(GameServiceError) as raised:
        safe_extract_zip(archive, tmp_path / "output")

    assert raised.value.error_code == "ZIP_PATH_TRAVERSAL"
    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_zip_rejects_excessive_file_count(tmp_path: Path) -> None:
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("one.txt", "1")
        output.writestr("two.txt", "2")

    with pytest.raises(GameServiceError) as raised:
        safe_extract_zip(archive, tmp_path / "output", max_files=1)

    assert raised.value.error_code == "ZIP_BOMB_DETECTED"


def _write_level_dat(path: Path) -> None:
    path.mkdir(parents=True)
    document = File(
        {
            "Data": Compound(
                {
                    "LevelName": String("测试世界"),
                    "GameType": Int(0),
                    "Difficulty": Byte(2),
                    "DifficultyLocked": Byte(0),
                    "allowCommands": Byte(0),
                    "RandomSeed": Long(123456),
                    "LastPlayed": Long(1_700_000_000_000),
                    "Version": Compound({"Name": String("1.21.8")}),
                    "EclUnknownField": String("keep-me"),
                }
            )
        },
        gzipped=True,
    )
    document.save(path / "level.dat", gzipped=True)


class _WorldHarness(WorldCoordinator, WorkspaceCoordinator):
    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    def list_instances(self) -> list[dict[str, object]]:
        return []


@pytest.mark.parametrize("source_kind", ["directory", "zip"])
def test_world_import_accepts_folder_and_zip(tmp_path: Path, source_kind: str) -> None:
    from types import SimpleNamespace

    source = tmp_path / "export" / "world"
    _write_level_dat(source)
    if source_kind == "zip":
        archive_path = tmp_path / "world.ZIP"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.write(source / "level.dat", "world/level.dat")
        source = archive_path
    service = _WorldHarness(tmp_path / "app-data")
    context = SimpleNamespace(check_cancelled=lambda: None)
    service._application_operations = SimpleNamespace(submit=lambda _name, worker: worker(context))
    result = service.import_world(tmp_path, "demo", source, True)
    assert result == {"worldId": "world"}
    assert service.list_worlds(tmp_path, "demo", True)[0]["name"] == "测试世界"


def test_local_world_import_rejects_non_zip_but_download_helper_accepts_archive(tmp_path: Path) -> None:
    from types import SimpleNamespace

    source = tmp_path / "world"
    _write_level_dat(source)
    archive_path = tmp_path / "download.tmp"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(source / "level.dat", "world/level.dat")
    service = _WorldHarness(tmp_path / "app-data")
    calls = []
    service._application_operations = SimpleNamespace(submit=lambda *args: calls.append(args))
    with pytest.raises(GameServiceError) as raised:
        service.import_world(tmp_path, "demo", archive_path, True)
    assert raised.value.error_code == "UNSUPPORTED_WORLD_IMPORT"
    assert not calls
    assert service._import_world_source(tmp_path / "downloaded", archive_path) == {"worldId": "world"}


@pytest.mark.parametrize("world_count", [0, 2])
def test_world_import_rejects_invalid_or_multiple_worlds(tmp_path: Path, world_count: int) -> None:
    archive_path = tmp_path / "world.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index in range(world_count):
            source = tmp_path / f"world{index}"
            _write_level_dat(source)
            archive.write(source / "level.dat", f"world{index}/level.dat")
    service = _WorldHarness(tmp_path / "app-data")
    with pytest.raises(GameServiceError) as raised:
        service._import_world_source(tmp_path / "saves", archive_path)
    assert raised.value.error_code == "INVALID_WORLD_ARCHIVE"
    assert list((tmp_path / "saves").iterdir()) == []


def test_world_patch_preserves_unknown_nbt_and_creates_backup(tmp_path: Path) -> None:
    version = tmp_path / "versions" / "test"
    _write_level_dat(version / "saves" / "world")
    harness = _WorldHarness(tmp_path / "app-data")

    result = harness.patch_world(
        tmp_path,
        "test",
        "world",
        {"difficulty": 3, "allowCommands": True, "difficultyLocked": True},
        version_isolation=True,
    )

    loaded = load(version / "saves" / "world" / "level.dat")
    assert str(loaded["Data"]["EclUnknownField"]) == "keep-me"
    assert int(loaded["Data"]["Difficulty"]) == 3
    assert result["allowCommands"] is True
    assert len(list((tmp_path / "ECLBackups" / "test" / "world").glob("*.zip"))) == 1


def test_instance_profile_cover_field_is_preserved(tmp_path: Path) -> None:
    profile = tmp_path / "versions" / "test" / ".ecl" / "instance.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps({"schemaVersion": 1, "cover": {"type": "local", "value": "cover.png"}}), encoding="utf-8"
    )

    from ECL.services.game.instance_profiles import InstanceProfileStore
    from ECL.services.game.version_stats import VersionStatsStore

    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore())
    assert store.read_profile(tmp_path, "test")["cover"]["value"] == "cover.png"


def test_world_patch_expanded_fields_updates_nbt(tmp_path: Path) -> None:
    version = tmp_path / "versions" / "test"
    _write_level_dat(version / "saves" / "world")
    harness = _WorldHarness(tmp_path / "app-data")

    result = harness.patch_world(
        tmp_path,
        "test",
        "world",
        {
            "gameMode": 1,
            "raining": True,
            "thundering": True,
            "seed": 987654,
            "spawn": {"x": 10, "y": 64, "z": -20},
        },
        version_isolation=True,
    )

    loaded = load(version / "saves" / "world" / "level.dat")
    assert int(loaded["Data"]["GameType"]) == 1
    assert int(loaded["Data"]["raining"]) == 1
    assert int(loaded["Data"]["thundering"]) == 1
    assert int(loaded["Data"]["RandomSeed"]) == 987654
    assert int(loaded["Data"]["SpawnX"]) == 10
    assert int(loaded["Data"]["SpawnY"]) == 64
    assert int(loaded["Data"]["SpawnZ"]) == -20
    assert result["gameModeId"] == 1
    assert result["gameMode"] == "创造"
    assert result["weather"] == {"raining": True, "thundering": True}
    assert str(loaded["Data"]["EclUnknownField"]) == "keep-me"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("gameMode", 7, "INVALID_WORLD_GAMEMODE"),
        ("seed", "not-an-int", "INVALID_WORLD_SEED"),
        ("seed", True, "INVALID_WORLD_SEED"),
        ("spawn", {"x": 99999999, "y": 0, "z": 0}, "INVALID_WORLD_SPAWN"),
        ("spawn", {"x": "a", "y": 0, "z": 0}, "INVALID_WORLD_SPAWN"),
    ],
)
def test_world_patch_rejects_invalid_values(tmp_path: Path, field: str, value, code: str) -> None:
    version = tmp_path / "versions" / "test"
    _write_level_dat(version / "saves" / "world")
    harness = _WorldHarness(tmp_path / "app-data")

    with pytest.raises(GameServiceError) as raised:
        harness.patch_world(tmp_path, "test", "world", {field: value}, version_isolation=True)
    assert raised.value.error_code == code


def _options_harness(tmp_path: Path):
    from ECL.services.game.instance_options import InstanceOptionsCoordinator

    class _Harness(InstanceOptionsCoordinator, WorkspaceCoordinator):
        def __init__(self) -> None:
            self._data_path = tmp_path

        def list_instances(self) -> list[dict[str, object]]:
            return []

    instance = tmp_path / "versions" / "iso"
    instance.mkdir(parents=True)
    (instance / "options.txt").write_text(
        "language:zh_cn\nrenderDistance:12\nfullscreen:false\nsoundCategory_music:0.8\nunknownKey:a\n",
        encoding="utf-8",
    )
    return _Harness()


def test_options_read_structurizes_known_lines_and_counts_unknown(tmp_path: Path) -> None:
    harness = _options_harness(tmp_path)
    result = harness.read_options(tmp_path, "iso", version_isolation=True)
    entries = {item["key"]: item for item in result["options"]}
    assert entries["language"]["value"] == "zh_cn"
    assert entries["renderDistance"]["value"] == 12
    assert entries["renderDistance"]["max"] == 32
    assert entries["fullscreen"]["value"] is False
    assert entries["soundCategory_music"]["value"] == 0.8
    assert result["ignoredCount"] == 1


def test_options_patch_replaces_and_appends_keeping_unknown(tmp_path: Path) -> None:
    harness = _options_harness(tmp_path)
    result = harness.patch_options(
        tmp_path,
        "iso",
        {"renderDistance": 24, "fullscreen": True, "gamma": 1.2},
        version_isolation=True,
    )
    assert result["updated"] == 3
    lines = (tmp_path / "versions" / "iso" / "options.txt").read_text(encoding="utf-8").splitlines()
    assert "renderDistance:24" in lines
    assert "fullscreen:true" in lines
    assert "gamma:1.2" in lines
    assert "unknownKey:a" in lines
    assert "language:zh_cn" in lines
    assert lines.index("renderDistance:24") < lines.index("gamma:1.2")


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"gamemode": 1}, "UNSUPPORTED_GAME_OPTION"),
        ({"fullscreen": 1}, "INVALID_GAME_OPTION"),
        ({"renderDistance": 99}, "INVALID_GAME_OPTION"),
        ({"fov": "abc"}, "INVALID_GAME_OPTION"),
    ],
)
def test_options_patch_rejects_invalid_keys_and_values(tmp_path: Path, patch: dict, code: str) -> None:
    harness = _options_harness(tmp_path)
    with pytest.raises(GameServiceError) as raised:
        harness.patch_options(tmp_path, "iso", patch, version_isolation=True)
    assert raised.value.error_code == code
