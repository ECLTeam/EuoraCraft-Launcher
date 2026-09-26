# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：快捷启动目标解析：将 --launch 目标解析为游戏根与版本名。
#
# 公开接口：
#   - candidate_roots(config) -> list[Path] — 汇总候选游戏根目录（最近使用优先）。
#   - resolve_launch_target(target, roots) -> tuple[Path, str] — 解析目标为游戏根与版本名。
# ============================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ECL.utils.errors import GameServiceError


def candidate_roots(config: Mapping[str, Any]) -> list[Path]:
    """
    汇总快捷启动的候选游戏根目录。

    顺序为 ``game.last_install_path``（最近使用）在前、``game.minecraft_paths``
    在后；条目兼容 ``{"path": ...}`` 字典与纯字符串两种形态，与桥接层的
    既有解析保持一致。结果按解析后的绝对路径去重，保持首次出现顺序。

    :param config: 生效配置的 ``game`` 分区
    :return: 去重后的候选游戏根目录列表
    """
    roots: list[Path] = []
    last_install_path = str(config.get("last_install_path") or "").strip()
    if last_install_path:
        roots.append(Path(last_install_path).expanduser())
    for item in config.get("minecraft_paths") or []:
        value = item.get("path") if isinstance(item, dict) else item
        text = str(value or "").strip()
        if text:
            roots.append(Path(text).expanduser())
    unique: dict[str, Path] = {}
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        unique.setdefault(key, root)
    return list(unique.values())


def _is_path_form(target: str) -> bool:
    # 含路径分隔符或指向已存在文件系统条目的目标按目录形态解析。
    if any(separator in target for separator in ("/", "\\")):
        return True
    try:
        return Path(target).exists()
    except OSError:
        return False


def resolve_launch_target(target: str, roots: Sequence[Path]) -> tuple[Path, str]:
    """
    解析 ``--launch`` 目标为游戏根目录与版本名。

    目录形态要求指向 ``<游戏根>/versions/<版本名>``（允许尾部分隔符），
    且目录内存在同名版本清单 JSON；名称形态在候选游戏根中搜索
    ``versions/<版本名>/<版本名>.json``，命中多个游戏根时报出全部候选。

    :param target: 版本名或实例目录路径
    :param roots: 候选游戏根目录列表
    :return: (游戏根目录, 版本名)
    :raises GameServiceError: 目标为空、形态非法、未找到或同名实例冲突时抛出
    """
    cleaned = target.strip()
    if not cleaned or "\0" in cleaned:
        raise GameServiceError("快捷启动目标不能为空", "LAUNCH_TARGET_INVALID")
    if _is_path_form(cleaned):
        return _resolve_directory_target(cleaned)
    return _resolve_name_target(cleaned, roots)


def _resolve_directory_target(target: str) -> tuple[Path, str]:
    # 目录形态：从 <游戏根>/versions/<版本名> 推导游戏根与版本名。
    instance_path = Path(target).expanduser()
    resolved = instance_path.resolve()
    version_name = resolved.name
    versions_dir = resolved.parent
    if version_name == "versions" or versions_dir.name != "versions" or not resolved.is_dir():
        raise GameServiceError("实例目录需指向 <游戏根>/versions/<版本名> 目录", "LAUNCH_TARGET_INVALID")
    manifest_path = resolved / f"{version_name}.json"
    if not manifest_path.is_file():
        raise GameServiceError(f"实例 {version_name} 缺少版本清单: {manifest_path}", "LAUNCH_TARGET_NOT_FOUND")
    return versions_dir.parent, version_name


def _resolve_name_target(target: str, roots: Sequence[Path]) -> tuple[Path, str]:
    # 名称形态：在候选游戏根中搜索唯一的 versions/<名>/<名>.json。
    if target in {".", ".."} or Path(target).name != target:
        raise GameServiceError("实例名称不能包含路径分隔符，指向目录请使用实例目录路径", "LAUNCH_TARGET_INVALID")
    matches: dict[str, Path] = {}
    for root in roots:
        manifest_path = root / "versions" / target / f"{target}.json"
        if manifest_path.is_file():
            try:
                key = str(root.resolve())
            except OSError:
                key = str(root)
            matches.setdefault(key, root)
    if not matches:
        searched = "、".join(str(root) for root in roots) if roots else "尚未配置任何游戏目录"
        raise GameServiceError(
            f"未在已知游戏目录中找到实例 {target}，已搜索: {searched}",
            "LAUNCH_TARGET_NOT_FOUND",
        )
    if len(matches) > 1:
        candidates = "、".join(str(root) for root in matches.values())
        raise GameServiceError(
            f"存在多个同名实例 {target}，请改用实例目录路径指定: {candidates}",
            "LAUNCH_TARGET_AMBIGUOUS",
        )
    root = next(iter(matches.values()))
    return root, target


__all__ = ["candidate_roots", "resolve_launch_target"]
