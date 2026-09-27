# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：整合包格式识别与解析：将各发行格式归一为统一的安装计划模型。
#
# 公开接口：
#   - class PackFileEntry — 整合包内单个待下载文件条目（路径/直链/哈希/环境适用性）。
#   - class ModpackPlan — 归一后的整合包安装计划（版本、加载器、文件清单、overrides）。
#   - detect_pack_format(root) -> str — 按内容特征识别整合包格式标签。
#   - build_pack_plan(root) -> ModpackPlan — 解析指定格式并输出统一安装计划。
# ============================================================

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .base import GameServiceError

# Modrinth mrpack dependencies 键 -> ECL 加载器类型；未收录键忽略并记录警告。
_modrinth_loader_keys = {
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
    "forge": "forge",
    "neoforge": "neoforge",
}

# CurseForge modLoaders[].id 前缀 -> ECL 加载器类型。
_curseforge_loader_prefixes = {
    "forge": "forge",
    "fabric": "fabric",
    "neoforge": "neoforge",
    "quilt": "quilt",
}


@dataclass(frozen=True, slots=True)
class PackFileEntry:
    """
    整合包内单个待下载文件条目。

    ``url`` 为直链；CurseForge 条目在导入编排阶段经文件详情接口解析后填充。
    ``env_client`` 表示客户端侧适用性（required/optional/unsupported）。
    """

    target_relative: str
    url: str | None = None
    sha1: str | None = None
    sha512: str | None = None
    size: int | None = None
    env_client: str = "required"
    project_id: str | None = None
    file_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModpackPlan:
    """
    归一后的整合包安装计划。

    ``minecraft_version`` 为空表示无需基础版本（ECL 旧包整体即实例）。
    ``overrides_dir`` 指向解压目录内可作为实例内容复制的根目录，无 overrides 时为 None。
    """

    format_name: str
    pack_name: str
    summary: str = ""
    minecraft_version: str = ""
    loader_type: str = "vanilla"
    loader_version: str | None = None
    files: tuple[PackFileEntry, ...] = ()
    overrides_dir: Path | None = None
    needs_reinstall_loader: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _safe_pack_relative_path(value: Any, warnings: list[str]) -> PurePosixPath | None:
    """
    校验并归一整合包内声明的相对路径，非法路径返回 None 并记录警告。

    网络来源的整合包清单视为不可信输入：拒绝绝对路径、反斜杠外的
    目录穿越（..）与空路径，统一以 "/" 分隔。
    """
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"忽略无效的文件路径声明: {value!r}")
        return None
    normalized = PurePosixPath(value.replace("\\", "/"))
    first = normalized.parts[0] if normalized.parts else ""
    # PurePosixPath 不把 "C:/..." 视为绝对路径，需额外拒绝 Windows 盘符形态。
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or not normalized.parts
        or (len(first) == 2 and first[1] == ":")
    ):
        warnings.append(f"忽略不安全的整合包文件路径: {value!r}")
        return None
    return normalized


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise GameServiceError(f"整合包描述文件读取失败: {path.name}", "INVALID_PACK_ARCHIVE") from exc
    return value if isinstance(value, dict) else {}


def _loader_from_modrinth_dependencies(dependencies: dict[str, Any], warnings: list[str]) -> tuple[str, str | None]:
    loader_type = "vanilla"
    loader_version: str | None = None
    for key, value in dependencies.items():
        if key == "minecraft":
            continue
        mapped = _modrinth_loader_keys.get(str(key).strip().casefold())
        if mapped is None:
            warnings.append(f"忽略未知的整合包依赖项: {key}")
            continue
        loader_type = mapped
        if isinstance(value, str) and value.strip():
            loader_version = value.strip()
    return loader_type, loader_version


def _parse_modrinth_plan(root: Path) -> ModpackPlan:
    # 解析 modrinth.index.json（mrpack）：文件清单含直链、哈希与客户端适用性声明。
    warnings: list[str] = []
    manifest = _read_json_file(root / "modrinth.index.json")
    dependencies = manifest.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        raise GameServiceError("整合包缺少有效的依赖声明", "INVALID_PACK_ARCHIVE")
    minecraft_version = str(dependencies.get("minecraft") or "").strip()
    if not minecraft_version:
        raise GameServiceError("整合包缺少 Minecraft 版本声明", "INVALID_PACK_ARCHIVE")
    loader_type, loader_version = _loader_from_modrinth_dependencies(dependencies, warnings)

    entries: list[PackFileEntry] = []
    raw_files = manifest.get("files") or []
    if not isinstance(raw_files, list):
        raw_files = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        relative = _safe_pack_relative_path(raw.get("path"), warnings)
        if relative is None:
            continue
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        env_client = str(env.get("client") or "required").strip().casefold()
        if env_client not in {"required", "optional", "unsupported"}:
            env_client = "required"
        hashes = raw.get("hashes") if isinstance(raw.get("hashes"), dict) else {}
        downloads = raw.get("downloads") if isinstance(raw.get("downloads"), list) else []
        url = str(downloads[0]) if downloads and isinstance(downloads[0], str) else None
        size = raw.get("fileSize")
        entries.append(
            PackFileEntry(
                target_relative=relative.as_posix(),
                url=url,
                sha1=str(hashes["sha1"]) if isinstance(hashes.get("sha1"), str) else None,
                sha512=str(hashes["sha512"]) if isinstance(hashes.get("sha512"), str) else None,
                size=int(size) if isinstance(size, int) and size >= 0 else None,
                env_client=env_client,
            )
        )
    overrides = root / "overrides"
    return ModpackPlan(
        format_name="mrpack",
        pack_name=str(manifest.get("name") or root.name),
        summary=str(manifest.get("summary") or ""),
        minecraft_version=minecraft_version,
        loader_type=loader_type,
        loader_version=loader_version,
        files=tuple(entries),
        overrides_dir=overrides if overrides.is_dir() else None,
        warnings=tuple(warnings),
    )


def _loader_from_curseforge_id(value: Any, warnings: list[str]) -> tuple[str, str | None]:
    # CurseForge 的 modLoaders[].id 形如 "forge-47.3.0"，按首个 "-" 拆分前缀与版本。
    raw = str(value or "").strip()
    if not raw:
        return "vanilla", None
    prefix, _, version = raw.partition("-")
    mapped = _curseforge_loader_prefixes.get(prefix.strip().casefold())
    if mapped is None:
        warnings.append(f"忽略未知的 CurseForge 加载器声明: {raw}")
        return "vanilla", None
    return mapped, version.strip() or None


def _parse_curseforge_plan(root: Path) -> ModpackPlan:
    # 解析 CurseForge manifest.json：文件清单仅含 projectID/fileID，
    # 直链与哈希由导入编排阶段经 CurseForge 文件详情接口解析填充。
    warnings: list[str] = []
    manifest = _read_json_file(root / "manifest.json")
    manifest_type = str(manifest.get("manifestType") or "").strip()
    if manifest_type and manifest_type.casefold() != "minecraftmodpack":
        warnings.append(f"未知的 manifestType 声明: {manifest_type}，将按 CurseForge 整合包解析")
    minecraft = manifest.get("minecraft") if isinstance(manifest.get("minecraft"), dict) else {}
    minecraft_version = str(minecraft.get("version") or "").strip()
    if not minecraft_version:
        raise GameServiceError("整合包缺少 Minecraft 版本声明", "INVALID_PACK_ARCHIVE")

    loader_type = "vanilla"
    loader_version: str | None = None
    mod_loaders = minecraft.get("modLoaders") if isinstance(minecraft.get("modLoaders"), list) else []
    for loader in mod_loaders:
        if not isinstance(loader, dict) or not loader.get("primary"):
            continue
        loader_type, loader_version = _loader_from_curseforge_id(loader.get("id"), warnings)
        break

    entries: list[PackFileEntry] = []
    raw_files = manifest.get("files") or []
    if not isinstance(raw_files, list):
        raw_files = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        project_id = raw.get("projectID")
        file_id = raw.get("fileID")
        if not isinstance(project_id, int) or not isinstance(file_id, int):
            warnings.append(f"忽略无效的 CurseForge 文件条目: {raw!r}")
            continue
        required = raw.get("required")
        entries.append(
            PackFileEntry(
                target_relative="mods/",
                url=None,
                env_client="required" if required is not False else "optional",
                project_id=str(project_id),
                file_id=str(file_id),
            )
        )
    overrides_name = str(manifest.get("overrides") or "overrides")
    overrides = root / overrides_name
    return ModpackPlan(
        format_name="curseforge",
        pack_name=str(manifest.get("name") or root.name),
        summary=str(manifest.get("description") or ""),
        minecraft_version=minecraft_version,
        loader_type=loader_type,
        loader_version=loader_version,
        files=tuple(entries),
        overrides_dir=overrides if overrides.is_dir() else None,
        warnings=tuple(warnings),
    )


def detect_pack_format(root: Path) -> str:
    """
    按内容特征识别整合包格式标签。

    :param root: 已解压的整合包内容根目录
    :return: mrpack / curseforge / mcbbs / hmcl / multimc / ecl-legacy / unknown
    """
    if (root / "modrinth.index.json").is_file():
        return "mrpack"
    if (root / "manifest.json").is_file():
        return "curseforge"
    if (root / "mcbbs.packmeta").is_file():
        return "mcbbs"
    if (root / "modpack.json").is_file():
        return "hmcl"
    if (root / "mmc-pack.json").is_file():
        return "multimc"
    if (root / "ecl-pack.json").is_file():
        return "ecl-legacy"
    return "unknown"


_supported_plan_formats = frozenset({"mrpack", "curseforge"})


def build_pack_plan(root: Path) -> ModpackPlan:
    """
    识别并解析整合包，输出统一的安装计划。

    :param root: 已解压的整合包内容根目录
    :return: 归一化的安装计划
    :raises GameServiceError: 格式无法识别、暂不支持或描述文件损坏时抛出
    """
    if not root.is_dir():
        raise GameServiceError("整合包内容目录不存在", "INVALID_PACK_ARCHIVE")
    detected = detect_pack_format(root)
    if detected == "mrpack":
        return _parse_modrinth_plan(root)
    if detected == "curseforge":
        return _parse_curseforge_plan(root)
    if detected == "ecl-legacy":
        # ECL 旧包整体即实例内容，由导入编排按旧流程处理，不走统一计划。
        raise GameServiceError("ECL 旧格式整合包由导入编排直接处理", "INVALID_PACK_ARCHIVE")
    if detected == "unknown":
        raise GameServiceError("无法识别整合包格式", "INVALID_PACK_ARCHIVE")
    raise GameServiceError(f"暂不支持该整合包格式: {detected}", "INVALID_PACK_ARCHIVE")
