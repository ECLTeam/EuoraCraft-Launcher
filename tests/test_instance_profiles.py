# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 instance_profiles 模块的自动化测试。
#
# 公开接口：
#   - test_profile_keeps_explicit_false_and_resets_single_field(tmp_path) -> None
#   - test_local_icon_is_validated_and_copied_into_ecl_directory(tmp_path) -> None
#   - test_pcl_metadata_is_read_only_and_ecl_override_has_priority(tmp_path) -> None
#   - test_external_stats_only_add_positive_deltas(tmp_path) -> None
# ============================================================

from __future__ import annotations

import struct

import pytest

from ECL.minecraft.services.game.instance_profiles import InstanceProfileStore
from ECL.minecraft.services.game.version_stats import VersionStatsStore


def _instance(tmp_path, version_id: str = "1.21.8"):
    game_path = tmp_path / ".minecraft"
    instance_path = game_path / "versions" / version_id
    instance_path.mkdir(parents=True)
    (instance_path / f"{version_id}.json").write_text("{}", encoding="utf-8")
    return game_path, instance_path


def _base_version(version_id: str = "1.21.8") -> dict:
    return {
        "id": version_id,
        "versionId": version_id,
        "versionType": "release",
        "path": "",
        "displayName": version_id,
        "primaryLoader": "Vanilla",
        "vanillaName": version_id,
    }


def test_profile_keeps_explicit_false_and_resets_single_field(tmp_path) -> None:
    game_path, _instance_path = _instance(tmp_path)
    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore())

    profile = store.patch_profile(
        game_path,
        "1.21.8",
        {"alias": "朋友服", "favorite": False, "tags": ["机械", "机械", " 生存 "]},
    )

    assert profile["favorite"] is False
    assert profile["tags"] == ["机械", "生存"]
    assert (game_path / "versions" / "1.21.8").name == "1.21.8"

    reset = store.reset_profile_fields(game_path, "1.21.8", ["alias"])
    assert "alias" not in reset
    assert reset["favorite"] is False


@pytest.mark.parametrize(
    ("version_type", "icon_key"),
    [
        ("release", "grass"),
        ("snapshot", "command"),
        ("old_beta", "coal"),
        ("old_alpha", "quartz"),
        ("april_fools", "iron"),
        ("unknown", "grass"),
    ],
)
def test_automatic_icon_uses_version_type_and_preserves_override(tmp_path, version_type, icon_key) -> None:
    game_path, _ = _instance(tmp_path)
    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore())
    version = {**_base_version(), "versionType": version_type}
    assert store.enrich_version(game_path, version)["icon"] == {"type": "builtin", "value": icon_key}
    store.set_icon(game_path, "1.21.8", "builtin", value="grass")
    assert store.enrich_version(game_path, version)["icon"] == {"type": "builtin", "value": "grass"}
    store.reset_profile_fields(game_path, "1.21.8", ["icon"])
    version["primaryLoader"] = "Fabric"
    assert store.enrich_version(game_path, version)["icon"] == {"type": "loader", "value": "fabric"}


def test_resolve_icon_prefers_persisted_icon_and_falls_back_to_automatic(tmp_path) -> None:
    game_path, instance_path = _instance(tmp_path)
    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore())

    assert store.resolve_icon(game_path, "1.21.8", version_type="snapshot", primary_loader="Vanilla") == {
        "type": "builtin",
        "value": "command",
    }
    assert store.resolve_icon(game_path, "1.21.8", version_type="release", primary_loader="Fabric") == {
        "type": "loader",
        "value": "fabric",
    }

    store.set_icon(game_path, "1.21.8", "builtin", value="chest")
    assert store.resolve_icon(game_path, "1.21.8", version_type="snapshot", primary_loader="Vanilla") == {
        "type": "builtin",
        "value": "chest",
    }

    image_path = tmp_path / "icon.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 32, 32) + b"payload")
    store.set_icon(game_path, "1.21.8", "local", source_path=image_path)
    assert store.resolve_icon(game_path, "1.21.8", version_type="release", primary_loader="Vanilla") == {
        "type": "local",
        "value": str(instance_path / ".ecl" / "icon.png"),
    }

    (instance_path / ".ecl" / "instance.json").write_text('{"icon": 1}', encoding="utf-8")
    assert store.resolve_icon(game_path, "1.21.8", version_type="snapshot", primary_loader="Vanilla") == {
        "type": "builtin",
        "value": "command",
    }


def test_local_icon_is_validated_and_copied_into_ecl_directory(tmp_path) -> None:
    game_path, instance_path = _instance(tmp_path)
    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore())
    image_path = tmp_path / "icon.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 32, 32) + b"payload")

    profile = store.set_icon(game_path, "1.21.8", "local", source_path=image_path)

    assert profile["icon"] == {"type": "local", "value": "icon.png"}
    assert (instance_path / ".ecl" / "icon.png").read_bytes() == image_path.read_bytes()

    invalid = tmp_path / "invalid.png"
    invalid.write_text("not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="无法识别"):
        store.set_icon(game_path, "1.21.8", "local", source_path=invalid)

    with pytest.raises(ValueError, match="不受支持"):
        store.set_icon(game_path, "1.21.8", "builtin", value="unknown")

    with pytest.raises(ValueError, match="无效"):
        store.patch_profile(
            game_path,
            "1.21.8",
            {"icon": {"type": "local", "value": "../secret.png"}},
        )


def test_pcl_metadata_is_read_only_and_ecl_override_has_priority(tmp_path) -> None:
    game_path, instance_path = _instance(tmp_path)
    pcl_directory = instance_path / "PCL"
    pcl_directory.mkdir()
    setup_path = pcl_directory / "Setup.ini"
    setup_path.write_text("CustomInfo=第三方描述\nIsStar=True\nVersionLaunchCount=7\n", encoding="utf-8")
    original = setup_path.read_bytes()
    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore())

    imported = store.enrich_version(game_path, _base_version())
    assert imported["description"] == "第三方描述"
    assert imported["favorite"] is True
    assert imported["launchCount"] == 7

    store.patch_profile(game_path, "1.21.8", {"description": "ECL 描述", "favorite": False})
    overridden = store.enrich_version(game_path, _base_version())
    assert overridden["description"] == "ECL 描述"
    assert overridden["favorite"] is False
    assert setup_path.read_bytes() == original


def test_external_stats_only_add_positive_deltas(tmp_path) -> None:
    game_path, _instance_path = _instance(tmp_path)
    stats = VersionStatsStore()

    first = stats.reconcile_external(game_path, "1.21.8", {"pcl": {"launchCount": 4}})
    repeated = stats.reconcile_external(game_path, "1.21.8", {"pcl": {"launchCount": 4}})
    increased = stats.reconcile_external(game_path, "1.21.8", {"pcl": {"launchCount": 6}})
    reset = stats.reconcile_external(game_path, "1.21.8", {"pcl": {"launchCount": 1}})

    assert first["launchCount"] == 4
    assert repeated["launchCount"] == 4
    assert increased["launchCount"] == 6
    assert reset["launchCount"] == 6
