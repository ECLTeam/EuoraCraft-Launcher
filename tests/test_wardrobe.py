# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 wardrobe 模块的自动化测试。
#
# 公开接口：
#   - png_header(width, height, suffix=…) -> bytes
#   - write_texture(path, width, height, suffix=…) -> Path
#   - test_import_copies_texture_and_deduplicates_by_kind_and_hash(tmp_path) -> None
#   - test_import_bytes_persists_downloaded_account_skin(tmp_path) -> None
#   - test_update_and_delete_preserve_uploaded_account_semantics(tmp_path) -> None
#   - test_favorite_skin_is_persisted_and_sorted_first(tmp_path) -> None
#   - test_import_rejects_invalid_dimensions(tmp_path, kind, width, height) -> None
#   - test_import_accepts_legacy_and_hd_skin_for_preview(tmp_path) -> None
#   - test_import_rejects_invalid_png_and_large_file(tmp_path) -> None
#   - test_corrupt_metadata_is_backed_up_before_recovery(tmp_path) -> None
# ============================================================

import json
import struct
from pathlib import Path

import pytest

from ECL.services.account.wardrobe import WardrobeError, WardrobeStore


def png_header(width: int, height: int, suffix: bytes = b"") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height) + suffix


def write_texture(path: Path, width: int, height: int, suffix: bytes = b"") -> Path:
    path.write_bytes(png_header(width, height, suffix))
    return path


def test_import_copies_texture_and_deduplicates_by_kind_and_hash(tmp_path: Path) -> None:
    store = WardrobeStore(tmp_path)
    source = write_texture(tmp_path / "skin.png", 64, 64)

    item, deduplicated = store.import_file(source, "skin", model="slim")
    duplicate, duplicate_hit = store.import_file(source, "skin", model="classic")

    source.unlink()
    loaded_item, texture = store.read_texture(item["id"])
    assert not deduplicated
    assert duplicate_hit
    assert duplicate["id"] == item["id"]
    assert loaded_item["model"] == "slim"
    assert texture == png_header(64, 64)


def test_import_bytes_persists_downloaded_account_skin(tmp_path: Path) -> None:
    store = WardrobeStore(tmp_path)
    texture = png_header(64, 64, b"account-skin")

    item, deduplicated = store.import_bytes(texture, "skin", "Player 当前皮肤", "slim")
    duplicate, duplicate_hit = store.import_bytes(texture, "skin", "重复名称", "classic")

    assert not deduplicated
    assert duplicate_hit
    assert duplicate["id"] == item["id"]
    assert item["name"] == "Player 当前皮肤"
    assert item["model"] == "slim"
    assert store.read_texture(item["id"])[1] == texture


def test_update_and_delete_preserve_uploaded_account_semantics(tmp_path: Path) -> None:
    store = WardrobeStore(tmp_path)
    item, _ = store.import_file(write_texture(tmp_path / "skin.png", 64, 64), "skin")

    updated = store.update_item(item["id"], "新皮肤", "slim")
    store.delete_item(item["id"])

    assert updated["name"] == "新皮肤"
    assert updated["model"] == "slim"
    assert store.list_items() == []
    with pytest.raises(WardrobeError, match="不存在"):
        store.read_texture(item["id"])


def test_favorite_skin_is_persisted_and_sorted_first(tmp_path: Path) -> None:
    store = WardrobeStore(tmp_path)
    first, _ = store.import_bytes(png_header(64, 64, b"first"), "skin", "First")
    second, _ = store.import_bytes(png_header(64, 64, b"second"), "skin", "Second")

    updated = store.update_item(first["id"], None, None, True)
    reloaded = WardrobeStore(tmp_path)

    assert updated["favorite"] is True
    assert [item["id"] for item in reloaded.list_items()] == [first["id"], second["id"]]


@pytest.mark.parametrize(
    ("kind", "width", "height"),
    [
        ("skin", 64, 48),
        ("skin", 65, 64),
        ("cape", 64, 64),
        ("cape", 128, 32),
        ("skin", 2048, 2048),
    ],
)
def test_import_rejects_invalid_dimensions(tmp_path: Path, kind: str, width: int, height: int) -> None:
    store = WardrobeStore(tmp_path)
    source = write_texture(tmp_path / f"{kind}.png", width, height)

    with pytest.raises(WardrobeError) as exc_info:
        store.import_file(source, kind)  # type: ignore[arg-type]

    assert exc_info.value.error_code == "WARDROBE_INVALID_DIMENSIONS"


def test_import_accepts_legacy_and_hd_skin_for_preview(tmp_path: Path) -> None:
    store = WardrobeStore(tmp_path)

    legacy, _ = store.import_file(write_texture(tmp_path / "legacy.png", 64, 32), "skin")
    high_definition, _ = store.import_file(write_texture(tmp_path / "hd.png", 128, 128), "skin")

    assert (legacy["width"], legacy["height"]) == (64, 32)
    assert (high_definition["width"], high_definition["height"]) == (128, 128)


def test_import_rejects_invalid_png_and_large_file(tmp_path: Path) -> None:
    store = WardrobeStore(tmp_path)
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not a png")
    large = tmp_path / "large.png"
    large.write_bytes(png_header(64, 64) + b"0" * WardrobeStore.max_texture_bytes)

    with pytest.raises(WardrobeError) as invalid_error:
        store.import_file(invalid, "skin")
    with pytest.raises(WardrobeError) as large_error:
        store.import_file(large, "skin")

    assert invalid_error.value.error_code == "WARDROBE_INVALID_PNG"
    assert large_error.value.error_code == "WARDROBE_FILE_TOO_LARGE"


def test_corrupt_metadata_is_backed_up_before_recovery(tmp_path: Path) -> None:
    metadata = tmp_path / "wardrobe" / "wardrobe.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{broken", encoding="utf-8")

    store = WardrobeStore(tmp_path)
    store.import_file(write_texture(tmp_path / "skin.png", 64, 64), "skin")

    backups = list(metadata.parent.glob("wardrobe.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"
    assert json.loads(metadata.read_text(encoding="utf-8"))["version"] == 1
