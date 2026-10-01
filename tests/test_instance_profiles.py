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
#   - test_qomicex_manual_index_maps_metadata_without_writing(tmp_path) -> None
#   - test_qomicex_index_is_parsed_once_for_unchanged_scan(tmp_path, monkeypatch) -> None
# ============================================================

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

from ECL.plugins import InstanceCompatibilityRegistry, PluginManager
from ECL.services.game.instance_compat import InstanceCompatibilityReader
from ECL.services.game.instance_profiles import InstanceProfileStore
from ECL.services.game.version_stats import VersionStatsStore


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


def _store_with_qomicex_plugin(tmp_path):
    registry = InstanceCompatibilityRegistry()
    framework = PluginManager(instance_compatibility=registry)
    framework.initialize(tmp_path / "data", Path(__file__).parent.parent)
    assert framework._status.get("qomicex-compat") == "enabled"
    store = InstanceProfileStore(
        tmp_path / "profiles",
        VersionStatsStore(),
        compatibility_reader=InstanceCompatibilityReader(registry),
    )
    return store, framework


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


def test_qomicex_manual_index_maps_metadata_without_writing(tmp_path) -> None:
    game_path, instance_path = _instance(tmp_path)
    qomicex_path = tmp_path / "instances.json"
    qomicex_path.write_text(
        json.dumps(
            [
                {
                    "id": "q-1",
                    "name": "1.21.8",
                    "gameVersion": "1.21.8",
                    "loader": "Vanilla",
                    "gameDir": str(instance_path),
                    "isHidden": True,
                    "isDefault": True,
                    "playTime": 12,
                    "lastPlayed": "2026-08-13T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    original = qomicex_path.read_bytes()
    store, framework = _store_with_qomicex_plugin(tmp_path)

    result = store.enrich_version(
        game_path,
        _base_version(),
        compatibility_options={"qomicex": {"instances_path": qomicex_path}},
    )

    assert result["hidden"] is True
    assert result["pinned"] is True
    assert result["totalRunDurationSeconds"] == 720
    assert result["lastLaunchedAt"] == "2026-08-13T12:00:00Z"
    assert result["availableSources"] == ["qomicex"]
    assert qomicex_path.read_bytes() == original
    framework.close()


def test_qomicex_index_is_parsed_once_for_unchanged_scan(tmp_path, monkeypatch) -> None:
    game_path, instance_path = _instance(tmp_path)
    qomicex_path = tmp_path / "instances.json"
    qomicex_path.write_text(
        json.dumps([{"name": "1.21.8", "gameDir": str(instance_path)}]),
        encoding="utf-8",
    )
    store, framework = _store_with_qomicex_plugin(tmp_path)
    plugin = framework.get_plugin("qomicex-compat")
    assert plugin is not None
    plugin_module = sys.modules[plugin.__class__.__module__]
    original_read_text = plugin_module._read_text
    read_count = 0

    def counted_read_text(path):
        nonlocal read_count
        read_count += 1
        return original_read_text(path)

    monkeypatch.setattr(plugin_module, "_read_text", counted_read_text)

    for _ in range(2):
        store.enrich_version(
            game_path,
            _base_version(),
            compatibility_options={"qomicex": {"instances_path": qomicex_path}},
        )

    assert read_count == 1
    framework.close()
