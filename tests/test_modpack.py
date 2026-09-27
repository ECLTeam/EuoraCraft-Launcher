# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对整合包格式识别与解析模块的自动化测试。
#
# 公开接口：
#   - test_detect_pack_formats(tmp_path) -> None
#   - test_parse_modrinth_plan(tmp_path) -> None
#   - test_parse_modrinth_missing_minecraft_raises(tmp_path) -> None
#   - test_parse_modrinth_sanitizes_unsafe_paths(tmp_path) -> None
#   - test_parse_modrinth_unknown_dependency_warns(tmp_path) -> None
#   - test_parse_curseforge_plan(tmp_path) -> None
#   - test_parse_curseforge_missing_minecraft_raises(tmp_path) -> None
#   - test_parse_curseforge_uses_primary_loader(tmp_path) -> None
#   - test_parse_curseforge_unexpected_manifest_type_warns(tmp_path) -> None
#   - test_build_pack_plan_rejects_unknown(tmp_path) -> None
#   - test_build_pack_plan_rejects_ecl_legacy(tmp_path) -> None
#   - test_build_pack_plan_rejects_missing_directory(tmp_path) -> None
# ============================================================

import json
from pathlib import Path

import pytest

from ECL.services.game.modpack import ModpackPlan, build_pack_plan, detect_pack_format
from ECL.utils.errors import GameServiceError


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_detect_pack_formats(tmp_path) -> None:
    """各格式特征文件应返回对应标签，无特征时返回 unknown。"""
    markers = {
        "modrinth.index.json": "mrpack",
        "manifest.json": "curseforge",
        "mcbbs.packmeta": "mcbbs",
        "modpack.json": "hmcl",
        "mmc-pack.json": "multimc",
        "ecl-pack.json": "ecl-legacy",
    }
    for marker, expected in markers.items():
        root = tmp_path / expected
        root.mkdir()
        (root / marker).write_text("{}", encoding="utf-8")
        assert detect_pack_format(root) == expected

    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_pack_format(empty) == "unknown"


def test_parse_modrinth_plan(tmp_path) -> None:
    """mrpack 清单应完整映射依赖、加载器、哈希与客户端适用性声明。"""
    root = tmp_path / "pack"
    root.mkdir()
    (root / "overrides").mkdir()
    _write_json(
        root / "modrinth.index.json",
        {
            "formatVersion": 1,
            "game": "minecraft",
            "name": "My Pack",
            "summary": "A demo pack",
            "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.14"},
            "files": [
                {
                    "path": "mods/a.jar",
                    "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
                    "env": {"client": "required", "server": "required"},
                    "downloads": ["https://example.com/a.jar"],
                    "fileSize": 1024,
                },
                {
                    "path": "mods/b.jar",
                    "hashes": {"sha512": "c" * 128},
                    "env": {"client": "unsupported", "server": "required"},
                    "downloads": ["https://example.com/b.jar"],
                },
                {
                    "path": "mods/c.jar",
                    "downloads": ["https://example.com/c.jar"],
                },
            ],
        },
    )

    plan = build_pack_plan(root)

    assert isinstance(plan, ModpackPlan)
    assert plan.format_name == "mrpack"
    assert plan.pack_name == "My Pack"
    assert plan.summary == "A demo pack"
    assert plan.minecraft_version == "1.21.1"
    assert plan.loader_type == "fabric"
    assert plan.loader_version == "0.16.14"
    assert plan.overrides_dir == root / "overrides"
    assert [entry.target_relative for entry in plan.files] == ["mods/a.jar", "mods/b.jar", "mods/c.jar"]
    assert plan.files[0].sha1 == "a" * 40
    assert plan.files[0].sha512 == "b" * 128
    assert plan.files[0].size == 1024
    assert plan.files[0].env_client == "required"
    assert plan.files[1].env_client == "unsupported"
    # 未声明 env 的条目按规范默认 required
    assert plan.files[2].env_client == "required"
    assert plan.warnings == ()


def test_parse_modrinth_missing_minecraft_raises(tmp_path) -> None:
    """缺少 Minecraft 版本声明的 mrpack 应拒绝解析。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(root / "modrinth.index.json", {"dependencies": {"fabric-loader": "0.16.14"}})

    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_parse_modrinth_sanitizes_unsafe_paths(tmp_path) -> None:
    """绝对路径与目录穿越声明应被忽略并记录警告，反斜杠应归一为 POSIX 分隔。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(
        root / "modrinth.index.json",
        {
            "dependencies": {"minecraft": "1.21.1"},
            "files": [
                {"path": "../escape.jar", "downloads": ["https://example.com/x.jar"]},
                {"path": "C:\\abs\\path.jar", "downloads": ["https://example.com/x.jar"]},
                {"path": "config\\opts.txt", "downloads": ["https://example.com/x.jar"]},
            ],
        },
    )

    plan = build_pack_plan(root)

    assert [entry.target_relative for entry in plan.files] == ["config/opts.txt"]
    assert len(plan.warnings) == 2


def test_parse_modrinth_unknown_dependency_warns(tmp_path) -> None:
    """未收录的依赖键应忽略并记录警告，不得中断解析。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(
        root / "modrinth.index.json",
        {"dependencies": {"minecraft": "1.21.1", "mystery-loader": "1.0"}, "files": []},
    )

    plan = build_pack_plan(root)

    assert plan.loader_type == "vanilla"
    assert any("mystery-loader" in warning for warning in plan.warnings)


def test_parse_curseforge_plan(tmp_path) -> None:
    """CurseForge manifest 应映射加载器与 projectID/fileID 文件条目。"""
    root = tmp_path / "pack"
    root.mkdir()
    (root / "overrides").mkdir()
    _write_json(
        root / "manifest.json",
        {
            "manifestType": "minecraftModpack",
            "manifestVersion": 1,
            "name": "CF Pack",
            "version": "1.2.3",
            "overrides": "overrides",
            "minecraft": {
                "version": "1.20.1",
                "modLoaders": [{"id": "forge-47.3.0", "primary": True}],
            },
            "files": [
                {"projectID": 1, "fileID": 11, "required": True},
                {"projectID": 2, "fileID": 22, "required": False},
                {"projectID": "bad", "fileID": 33},
            ],
        },
    )

    plan = build_pack_plan(root)

    assert plan.format_name == "curseforge"
    assert plan.pack_name == "CF Pack"
    assert plan.minecraft_version == "1.20.1"
    assert plan.loader_type == "forge"
    assert plan.loader_version == "47.3.0"
    assert plan.overrides_dir == root / "overrides"
    assert len(plan.files) == 2
    assert plan.files[0].project_id == "1"
    assert plan.files[0].file_id == "11"
    assert plan.files[0].env_client == "required"
    assert plan.files[1].env_client == "optional"
    assert len(plan.warnings) == 1


def test_parse_curseforge_missing_minecraft_raises(tmp_path) -> None:
    """缺少 Minecraft 版本声明的 CurseForge 包应拒绝解析。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(root / "manifest.json", {"manifestType": "minecraftModpack", "minecraft": {}})

    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_parse_curseforge_uses_primary_loader(tmp_path) -> None:
    """仅 primary 的 modLoader 生效；无 modLoaders 时按 vanilla 处理。"""
    root_primary = tmp_path / "primary"
    root_primary.mkdir()
    _write_json(
        root_primary / "manifest.json",
        {
            "minecraft": {
                "version": "1.21.1",
                "modLoaders": [
                    {"id": "forge-1.0", "primary": False},
                    {"id": "fabric-0.16.0", "primary": True},
                ],
            },
        },
    )
    plan = build_pack_plan(root_primary)
    assert (plan.loader_type, plan.loader_version) == ("fabric", "0.16.0")

    root_vanilla = tmp_path / "vanilla"
    root_vanilla.mkdir()
    _write_json(root_vanilla / "manifest.json", {"minecraft": {"version": "1.21.1"}})
    plan = build_pack_plan(root_vanilla)
    assert (plan.loader_type, plan.loader_version) == ("vanilla", None)


def test_parse_curseforge_unexpected_manifest_type_warns(tmp_path) -> None:
    """manifestType 缺失或未知时按 CurseForge 兼容解析并记录警告。"""
    root = tmp_path / "pack"
    root.mkdir()
    _write_json(
        root / "manifest.json",
        {"manifestType": "somethingElse", "minecraft": {"version": "1.21.1"}},
    )

    plan = build_pack_plan(root)

    assert plan.format_name == "curseforge"
    assert any("somethingElse" in warning for warning in plan.warnings)


def test_build_pack_plan_rejects_unknown(tmp_path) -> None:
    """无特征文件的目录应报无法识别。"""
    root = tmp_path / "unknown"
    root.mkdir()
    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_build_pack_plan_rejects_ecl_legacy(tmp_path) -> None:
    """ECL 旧包由导入编排按旧流程处理，统一计划入口应明确拒绝。"""
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "ecl-pack.json").write_text("{}", encoding="utf-8")
    with pytest.raises(GameServiceError) as error:
        build_pack_plan(root)
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"


def test_build_pack_plan_rejects_missing_directory(tmp_path) -> None:
    """不存在的目录应拒绝解析。"""
    with pytest.raises(GameServiceError) as error:
        build_pack_plan(tmp_path / "missing")
    assert error.value.error_code == "INVALID_PACK_ARCHIVE"
