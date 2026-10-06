from __future__ import annotations

import concurrent.futures
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from ECL.api.models import WorldPatchData
from ECL.game import Compound, File, Long, String, load
from ECL.services.game.base import GameServiceError
from ECL.services.game.workspace import WorkspaceCoordinator
from ECL.services.game.worlds import WorldCoordinator


class WorldHarness(WorldCoordinator, WorkspaceCoordinator):
    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path

    def list_instances(self) -> list[dict[str, object]]:
        return []


def write_world(root: Path, kind: str, seed: int | None) -> Path:
    world_path = root / "versions" / "demo" / "saves" / "world"
    world_path.mkdir(parents=True)
    fields = Compound({"LevelName": String("测试世界"), "Version": Compound({"Name": String("1.20.1")})})
    if kind == "legacy" and seed is not None:
        fields["RandomSeed"] = Long(seed)
    elif kind == "modern":
        fields["WorldGenSettings"] = Compound({"seed": Long(seed)} if seed is not None else {})
    elif kind == "split":
        settings_file = world_path / "data" / "minecraft" / "world_gen_settings.dat"
        settings_file.parent.mkdir(parents=True)
        File({"data": Compound({"seed": Long(seed), "custom": String("keep")})}, gzipped=True).save(settings_file)
    File({"Data": fields}, gzipped=True).save(world_path / "level.dat")
    return world_path


@pytest.mark.parametrize("kind", ["legacy", "modern", "split"])
@pytest.mark.parametrize("seed", [0, -4172144997902289642, -(2**63), 2**63 - 1])
def test_read_world_seed_formats_are_exact(tmp_path: Path, kind: str, seed: int) -> None:
    world_path = write_world(tmp_path, kind, seed)
    assert WorldCoordinator._read_world(world_path)["seed"] == str(seed)


@pytest.mark.parametrize("kind", ["legacy", "modern"])
def test_missing_seed_is_unknown_not_none(tmp_path: Path, kind: str) -> None:
    world_path = write_world(tmp_path, kind, None)
    detail = WorldCoordinator._read_world(world_path)
    assert detail["seed"] == ""
    assert detail["seedEditable"] is False
    assert detail["seedError"]


def test_modern_seed_wins_over_legacy_and_corruption_is_not_hidden(tmp_path: Path) -> None:
    world_path = write_world(tmp_path, "modern", 0)
    document = load(world_path / "level.dat")
    document["Data"]["RandomSeed"] = Long(99)
    document.save(world_path / "level.dat", gzipped=True)
    assert WorldCoordinator._read_world(world_path)["seed"] == "0"
    document["Data"]["WorldGenSettings"]["seed"] = String("99")
    document.save(world_path / "level.dat", gzipped=True)
    assert WorldCoordinator._read_world(world_path)["seed"] == ""


@pytest.mark.parametrize("kind", ["legacy", "modern", "split"])
def test_patch_seed_targets_actual_storage_and_keeps_unknown_fields(tmp_path: Path, kind: str) -> None:
    world_path = write_world(tmp_path, kind, 123)
    service = WorldHarness(tmp_path / "app-data")
    changed_seed = str(-(2**63))
    result = service.patch_world(tmp_path, "demo", "world", {"seed": changed_seed, "difficulty": 3}, True)
    assert result["seed"] == changed_seed
    document = load(world_path / "level.dat")
    assert str(document["Data"]["LevelName"]) == "测试世界"
    if kind != "legacy":
        assert "RandomSeed" not in document["Data"]
    if kind == "split":
        settings = load(world_path / "data" / "minecraft" / "world_gen_settings.dat")
        assert str(settings["data"]["custom"]) == "keep"
    backups = list((tmp_path / "ECLBackups" / "demo" / "world").glob("*.zip"))
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as archive:
        assert "level.dat" in archive.namelist()
        if kind == "split":
            assert "data/minecraft/world_gen_settings.dat" in archive.namelist()


@pytest.mark.parametrize("value", [True, 1.0, "1.5", "1e3", "not-an-int", str(2**63), str(-(2**63) - 1), 2**53])
def test_seed_ipc_rejects_invalid_or_unsafe_numbers(value: object) -> None:
    with pytest.raises(ValidationError):
        WorldPatchData(seed=value)


@pytest.mark.parametrize("seed", [str(-(2**63)), str(2**63 - 1), "0", 123])
def test_seed_ipc_accepts_exact_text_and_legacy_safe_numbers(seed: str | int) -> None:
    assert WorldPatchData(seed=seed).seed == str(seed)


@pytest.mark.parametrize("value", [True, 1.0, "1.5", str(2**63), 2**53])
def test_invalid_seed_patch_does_not_change_files_or_create_backup(tmp_path: Path, value: object) -> None:
    world_path = write_world(tmp_path, "modern", 123)
    original = (world_path / "level.dat").read_bytes()
    with pytest.raises(GameServiceError) as raised:
        WorldHarness(tmp_path / "app-data").patch_world(tmp_path, "demo", "world", {"seed": value}, True)
    assert raised.value.error_code == "INVALID_WORLD_SEED"
    assert (world_path / "level.dat").read_bytes() == original
    assert not (tmp_path / "ECLBackups").exists()


def test_split_corruption_does_not_fall_back_to_legacy(tmp_path: Path) -> None:
    world_path = write_world(tmp_path, "legacy", 123)
    settings_file = world_path / "data" / "minecraft" / "world_gen_settings.dat"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_bytes(b"not NBT")
    assert WorldCoordinator._read_world(world_path)["seed"] == ""


def test_missing_seed_is_not_changed_by_unrelated_patch(tmp_path: Path) -> None:
    world_path = write_world(tmp_path, "modern", None)
    WorldHarness(tmp_path / "app-data").patch_world(tmp_path, "demo", "world", {"difficulty": 3}, True)
    assert "seed" not in load(world_path / "level.dat")["Data"]["WorldGenSettings"]


def test_split_write_failure_rolls_back_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world_path = write_world(tmp_path, "split", 123)
    files = [world_path / "level.dat", world_path / "data" / "minecraft" / "world_gen_settings.dat"]
    originals = [file.read_bytes() for file in files]
    original_replace = Path.replace

    def fail_second_replace(source: Path, destination: Path) -> Path:
        if destination == files[1] and source.name.endswith(".ecl-tmp"):
            raise OSError("injected second-file failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_second_replace)
    with pytest.raises(GameServiceError):
        WorldHarness(tmp_path / "app-data").patch_world(
            tmp_path, "demo", "world", {"seed": "456", "difficulty": 3}, True
        )
    assert [file.read_bytes() for file in files] == originals
    assert not list(world_path.rglob("*.ecl-tmp"))


def test_concurrent_seed_changes_are_serialized(tmp_path: Path) -> None:
    world_path = write_world(tmp_path, "split", 123)
    service = WorldHarness(tmp_path / "app-data")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(service.patch_world, tmp_path, "demo", "world", {"seed": str(seed)}, True)
            for seed in range(4)
        ]
        assert sorted(future.result()["seed"] for future in futures) == ["0", "1", "2", "3"]
    assert WorldCoordinator._read_world(world_path)["seed"] in {"0", "1", "2", "3"}


def test_chunkbase_rejects_unavailable_seed(tmp_path: Path) -> None:
    write_world(tmp_path, "modern", None)
    with pytest.raises(GameServiceError) as raised:
        WorldHarness(tmp_path / "app-data").chunkbase_url(tmp_path, "demo", "world", True)
    assert raised.value.error_code == "WORLD_SEED_UNAVAILABLE"


def test_legacy_vanilla_generator_seed_references_follow_world_seed(tmp_path: Path) -> None:
    world_path = write_world(tmp_path, "modern", 123)
    document = load(world_path / "level.dat")
    generator = Compound(
        {
            "type": String("minecraft:noise"),
            "seed": Long(123),
            "biome_source": Compound({"type": String("minecraft:vanilla_layered"), "seed": Long(123)}),
        }
    )
    custom_generator = Compound({"type": String("mod:custom"), "seed": Long(777)})
    document["Data"]["WorldGenSettings"]["dimensions"] = Compound(
        {
            "minecraft:overworld": Compound({"generator": generator}),
            "mod:custom_dimension": Compound({"generator": custom_generator}),
        }
    )
    document.save(world_path / "level.dat", gzipped=True)
    WorldHarness(tmp_path / "app-data").patch_world(tmp_path, "demo", "world", {"seed": "456"}, True)
    dimensions = load(world_path / "level.dat")["Data"]["WorldGenSettings"]["dimensions"]
    assert int(dimensions["minecraft:overworld"]["generator"]["seed"]) == 456
    assert int(dimensions["minecraft:overworld"]["generator"]["biome_source"]["seed"]) == 456
    assert int(dimensions["mod:custom_dimension"]["generator"]["seed"]) == 777


def test_split_file_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ECL.services.game.world_seeds import WorldSeedStore

    world_path = write_world(tmp_path, "split", 123)
    monkeypatch.setattr(WorldSeedStore, "max_nbt_bytes", 8)
    assert WorldCoordinator._read_world(world_path)["seed"] == ""


def test_seed_path_cannot_follow_external_symlink(tmp_path: Path) -> None:
    world_path = write_world(tmp_path, "legacy", 123)
    outside = tmp_path / "outside.dat"
    File({"data": Compound({"seed": Long(999)})}).save(outside)
    settings_file = world_path / "data" / "minecraft" / "world_gen_settings.dat"
    settings_file.parent.mkdir(parents=True)
    try:
        settings_file.symlink_to(outside)
    except OSError:
        pytest.skip("系统未授予符号链接权限")
    assert WorldCoordinator._read_world(world_path)["seed"] == ""
    original = outside.read_bytes()
    with pytest.raises(GameServiceError):
        WorldHarness(tmp_path / "app-data").patch_world(tmp_path, "demo", "world", {"seed": "456"}, True)
    assert outside.read_bytes() == original


def test_seed_write_is_blocked_while_game_is_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world_path = write_world(tmp_path, "modern", 123)
    service = WorldHarness(tmp_path / "app-data")
    monkeypatch.setattr(service, "list_instances", lambda: [{"gamePath": str(tmp_path), "versionId": "demo"}])
    with pytest.raises(GameServiceError) as raised:
        service.patch_world(tmp_path, "demo", "world", {"seed": "456"}, True)
    assert raised.value.error_code == "INSTANCE_IS_RUNNING"
    assert WorldCoordinator._read_world(world_path)["seed"] == "123"
