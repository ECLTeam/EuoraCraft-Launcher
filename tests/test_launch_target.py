# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对快捷启动目标解析的自动化测试。
#
# 公开接口：
#   - test_candidate_roots_orders_and_dedupes(tmp_path) -> None
#   - test_candidate_roots_skips_empty_entries(tmp_path) -> None
#   - test_resolve_by_name(tmp_path) -> None
#   - test_resolve_by_name_not_found(tmp_path) -> None
#   - test_resolve_by_name_ambiguous(tmp_path) -> None
#   - test_resolve_by_directory(tmp_path) -> None
#   - test_resolve_by_directory_missing_manifest(tmp_path) -> None
#   - test_resolve_by_directory_invalid_form(tmp_path) -> None
#   - test_resolve_rejects_empty_and_relative_references(tmp_path) -> None
# ============================================================

from pathlib import Path

import pytest

from ECL.services.game.launch import candidate_roots, resolve_launch_target
from ECL.utils.errors import GameServiceError


def _make_instance(root: Path, name: str) -> Path:
    version_dir = root / "versions" / name
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / f"{name}.json").write_text("{}", encoding="utf-8")
    return root


def test_candidate_roots_orders_and_dedupes(tmp_path) -> None:
    """最近使用的游戏根应排在最前，重复条目去重且兼容字典与字符串形态。"""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    config = {
        "last_install_path": str(root_b),
        "minecraft_paths": [{"path": str(root_a)}, str(root_b)],
    }

    roots = candidate_roots(config)

    assert roots == [root_b, root_a]


def test_candidate_roots_skips_empty_entries(tmp_path) -> None:
    """空字符串与缺失字段不应产生候选根。"""
    root_a = tmp_path / "a"
    root_a.mkdir()
    config = {
        "last_install_path": "  ",
        "minecraft_paths": [{"path": ""}, {}, "   ", {"path": str(root_a)}],
    }

    roots = candidate_roots(config)

    assert roots == [root_a]


def test_resolve_by_name(tmp_path) -> None:
    """名称形态应在候选根中命中唯一实例。"""
    root = _make_instance(tmp_path / "mc", "Foo")

    game_path, version = resolve_launch_target("Foo", [root])

    assert game_path == root
    assert version == "Foo"


def test_resolve_by_name_not_found(tmp_path) -> None:
    """名称未命中时应报未找到并列出已搜索的根。"""
    with pytest.raises(GameServiceError) as exc_info:
        resolve_launch_target("Bar", [tmp_path])

    assert exc_info.value.error_code == "LAUNCH_TARGET_NOT_FOUND"
    assert str(tmp_path) in str(exc_info.value)


def test_resolve_by_name_ambiguous(tmp_path) -> None:
    """多个游戏根存在同名实例时应报冲突并列出全部候选。"""
    root_a = _make_instance(tmp_path / "a", "Foo")
    root_b = _make_instance(tmp_path / "b", "Foo")

    with pytest.raises(GameServiceError) as exc_info:
        resolve_launch_target("Foo", [root_a, root_b])

    assert exc_info.value.error_code == "LAUNCH_TARGET_AMBIGUOUS"
    assert str(root_a) in str(exc_info.value)
    assert str(root_b) in str(exc_info.value)


def test_resolve_by_directory(tmp_path) -> None:
    """目录形态应推导出游戏根与版本名，且允许名称含空格与尾部分隔符。"""
    root = _make_instance(tmp_path / "mc", "Foo Bar")
    instance_dir = root / "versions" / "Foo Bar"

    game_path, version = resolve_launch_target(str(instance_dir) + "\\", [])

    assert game_path == root.resolve()
    assert version == "Foo Bar"


def test_resolve_by_directory_missing_manifest(tmp_path) -> None:
    """目录存在但缺少版本清单时应报未找到。"""
    version_dir = tmp_path / "mc" / "versions" / "Foo"
    version_dir.mkdir(parents=True)

    with pytest.raises(GameServiceError) as exc_info:
        resolve_launch_target(str(version_dir), [])

    assert exc_info.value.error_code == "LAUNCH_TARGET_NOT_FOUND"


def test_resolve_by_directory_invalid_form(tmp_path) -> None:
    """目录形态不满足 <游戏根>/versions/<版本名> 时应报非法目标。"""
    other_dir = tmp_path / "not-versions"
    other_dir.mkdir()

    with pytest.raises(GameServiceError) as exc_info:
        resolve_launch_target(str(other_dir), [])

    assert exc_info.value.error_code == "LAUNCH_TARGET_INVALID"


def test_resolve_rejects_empty_and_relative_references(tmp_path) -> None:
    """空目标与相对引用（. / ..）应报非法目标。"""
    for bad_target in ("", "  ", ".", ".."):
        with pytest.raises(GameServiceError) as exc_info:
            resolve_launch_target(bad_target, [tmp_path])

        assert exc_info.value.error_code == "LAUNCH_TARGET_INVALID"
