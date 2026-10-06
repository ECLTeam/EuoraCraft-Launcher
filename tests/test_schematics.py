# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 schematics 模块的自动化测试。
#
# 公开接口：
#   - test_block_color_resolves_base_name_and_falls_back() -> None
#   - test_schematic_preview_rejects_missing_file(tmp_path) -> None
#   - test_schematic_preview_invalid_content_fails_gracefully(tmp_path) -> None
#   - test_litematic_preview_returns_palette_and_regions(tmp_path) -> None
#   - test_litematic_preview_normalizes_negative_compound_size(tmp_path) -> None
#   - test_schematic_assets_extracts_model_closure_and_texture(tmp_path) -> None
#   - test_schem_preview_parses_sponge_layout(tmp_path) -> None
#   - test_downsample_caps_voxel_count(tmp_path) -> None
# ============================================================

from __future__ import annotations

import base64
import hashlib
import json
from array import array
from pathlib import Path
from threading import RLock
from zipfile import ZipFile

import pytest

from ECL.game import ByteArray, Compound, File, Int, IntArray, List, LongArray, String, load_limited
from ECL.services.game.base import GameServiceError
from ECL.services.game.schematics import SchematicCoordinator, _block_color
from ECL.services.game.workspace import WorkspaceCoordinator


class _SchematicHarness(SchematicCoordinator, WorkspaceCoordinator):
    def __init__(self, data_path: Path) -> None:
        self._data_path = data_path
        self._lock = RLock()
        self._schematic_sessions = {}

    def list_instances(self) -> list[dict[str, object]]:
        return []


def _schematic_root(tmp_path: Path) -> Path:
    root = tmp_path / "versions" / "iso" / "schematics"
    root.mkdir(parents=True)
    return root


def _write_litematic(
    path: Path, width: int = 4, height: int = 3, length: int = 3, position: tuple[int, int, int] = (1, 2, 3)
) -> None:
    # BlockStatePalette 有 3 个方块（air/dirt/stone），MC 调色板位宽最小为 4。
    entries = [
        Compound({"Name": String("minecraft:air")}),
        Compound({"Name": String("minecraft:dirt")}),
        Compound({"Name": String("minecraft:stone")}),
    ]
    per_word = 64 // 4
    total = abs(width) * abs(height) * abs(length)
    words = [0] * ((total + per_word - 1) // per_word)

    def set_index(index: int, palette_id: int) -> None:
        word_index = index // per_word
        offset = (index % per_word) * 4
        words[word_index] |= palette_id << offset

    # x 最内层；把 (0,0,0) 设为 dirt、(1,0,0) 设为 stone，其余默认为 air。
    set_index(0, 1)
    set_index(1, 2)
    region = Compound(
        {
            "Size": Compound({"x": Int(width), "y": Int(height), "z": Int(length)}),
            "Position": Compound({"x": Int(position[0]), "y": Int(position[1]), "z": Int(position[2])}),
            "BlockStatePalette": List(entries),
            "BlockStates": LongArray(words),
        }
    )
    document = File({"Regions": Compound({"main": region})}, gzipped=True)
    document.save(path, gzipped=True)


def _write_schem(path: Path) -> None:
    palette = Compound(
        {
            "minecraft:air": Int(0),
            "minecraft:grass_block": Int(1),
            "minecraft:stone": Int(2),
            "minecraft:piston[extended=false,facing=north]": Int(3),
        }
    )
    # Sponge 索引 = (y * length + z) * width + x；size[2,2,1] 共 4 格。
    blocks = ByteArray(bytes([1, 0, 2, 3]))
    document = File(
        {
            "Version": Int(2),
            "DataVersion": Int(2586),
            "Width": Int(2),
            "Height": Int(2),
            "Length": Int(1),
            "Size": IntArray([2, 2, 1]),
            "Palette": palette,
            "Blocks": blocks,
        },
        gzipped=True,
    )
    document.save(path, gzipped=True)


def test_block_color_resolves_base_name_and_falls_back() -> None:
    assert _block_color("minecraft:stone") == (124, 124, 124)
    assert _block_color("minecraft:oak_log[axis=y]") == (94, 74, 44)
    assert _block_color("minecraft:air") == (0, 0, 0)


def test_schematic_preview_rejects_missing_file(tmp_path: Path) -> None:
    _schematic_root(tmp_path)
    harness = _SchematicHarness(tmp_path / "app-data")
    with pytest.raises(GameServiceError) as raised:
        harness.schematic_preview(tmp_path, "iso", "nonexistent.schem", True)
    # 路径解析统一复用资源层语义，缺失文件归入资源未找到。
    assert raised.value.error_code == "RESOURCE_NOT_FOUND"


def test_schematic_preview_invalid_content_fails_gracefully(tmp_path: Path) -> None:
    root = _schematic_root(tmp_path)
    (root / "bad.schem").write_bytes(b"not-a-schematic")
    harness = _SchematicHarness(tmp_path / "app-data")
    with pytest.raises(GameServiceError) as raised:
        harness.schematic_preview(tmp_path, "iso", "bad.schem", True)
    assert raised.value.error_code == "SCHEMATIC_INVALID"


def test_litematic_preview_returns_palette_and_regions(tmp_path: Path) -> None:
    _write_litematic(_schematic_root(tmp_path) / "build.litematic")
    harness = _SchematicHarness(tmp_path / "app-data")

    result = harness.schematic_preview(tmp_path, "iso", "build.litematic", True)

    assert result["type"] == "litematic"
    assert result["size"] == [5, 5, 6]  # position [1,2,3] + size [4,3,3]
    region = result["regions"][0]
    assert region["name"] == "main"
    assert region["size"] == [4, 3, 3]
    assert region["position"] == [1, 2, 3]
    assert region["palette"] == [
        {"name": "minecraft:air", "properties": {}, "color": [0, 0, 0]},
        {"name": "minecraft:dirt", "properties": {}, "color": [124, 94, 70]},
        {"name": "minecraft:stone", "properties": {}, "color": [124, 124, 124]},
    ]
    assert len(region["indices"]) == 36
    assert region["indices"][0] == 1
    assert region["indices"][1] == 2


def test_litematic_preview_normalizes_negative_compound_size(tmp_path: Path) -> None:
    _write_litematic(
        _schematic_root(tmp_path) / "negative.litematic", width=-4, height=-3, length=-3, position=(3, 2, 2)
    )
    harness = _SchematicHarness(tmp_path / "app-data")

    result = harness.schematic_preview(tmp_path, "iso", "negative.litematic", True)

    assert result["size"] == [4, 3, 3]
    region = result["regions"][0]
    assert region["size"] == [4, 3, 3]
    assert region["position"] == [0, 0, 0]
    assert len(region["indices"]) == 36


def test_schematic_assets_extracts_model_closure_and_texture(tmp_path: Path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "demo"
    version_path.mkdir(parents=True)
    with ZipFile(version_path / "demo.jar", "w") as archive:
        archive.writestr(
            "assets/minecraft/blockstates/stone.json", json.dumps({"variants": {"": {"model": "block/stone"}}})
        )
        archive.writestr(
            "assets/minecraft/models/block/stone.json",
            json.dumps(
                {
                    "parent": "block/cube_all",
                    "textures": {"all": {"force_translucent": True, "sprite": "minecraft:block/stone"}},
                }
            ),
        )
        archive.writestr("assets/minecraft/models/block/cube_all.json", json.dumps({"parent": "block/block"}))
        archive.writestr("assets/minecraft/models/block/block.json", "{}")
        archive.writestr("assets/minecraft/textures/block/stone.png", b"png")
        archive.writestr("assets/minecraft/textures/block/stone.png.mcmeta", json.dumps({"animation": {}}))
    harness = _SchematicHarness(tmp_path / "app-data")

    bundle = harness.schematic_assets(game_path, "demo", ["minecraft:stone"], True)

    assert "minecraft:stone" in bundle["blockstates"]
    assert "minecraft:block/stone" in bundle["models"]
    assert "minecraft:block/cube_all" in bundle["models"]
    assert bundle["textures"]["minecraft:block/stone"] == "cG5n"
    assert bundle["animated"] == ["minecraft:block/stone"]
    assert bundle["missingBlocks"] == []


def test_schem_preview_parses_sponge_layout(tmp_path: Path) -> None:
    _write_schem(_schematic_root(tmp_path) / "build.schem")
    harness = _SchematicHarness(tmp_path / "app-data")

    result = harness.schematic_preview(tmp_path, "iso", "build.schem", True)

    assert result["type"] == "schem"
    assert result["size"] == [2, 2, 1]
    region = result["regions"][0]
    assert region["size"] == [2, 2, 1]
    assert len(region["indices"]) == 4
    assert region["indices"][0] == 1  # grass_block
    assert region["indices"][2] == 2  # stone
    piston = region["palette"][3]
    assert piston["name"] == "minecraft:piston"
    assert piston["properties"] == {"extended": "false", "facing": "north"}


def test_downsample_caps_voxel_count(tmp_path: Path) -> None:
    harness = _SchematicHarness(tmp_path / "app-data")
    huge = {
        "name": "big",
        "size": [128, 128, 128],
        "position": [0, 0, 0],
        "indices": [0] * (128 * 128 * 128),
        "palette": [(10, 10, 10)],
        "maskBits": 4,
    }
    sampled = harness._downsample_region(huge)
    assert sampled["size"][0] <= 64
    assert len(sampled["indices"]) <= 262144


def test_stream_session_preserves_large_litematic_without_sampling(tmp_path: Path) -> None:
    _write_litematic(_schematic_root(tmp_path) / "large.litematic", width=65, height=65, length=65)
    harness = _SchematicHarness(tmp_path / "app-data")

    opened = harness.schematic_session_open(tmp_path, "iso", "large.litematic", True)

    assert opened["size"] == [65, 65, 65]
    assert opened["materialCounts"] == {"minecraft:dirt": 1, "minecraft:stone": 1}
    assert opened["chunks"] == [[0, 0, 0]]
    batch = harness.schematic_session_chunks(opened["sessionId"], [(0, 0, 0)])
    decoded = array("H")
    decoded.frombytes(base64.b64decode(batch["chunks"][0]["indices"]))
    assert decoded[0] == 1
    assert decoded[1] == 2
    assert harness.schematic_session_close(opened["sessionId"]) == {"closed": True}
    assert harness.schematic_session_close(opened["sessionId"]) == {"closed": False}


def test_litematic_palette_indices_cross_long_word_boundary(tmp_path: Path) -> None:
    schematic_path = _schematic_root(tmp_path) / "packed.litematic"
    palette = List([Compound({"Name": String("minecraft:air")})])
    palette.extend(Compound({"Name": String(f"minecraft:block_{index}")}) for index in range(1, 32))
    region = Compound(
        {
            "Size": Compound({"x": Int(14), "y": Int(1), "z": Int(1)}),
            "Position": Compound({"x": Int(0), "y": Int(0), "z": Int(0)}),
            "BlockStatePalette": palette,
            "BlockStates": LongArray([0xF000000000000000 - (1 << 64), 11]),
        }
    )
    File({"Regions": Compound({"main": region})}, gzipped=True).save(schematic_path, gzipped=True)
    harness = _SchematicHarness(tmp_path / "app-data")

    legacy = harness.schematic_preview(tmp_path, "iso", "packed.litematic", True)
    streamed = harness.schematic_session_open(tmp_path, "iso", "packed.litematic", True)

    assert legacy["regions"][0]["indices"][12:14] == [31, 5]
    assert streamed["materialCounts"] == {"minecraft:block_31": 1, "minecraft:block_5": 1}


def test_stream_session_reads_schem_properties_and_chunk_boundary(tmp_path: Path) -> None:
    schematic_path = _schematic_root(tmp_path) / "boundary.schem"
    File(
        {
            "Size": IntArray([17, 1, 1]),
            "Palette": Compound({"minecraft:air": Int(0), "minecraft:piston[extended=false,facing=north]": Int(1)}),
            "Blocks": ByteArray(bytes([0] * 15 + [1, 1])),
        },
        gzipped=True,
    ).save(schematic_path, gzipped=True)
    harness = _SchematicHarness(tmp_path / "app-data")

    opened = harness.schematic_session_open(tmp_path, "iso", "boundary.schem", True)

    assert opened["chunks"] == [[0, 0, 0], [1, 0, 0]]
    assert opened["materialCounts"] == {"minecraft:piston": 2}
    assert opened["palette"][1]["properties"] == {"extended": "false", "facing": "north"}
    batch = harness.schematic_session_chunks(opened["sessionId"], [(0, 0, 0), (1, 0, 0)])
    first = array("H")
    second = array("H")
    first.frombytes(base64.b64decode(batch["chunks"][0]["indices"]))
    second.frombytes(base64.b64decode(batch["chunks"][1]["indices"]))
    assert first[15] == second[0] == 1


def test_stream_session_rejects_limit_and_changed_file(tmp_path: Path) -> None:
    schematic_path = _schematic_root(tmp_path) / "build.schem"
    _write_schem(schematic_path)
    harness = _SchematicHarness(tmp_path / "app-data")
    harness.stream_max_voxels = 3
    with pytest.raises(GameServiceError) as raised:
        harness.schematic_session_open(tmp_path, "iso", "build.schem", True)
    assert raised.value.error_code == "SCHEMATIC_TOO_LARGE"
    harness.stream_max_voxels = 16_000_000
    opened = harness.schematic_session_open(tmp_path, "iso", "build.schem", True)
    schematic_path.write_bytes(schematic_path.read_bytes() + b"changed")
    with pytest.raises(GameServiceError) as stale:
        harness.schematic_session_chunks(opened["sessionId"], [(0, 0, 0)])
    assert stale.value.error_code == "SCHEMATIC_SESSION_EXPIRED"


def test_limited_nbt_reader_rejects_gzip_expansion(tmp_path: Path) -> None:
    schematic_path = _schematic_root(tmp_path) / "build.schem"
    _write_schem(schematic_path)
    with pytest.raises(ValueError, match="安全上限"):
        load_limited(schematic_path, 10)


def test_schematic_assets_follows_launcher_language_and_falls_back_to_english(tmp_path: Path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "demo"
    version_path.mkdir(parents=True)
    (version_path / "options.txt").write_text("lang:en_us\n", encoding="utf-8")
    (version_path / "demo.json").write_text(json.dumps({"assetIndex": {"id": "demo-assets"}}), encoding="utf-8")
    with ZipFile(version_path / "demo.jar", "w") as archive:
        archive.writestr(
            "assets/minecraft/lang/en_us.json",
            json.dumps({"block.minecraft.stone": "Stone", "block.minecraft.dirt": "Dirt"}),
        )
    language = json.dumps({"block.minecraft.stone": "石头"}, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha1(language).hexdigest()
    object_path = game_path / "assets" / "objects" / digest[:2] / digest
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(language)
    index_path = game_path / "assets" / "indexes" / "demo-assets.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"objects": {"minecraft/lang/zh_cn.json": {"hash": digest}}}), encoding="utf-8")
    harness = _SchematicHarness(tmp_path / "app-data")

    bundle = harness.schematic_assets(
        game_path,
        "demo",
        ["minecraft:stone", "minecraft:dirt", "example:unknown"],
        True,
        "zh-CN",
    )

    assert bundle["blockNames"] == {"minecraft:stone": "石头", "minecraft:dirt": "Dirt"}
    english = harness.schematic_assets(game_path, "demo", ["minecraft:stone"], True, "en-US")
    assert english["blockNames"] == {"minecraft:stone": "Stone"}


def test_schematic_material_manifest_exports_localized_json_and_csv(tmp_path: Path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "demo"
    schematic_root = version_path / "schematics"
    schematic_root.mkdir(parents=True)
    _write_litematic(schematic_root / "build.litematic")
    (version_path / "demo.json").write_text(json.dumps({"assetIndex": {"id": "demo-assets"}}), encoding="utf-8")
    with ZipFile(version_path / "demo.jar", "w") as archive:
        archive.writestr(
            "assets/minecraft/lang/en_us.json",
            json.dumps({"block.minecraft.stone": "Stone", "block.minecraft.dirt": "Dirt"}),
        )
    language = json.dumps({"block.minecraft.stone": "石头"}, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha1(language).hexdigest()
    object_path = game_path / "assets" / "objects" / digest[:2] / digest
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(language)
    index_path = game_path / "assets" / "indexes" / "demo-assets.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"objects": {"minecraft/lang/zh_cn.json": {"hash": digest}}}), encoding="utf-8")
    harness = _SchematicHarness(tmp_path / "app-data")
    opened = harness.schematic_session_open(game_path, "demo", "build.litematic", True)

    json_path = tmp_path / "materials.json"
    csv_path = tmp_path / "materials.csv"
    assert harness.export_schematic_material_manifest(
        game_path,
        "demo",
        opened["sessionId"],
        json_path,
        "json",
        "zh-CN",
        ["minecraft:dirt"],
        True,
    ) == {"path": str(json_path)}
    assert harness.export_schematic_material_manifest(
        game_path,
        "demo",
        opened["sessionId"],
        csv_path,
        "csv",
        "zh-CN",
        ["minecraft:dirt"],
        True,
    ) == {"path": str(csv_path)}

    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["materials"] == [
        {"id": "minecraft:dirt", "name": "Dirt", "count": 1, "hasTexture": False, "hasTranslation": True},
        {"id": "minecraft:stone", "name": "石头", "count": 1, "hasTexture": True, "hasTranslation": True},
    ]
    assert "minecraft:dirt,Dirt,1,False,True" in csv_path.read_text(encoding="utf-8")


def test_special_model_textures_include_fluids_and_block_entities() -> None:
    assert SchematicCoordinator._special_texture_ids("minecraft:water") == {
        "minecraft:block/water_still",
        "minecraft:block/water_flow",
    }
    assert SchematicCoordinator._special_texture_ids("minecraft:chest") == {"minecraft:entity/chest/normal"}
    assert SchematicCoordinator._special_texture_ids("minecraft:oak_wall_sign") == {
        "minecraft:entity/signs/oak",
        "minecraft:entity/signs/hanging/oak",
    }
    assert SchematicCoordinator._special_texture_ids("example:water") == set()
