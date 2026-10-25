from __future__ import annotations

import json
import stat
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ECL.foundation import EventBus
from ECL.services.game import GameServiceError, modpack_export
from ECL.services.game.modpack_export import ExportContentPolicy, scan_export_files
from ECL.services.game.workspace import resolve_instance_target
from ECL.services.operations import OperationContext


def _source(tmp_path: Path, isolated: bool):
    game_root = tmp_path / "minecraft"
    instance = game_root / "versions" / "Demo"
    instance.mkdir(parents=True)
    (instance / "Demo.json").write_text(json.dumps({"id": "1.21.1"}), encoding="utf-8")
    target = resolve_instance_target(game_root, "Demo", isolated)
    return target, OperationContext("a" * 32, EventBus(), Event())


def _write(root: Path, relative: str, content: bytes = b"test") -> Path:
    file = root / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    return file


@pytest.mark.parametrize("isolated", [False, True])
def test_export_uses_actual_data_directory_and_excludes_runtime(tmp_path, isolated):
    target, context = _source(tmp_path, isolated)
    for relative in [
        "mods/keep.jar",
        "mods/off.jar.disabled",
        "config/client.json",
        "saves/World/level.dat",
        "screenshots/image.png",
        "servers.dat",
        "config/accountsx/token.json",
        "logs/latest.log",
        "assets/index.json",
        "ecl.json",
    ]:
        _write(target.game_data_path, relative)
    if not isolated:
        _write(target.game_data_path, "Other/Other.json")
        _write(target.game_data_path, "Other/mods/private.jar")
    snapshot = scan_export_files(target, {"minecraft": "1.21.1"}, context)
    files = {file.relative: file for file in snapshot.files}
    assert set(files) == {
        "mods/keep.jar",
        "mods/off.jar.disabled",
        "config/client.json",
        "saves/World/level.dat",
        "screenshots/image.png",
        "servers.dat",
    }
    defaults = {file.relative for file in snapshot.files if file.category in ExportContentPolicy.recommended_categories}
    assert defaults == {"mods/keep.jar", "config/client.json"}
    assert files["mods/off.jar.disabled"].category == "disabledMods"
    assert (target.game_data_path / "config/accountsx/token.json").read_bytes() == b"test"


def test_selection_rejects_changed_selected_file_without_adding_new_files(tmp_path):
    target, context = _source(tmp_path, True)
    file = _write(target.game_data_path, "mods/keep.jar")
    before = scan_export_files(target, {"minecraft": "1.21.1"}, context)
    selected = [before.files[0].file_id]
    _write(target.game_data_path, "mods/new.jar")
    after = scan_export_files(target, {"minecraft": "1.21.1"}, context)
    assert len(after.selection(before.source_revision, selected)) == 1
    file.write_bytes(b"changed content")
    current = scan_export_files(target, {"minecraft": "1.21.1"}, context)
    with pytest.raises(GameServiceError, match="刷新"):
        current.selection(before.source_revision, selected)


def test_empty_selection_is_dependencies_only_and_duplicate_ids_are_rejected(tmp_path):
    target, context = _source(tmp_path, True)
    _write(target.game_data_path, "mods/a.jar")
    snapshot = scan_export_files(target, {"minecraft": "1.21.1"}, context)
    assert snapshot.selection(snapshot.source_revision, []) == ()
    with pytest.raises(GameServiceError):
        snapshot.selection(snapshot.source_revision, [snapshot.files[0].file_id] * 2)
    with pytest.raises(GameServiceError):
        snapshot.selection(snapshot.source_revision, ["f" * 64])


def test_source_and_file_identity_checked_again_before_commit(tmp_path):
    target, context = _source(tmp_path, True)
    file = _write(target.game_data_path, "mods/a.jar")
    snapshot = scan_export_files(target, {"minecraft": "1.21.1"}, context)
    snapshot.files[0].validate(snapshot.root)
    file.write_bytes(b"changed")
    with pytest.raises(GameServiceError):
        snapshot.files[0].validate(snapshot.root)
    (target.instance_path / "Demo.json").write_text('{"id":"1.21.2"}', encoding="utf-8")
    with pytest.raises(GameServiceError):
        snapshot.validate_source()


def test_file_count_limit_fails_instead_of_returning_a_truncated_list(tmp_path, monkeypatch):
    target, context = _source(tmp_path, True)
    _write(target.game_data_path, "mods/a.jar")
    _write(target.game_data_path, "mods/b.jar")
    monkeypatch.setattr(ExportContentPolicy, "max_files", 1)
    with pytest.raises(GameServiceError) as error:
        scan_export_files(target, {"minecraft": "1.21.1"}, context)
    assert error.value.error_code == "EXPORT_SCAN_LIMIT"


@pytest.mark.parametrize(
    ("platform", "mode", "attributes", "expected"),
    [
        ("win32", stat.S_IFDIR, 0x400, True),
        ("win32", stat.S_IFDIR, 0, False),
        ("linux", stat.S_IFLNK, 0, True),
        ("linux", stat.S_IFREG, 0, False),
    ],
)
def test_link_policy_covers_windows_reparse_and_posix_symlink(monkeypatch, platform, mode, attributes, expected):
    monkeypatch.setattr(modpack_export.sys, "platform", platform)
    metadata = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
    assert ExportContentPolicy.is_link(metadata) is expected


def test_scan_never_follows_an_external_directory_link(tmp_path):
    target, context = _source(tmp_path, True)
    external = tmp_path / "external"
    _write(external, "secret.txt")
    try:
        (target.game_data_path / "linked").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建目录链接")
    assert scan_export_files(target, {"minecraft": "1.21.1"}, context).files == ()
