from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ECL.game import (
    ByteArray,
    Compound,
    File,
    Float,
    GameDataError,
    Int,
    List,
    LongArray,
    ResourceFilePolicy,
    String,
    load,
)
from ECL.services.game.resources import ResourceCoordinator
from ECL.services.game.workspace import WorkspaceCoordinator


class ResourceService(ResourceCoordinator, WorkspaceCoordinator):
    logger = logging.getLogger(__name__)


def write_pack(path: Path, *, shader: bool = False, modern: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if shader:
            archive.writestr("outer/shaders/final.fsh", "void main() {}")
        else:
            pack = {
                "description": "Valid",
                **({"min_format": [82, 0], "max_format": 99} if modern else {"pack_format": 34}),
            }
            archive.writestr("pack.mcmeta", json.dumps({"pack": pack}))


def write_schematic(path: Path, kind: str) -> None:
    if kind == "litematic":
        root = {
            "Regions": Compound(
                {
                    "main": Compound(
                        {
                            "Size": Compound({"x": Int(-1), "y": Int(1), "z": Int(1)}),
                            "Position": Compound({"x": Int(0), "y": Int(0), "z": Int(0)}),
                            "BlockStatePalette": List([Compound({"Name": String("minecraft:air")})]),
                            "BlockStates": LongArray([0]),
                        }
                    )
                }
            ),
        }
    else:
        root = {"Width": Int(1), "Height": Int(1), "Length": Int(1)}
        if kind == "schematic":
            root.update({"Materials": String("Alpha"), "Blocks": ByteArray(b"\x01"), "Data": ByteArray(b"\x00")})
        else:
            root.update({"Version": Int(int(kind)), "DataVersion": Int(2586)})
            palette = Compound({"minecraft:air": Int(0)})
            if kind == "3":
                root["Blocks"] = Compound({"Palette": palette, "Data": ByteArray(b"\x00")})
                root = {"Schematic": Compound(root)}
            else:
                root.update({"Palette": palette, "BlockData": ByteArray(b"\x00")})
    path.parent.mkdir(parents=True, exist_ok=True)
    File(root).save(path, gzipped=True)


@pytest.mark.parametrize("resource_type", ["shaderpack", "datapack", "resourcepack", "schematic"])
@pytest.mark.parametrize("is_isolated", [False, True])
def test_list_filters_wrong_extensions_and_invalid_content(
    tmp_path: Path, resource_type: str, is_isolated: bool
) -> None:
    service = ResourceService()
    (tmp_path / "versions" / ("demo" if is_isolated else "") / "saves" / "world").mkdir(parents=True)
    root = service._resource_root(tmp_path, "demo", resource_type, is_isolated, "world")
    root.mkdir(parents=True)
    if resource_type == "schematic":
        valid = root / "valid.SCHEMATIC"
        write_schematic(valid, "schematic")
        (root / "fake.schem").write_text("not nbt", encoding="utf-8")
    else:
        valid = root / "valid.ZIP"
        write_pack(valid, shader=resource_type == "shaderpack", modern=resource_type == "datapack")
        (root / "fake.zip").write_text("not zip", encoding="utf-8")
        with zipfile.ZipFile(root / "unrelated.zip", "w") as archive:
            archive.writestr("notes.txt", "not a pack")
    for name in ("notes.txt", "icon.png", "mod.jar", "copy.zip.bak"):
        (root / name).write_bytes(b"unrelated")
    (root / "random-folder").mkdir()
    before = set(root.iterdir())
    items = service.list_resources(tmp_path, "demo", resource_type, is_isolated, "world")
    assert [item["id"] for item in items] == [valid.name]
    assert set(root.iterdir()) == before


def test_list_keeps_only_valid_datapack_directories(tmp_path: Path) -> None:
    service = ResourceService()
    (tmp_path / "versions" / "demo" / "saves" / "world").mkdir(parents=True)
    root = service._resource_root(tmp_path, "demo", "datapack", True, "world")
    for name, content in [("valid", '{"pack":{"description":"Directory","pack_format":34}}'), ("invalid", "{}")]:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "pack.mcmeta").write_text(content, encoding="utf-8")
    assert [item["id"] for item in service.list_resources(tmp_path, "demo", "datapack", True, "world")] == ["valid"]


@pytest.mark.parametrize(
    "kind,extension",
    [("litematic", "litematic"), ("1", "schem"), ("2", "schem"), ("3", "schem"), ("schematic", "schematic")],
)
def test_valid_schematic_structures_survive_filtering(tmp_path: Path, kind: str, extension: str) -> None:
    service = ResourceService()
    root = service._resource_root(tmp_path, "demo", "schematic", True)
    write_schematic(root / f"valid.{extension}", kind)
    File({"Data": Compound({"LevelName": String("World")})}).save(root / f"fake.{extension}")
    assert [item["id"] for item in service.list_resources(tmp_path, "demo", "schematic", True)] == [
        f"valid.{extension}"
    ]


@pytest.mark.parametrize(
    "resource_type,invalid_name",
    [("shaderpack", "notes.txt"), ("datapack", "mod.jar"), ("schematic", "world.nbt"), ("shaderpack", "broken.zip")],
)
def test_install_prevalidates_entire_batch_before_submitting(
    tmp_path: Path, resource_type: str, invalid_name: str
) -> None:
    service = ResourceService()
    (tmp_path / "versions" / "demo" / "saves" / "world").mkdir(parents=True)
    submitted = []
    service._application_operations = SimpleNamespace(submit=lambda *args: submitted.append(args))
    valid = tmp_path / "valid.zip"
    if resource_type == "schematic":
        valid = tmp_path / "valid.schematic"
        write_schematic(valid, "schematic")
    else:
        write_pack(valid, shader=resource_type == "shaderpack")
    invalid = tmp_path / invalid_name
    invalid.write_bytes(b"invalid")
    with pytest.raises(GameDataError):
        service.install_resources(tmp_path, "demo", resource_type, [valid, invalid], True, "world")
    assert submitted == []
    assert not service._resource_root(tmp_path, "demo", resource_type, True, "world").exists()


def test_datapack_install_rejects_directory_even_if_list_accepts_it(tmp_path: Path) -> None:
    (tmp_path / "versions" / "demo" / "saves" / "world").mkdir(parents=True)
    source = tmp_path / "pack"
    source.mkdir()
    (source / "pack.mcmeta").write_text('{"pack":{"description":"Pack","pack_format":34}}', encoding="utf-8")
    service = ResourceService()
    service._application_operations = SimpleNamespace(submit=lambda *_args: {})
    with pytest.raises(GameDataError):
        service.install_resources(tmp_path, "demo", "datapack", [source], True, "world")


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("2", "BlockData", ByteArray(b"\x03")),
        ("2", "BlockData", ByteArray(b"\x80")),
        ("2", "Width", Int(2)),
        ("2", "Version", String("2")),
        ("2", "Version", Float(2.0)),
        ("2", "DataVersion", String("2586")),
        ("schematic", "Data", ByteArray(b"")),
        ("schematic", "AddBlocks", ByteArray(b"\x00\x00")),
        ("schematic", "Materials", String("Classic")),
        ("litematic", "Regions", Compound()),
    ],
)
def test_invalid_schematic_storage_is_rejected(tmp_path: Path, kind: str, field: str, value: object) -> None:
    path = tmp_path / ("bad.schem" if kind == "2" else f"bad.{kind}")
    write_schematic(path, kind)
    document = load(path)
    document[field] = value
    document.save(path)
    with pytest.raises(GameDataError):
        ResourceFilePolicy.validate(path, "schematic")


def test_list_cache_reuses_validation_but_not_install_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pack.zip"
    write_pack(path)
    ResourceFilePolicy.invalidate()
    original = ResourceFilePolicy._validate_content
    calls = []

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(ResourceFilePolicy, "_validate_content", counted)
    ResourceFilePolicy.validate(path, "datapack", use_cache=True)
    ResourceFilePolicy.validate(path, "datapack", use_cache=True)
    assert len(calls) == 1
    ResourceFilePolicy.validate(path, "datapack")
    assert len(calls) == 2
    write_pack(path, modern=True)
    ResourceFilePolicy.validate(path, "datapack", use_cache=True)
    assert len(calls) == 3
    ResourceFilePolicy.invalidate()
    ResourceFilePolicy.validate(path, "datapack", use_cache=True)
    assert len(calls) == 4
    monkeypatch.setattr(ResourceFilePolicy, "cache_seconds", -1.0)
    ResourceFilePolicy.validate(path, "datapack", use_cache=True)
    assert len(calls) == 5


def test_cache_limits_expiry_and_directory_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ResourceFilePolicy.invalidate()
    monkeypatch.setattr(ResourceFilePolicy, "max_cache_entries", 2)
    for index in range(3):
        path = tmp_path / f"pack{index}.zip"
        write_pack(path)
        ResourceFilePolicy.validate(path, "resourcepack", use_cache=True)
    assert len(ResourceFilePolicy._cache) == 2
    directory = tmp_path / "folder"
    directory.mkdir()
    metadata_path = directory / "pack.mcmeta"
    metadata_path.write_text('{"pack":{"description":"Good","pack_format":34}}', encoding="utf-8")
    ResourceFilePolicy.validate(directory, "datapack", allow_directory=True, use_cache=True)
    metadata_path.write_text("{}", encoding="utf-8")
    with pytest.raises(GameDataError):
        ResourceFilePolicy.validate(directory, "datapack", allow_directory=True, use_cache=True)
    monkeypatch.setattr(ResourceFilePolicy, "cache_seconds", -1)
    ResourceFilePolicy.validate(tmp_path / "pack2.zip", "resourcepack", use_cache=True)
    ResourceFilePolicy.invalidate()


def test_read_budgets_report_size_instead_of_invalid_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "valid.schematic"
    write_schematic(path, "schematic")
    monkeypatch.setattr(ResourceFilePolicy, "max_nbt_bytes", 8)
    with pytest.raises(GameDataError) as raised:
        ResourceFilePolicy.validate(path, "schematic")
    assert raised.value.error_code == "RESOURCE_TOO_LARGE"
    pack_path = tmp_path / "valid.zip"
    write_pack(pack_path)
    monkeypatch.setattr(ResourceFilePolicy, "max_metadata_bytes", 8)
    with pytest.raises(GameDataError) as raised:
        ResourceFilePolicy.validate(pack_path, "datapack")
    assert raised.value.error_code == "RESOURCE_TOO_LARGE"


@pytest.mark.parametrize(
    "resource_type,kind", [("shaderpack", "zip"), ("resourcepack", "zip"), ("schematic", "schematic")]
)
def test_valid_install_copies_to_visible_resource(tmp_path: Path, resource_type: str, kind: str) -> None:
    source = tmp_path / f"source.{kind}"
    if kind == "schematic":
        write_schematic(source, kind)
    else:
        write_pack(source, shader=resource_type == "shaderpack")
    context = SimpleNamespace(check_cancelled=lambda: None, progress=lambda *_args: None)
    service = ResourceService()
    service._application_operations = SimpleNamespace(submit=lambda _name, worker: worker(context))
    result = service.install_resources(tmp_path, "demo", resource_type, [source], True)
    assert result["installed"] == [source.name]
    assert [item["id"] for item in service.list_resources(tmp_path, "demo", resource_type, True)] == [source.name]
    assert not list(service._resource_root(tmp_path, "demo", resource_type, True).glob(".*.ecl-tmp*"))


def test_install_rechecks_copied_content_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.zip"
    write_pack(source, shader=True)
    original_copy = shutil.copy2

    def changed_copy(source_path, target_path):
        original_copy(source_path, target_path)
        Path(target_path).write_bytes(b"changed during copy")

    monkeypatch.setattr(shutil, "copy2", changed_copy)
    service = ResourceService()
    context = SimpleNamespace(check_cancelled=lambda: None, progress=lambda *_args: None)
    service._application_operations = SimpleNamespace(submit=lambda _name, worker: worker(context))
    with pytest.raises(GameDataError):
        service.install_resources(tmp_path, "demo", "shaderpack", [source], True)
    assert list(service._resource_root(tmp_path, "demo", "shaderpack", True).iterdir()) == []


def test_corrupt_deflate_member_does_not_break_other_list_entries(tmp_path: Path) -> None:
    service = ResourceService()
    root = service._resource_root(tmp_path, "demo", "resourcepack", True)
    write_pack(root / "valid.zip")
    bad_path = root / "bad.zip"
    write_pack(bad_path)
    content = bytearray(bad_path.read_bytes())
    with zipfile.ZipFile(bad_path) as archive:
        entry = archive.getinfo("pack.mcmeta")
        payload_offset = entry.header_offset + 30 + len(entry.filename.encode("utf-8")) + len(entry.extra)
    content[payload_offset] = 0xFF
    bad_path.write_bytes(content)
    assert [item["id"] for item in service.list_resources(tmp_path, "demo", "resourcepack", True)] == ["valid.zip"]
