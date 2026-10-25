# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：协调整合包读取、实例安装与按已确认文件选择导出，不修改导出源文件。
#
# 公开接口：
#   - class PackFileEntry — 整合包内单个待下载文件条目（路径/直链/哈希/环境适用性）。
#   - class ModpackPlan — 统一格式的整合包安装计划（版本、加载器、文件清单、overrides）。
#   - class ModpackFormatPolicy — 整合包格式解析策略：特征标记与加载器命名映射的唯一归属点。
#   - class ModpackCoordinator — 读取整合包、安装基础版本、下载并校验文件，最后创建实例。
#       - prepare_modpack(source, ...) — 准备安装预览与私有快照。
#       - get_modpack_preparation(preparation_id) / release_modpack_preparation(preparation_id) — 查询和释放预览。
#       - install_prepared_modpack(preparation_id, game_path, new_version_id, excluded_file_ids) — 按已确认的选择安装。
#       - preview_instance_export — 扫描实际游戏目录，返回可选文件预览操作。
#       - export_instance_pack — 按预览文件选择和整合包信息导出标准 mrpack。
#   - curseforge_fingerprint(path) — 流式计算 CurseForge 文件指纹。
#   - detect_pack_format(root) -> str — 按内容特征识别整合包格式标签。
#   - build_pack_plan(root) -> ModpackPlan — 解析指定格式并输出统一安装计划。
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from threading import Event, Thread
from types import MappingProxyType
from typing import Any, BinaryIO
from uuid import uuid4

import httpx
from pydantic import JsonValue

from ECL.services.operations import OperationContext
from ECL.utils import atomic_write_text

from .base import GameServiceError, _GameState
from .mod_sources import mod_api_base, mod_user_agent, rewrite_mod_file_url
from .modpack_export import (
    ExportContentPolicy,
    ExportFile,
    ExportMod,
    ExportSnapshot,
    OnlineExportFile,
    export_source_revision,
    scan_export_files,
)
from .modpack_preparation import PreparedModpackFile
from .resources import ResourceCatalogPolicy, _proxied_get, _proxied_post
from .workspace import ResolvedInstanceTarget, safe_extract_zip

# 在线整合包安装支持的来源。
_online_pack_sources = frozenset({"modrinth", "curseforge", "ftb"})

# 计算 CurseForge 指纹时跳过的空白字节，以及 murmur2 每次读取的字节数。
_fingerprint_skipped_bytes = frozenset({0x09, 0x0A, 0x0D, 0x20})
_fingerprint_chunk_bytes = 1024 * 1024
_murmur_m = 0x5BD1E995
_murmur_r = 24

# 匹配 version_id 中最后一个 x.y[.z] 形态的 MC 版本号；取最后一个以兼容
# "fabric-loader-0.16.14-1.21.5" 这类加载器版本号在前、MC 版本号在后的命名。
_minecraft_version_pattern = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")


class ModpackFormatPolicy:
    """
    整合包格式解析策略：各发行格式的特征与加载器命名到 ECL 领域模型的映射。

    作为格式解析全部映射表的唯一归属点（规范禁止模块级脱类映射常量），
    所有容器均为不可变对象；解析函数按需引用，键集变更时同步更新对应解析器。
    """

    # 格式特征文件 -> 格式标签，按序判定；兼容压缩包内单层目录包裹的发行包。
    format_markers = (
        ("modrinth.index.json", "mrpack"),
        ("manifest.json", "curseforge"),
        ("mcbbs.packmeta", "mcbbs"),
        ("modpack.json", "hmcl"),
        ("mmc-pack.json", "multimc"),
        ("ecl-pack.json", "ecl-legacy"),
    )

    # Modrinth mrpack dependencies 键 -> ECL 加载器类型；未收录键忽略并记录警告。
    modrinth_loader_keys = MappingProxyType(
        {
            "fabric-loader": "fabric",
            "quilt-loader": "quilt",
            "forge": "forge",
            "neoforge": "neoforge",
        }
    )

    # CurseForge modLoaders[].id 前缀 -> ECL 加载器类型。
    curseforge_loader_prefixes = MappingProxyType(
        {
            "forge": "forge",
            "fabric": "fabric",
            "neoforge": "neoforge",
            "quilt": "quilt",
        }
    )

    # MCBBS 附加组件与 MultiMC 组件 uid -> ECL 加载器类型（HMCL 组件 uid 体系）。
    component_loader_uids = MappingProxyType(
        {
            "net.minecraftforge": "forge",
            "net.fabricmc.fabric-loader": "fabric",
            "net.neoforged": "neoforge",
            "org.quiltmc.quilt-loader": "quilt",
        }
    )

    # FTB 版本清单 modloader 目标名 -> ECL 加载器类型。
    ftb_loader_names = MappingProxyType(
        {
            "forge": "forge",
            "neoforge": "neoforge",
            "fabric": "fabric",
            "quilt": "quilt",
        }
    )


@dataclass(frozen=True, slots=True)
class PackFileEntry:
    """
    整合包内单个待下载文件条目。

    ``url`` 为文件直链；CurseForge 条目在导入时通过文件详情接口获取该地址。
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
    统一格式的整合包安装计划。

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
    检查整合包内的相对路径并统一分隔符；路径无效时返回 None 并记录警告。

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
        mapped = ModpackFormatPolicy.modrinth_loader_keys.get(str(key).strip().casefold())
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
    mapped = ModpackFormatPolicy.curseforge_loader_prefixes.get(prefix.strip().casefold())
    if mapped is None:
        warnings.append(f"忽略未知的 CurseForge 加载器声明: {raw}")
        return "vanilla", None
    return mapped, version.strip() or None


def _parse_curseforge_plan(root: Path) -> ModpackPlan:
    # 解析 CurseForge manifest.json：文件清单仅含 projectID/fileID，
    # 导入时从 CurseForge 文件详情接口取得下载地址和哈希。
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


def _detect_pack_layout(root: Path) -> tuple[Path, str]:
    """
    按清单特征识别格式，返回 (清单所在目录, 格式标签)。

    兼容压缩包内单层目录包裹的发行包（MultiMC/HMCL 导出常见）：根目录
    无法命中特征时，若恰有一个子目录且其内命中特征，则以该子目录为准。
    """
    if not root.is_dir():
        return root, "unknown"
    for marker, tag in ModpackFormatPolicy.format_markers:
        if (root / marker).is_file():
            return root, tag
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return root, "unknown"
    if len(children) == 1:
        nested = children[0]
        for marker, tag in ModpackFormatPolicy.format_markers:
            if (nested / marker).is_file():
                return nested, tag
    return root, "unknown"


def detect_pack_format(root: Path) -> str:
    """
    按内容特征识别整合包格式标签。

    :param root: 已解压的整合包内容根目录
    :return: mrpack / curseforge / mcbbs / hmcl / multimc / ecl-legacy / unknown
    """
    return _detect_pack_layout(root)[1]


def _parse_mcbbs_plan(root: Path) -> ModpackPlan:
    # 解析 mcbbs.packmeta：版本与加载器来自 addons（HMCL 组件 uid 体系），
    # 文件清单仅支持 CurseForge 条目（projectID/fileID），其余类型记录警告跳过。
    warnings: list[str] = []
    manifest = _read_json_file(root / "mcbbs.packmeta")
    addons = manifest.get("addons") if isinstance(manifest.get("addons"), list) else []
    minecraft_version = ""
    loader_type = "vanilla"
    loader_version: str | None = None
    for addon in addons:
        if not isinstance(addon, dict):
            continue
        addon_id = str(addon.get("id") or "").strip()
        addon_version = str(addon.get("version") or "").strip()
        if addon_id == "net.minecraft":
            minecraft_version = addon_version
        elif addon_id in ModpackFormatPolicy.component_loader_uids:
            loader_type = ModpackFormatPolicy.component_loader_uids[addon_id]
            loader_version = addon_version or None
        elif addon_id and addon_id != "org.lwjgl3":
            warnings.append(f"忽略 MCBBS 附加组件: {addon_id}")
    if not minecraft_version:
        raise GameServiceError("MCBBS 整合包缺少 Minecraft 组件声明", "INVALID_PACK_ARCHIVE")
    entries: list[PackFileEntry] = []
    raw_files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        project_id = raw.get("projectID")
        file_id = raw.get("fileID")
        if not isinstance(project_id, int) or not isinstance(file_id, int):
            warnings.append(f"忽略暂不支持的 MCBBS 文件条目: {raw!r}")
            continue
        entries.append(
            PackFileEntry(
                target_relative="mods/",
                # force 控制更新时是否覆盖用户修改，不表示安装可选性。
                env_client="required",
                project_id=str(project_id),
                file_id=str(file_id),
            )
        )
    overrides = root / "overrides"
    return ModpackPlan(
        format_name="mcbbs",
        pack_name=str(manifest.get("name") or root.name),
        summary=str(manifest.get("description") or ""),
        minecraft_version=minecraft_version,
        loader_type=loader_type,
        loader_version=loader_version,
        files=tuple(entries),
        overrides_dir=overrides if overrides.is_dir() else None,
        warnings=tuple(warnings),
    )


def _parse_hmcl_plan(root: Path) -> ModpackPlan:
    # 解析 HMCL 自有格式 modpack.json：仅声明 gameVersion 与 minecraft/ 覆盖目录，
    # 不含加载器信息，与 PCL 的解析行为保持一致（按原版基础版本安装）。
    manifest = _read_json_file(root / "modpack.json")
    minecraft_version = str(manifest.get("gameVersion") or "").strip()
    if not minecraft_version:
        raise GameServiceError("HMCL 整合包缺少 gameVersion 声明", "INVALID_PACK_ARCHIVE")
    overrides = root / "minecraft"
    return ModpackPlan(
        format_name="hmcl",
        pack_name=str(manifest.get("name") or root.name),
        summary=str(manifest.get("description") or ""),
        minecraft_version=minecraft_version,
        files=(),
        overrides_dir=overrides if overrides.is_dir() else None,
    )


def _parse_multimc_plan(root: Path) -> ModpackPlan:
    # 解析 MultiMC mmc-pack.json：从 components 提取 Minecraft 与加载器版本，
    # 不移植其 patch 合并引擎，改为按解析结果重装基础版本（needs_reinstall_loader）。
    warnings: list[str] = []
    manifest = _read_json_file(root / "mmc-pack.json")
    components = manifest.get("components") if isinstance(manifest.get("components"), list) else []
    minecraft_version = ""
    loader_type = "vanilla"
    loader_version: str | None = None
    for component in components:
        if not isinstance(component, dict):
            continue
        uid = str(component.get("uid") or "").strip()
        version = component.get("version")
        version_text = str(version).strip() if isinstance(version, str) else ""
        if uid == "net.minecraft":
            minecraft_version = version_text
        elif uid in ModpackFormatPolicy.component_loader_uids:
            loader_type = ModpackFormatPolicy.component_loader_uids[uid]
            loader_version = version_text or None
        elif uid and uid != "org.lwjgl3":
            warnings.append(f"忽略 MultiMC 组件: {uid}")
    if not minecraft_version:
        raise GameServiceError("MultiMC 整合包缺少 Minecraft 组件版本", "INVALID_PACK_ARCHIVE")
    overrides = next((root / name for name in ("minecraft", ".minecraft") if (root / name).is_dir()), None)
    instance_cfg = root / "instance.cfg"
    pack_name = root.name
    if instance_cfg.is_file():
        for line in instance_cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "name" and value.strip():
                pack_name = value.strip()
                break
    return ModpackPlan(
        format_name="multimc",
        pack_name=pack_name,
        minecraft_version=minecraft_version,
        loader_type=loader_type,
        loader_version=loader_version,
        overrides_dir=overrides,
        needs_reinstall_loader=True,
        warnings=tuple(warnings),
    )


_supported_plan_formats = frozenset({"mrpack", "curseforge", "mcbbs", "hmcl", "multimc"})


def build_pack_plan(root: Path) -> ModpackPlan:
    """
    识别并解析整合包，输出统一的安装计划。

    :param root: 已解压的整合包内容根目录
    :return: 统一格式的安装计划
    :raises GameServiceError: 格式无法识别、暂不支持或描述文件损坏时抛出
    """
    if not root.is_dir():
        raise GameServiceError("整合包内容目录不存在", "INVALID_PACK_ARCHIVE")
    pack_root, detected = _detect_pack_layout(root)
    if detected == "mrpack":
        return _parse_modrinth_plan(pack_root)
    if detected == "curseforge":
        return _parse_curseforge_plan(pack_root)
    if detected == "mcbbs":
        return _parse_mcbbs_plan(pack_root)
    if detected == "hmcl":
        return _parse_hmcl_plan(pack_root)
    if detected == "multimc":
        return _parse_multimc_plan(pack_root)
    if detected == "ecl-legacy":
        # ECL 旧格式包直接包含实例文件，按旧导入流程处理。
        raise GameServiceError("ECL 旧格式整合包由导入编排直接处理", "INVALID_PACK_ARCHIVE")
    raise GameServiceError("无法识别整合包格式", "INVALID_PACK_ARCHIVE")


def _file_digest(path: Path, algorithm: str) -> str:
    # 流式计算文件摘要，避免大文件一次性载入内存。
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _murmur2_absorb(h: int, data: bytes) -> tuple[int, bytes]:
    # 吸收 4 字节整块，返回 (新状态, 不足一块的尾部字节)。
    i = 0
    aligned = len(data) - len(data) % 4
    while i < aligned:
        k = int.from_bytes(data[i : i + 4], "little")
        k = (k * _murmur_m) & 0xFFFFFFFF
        k ^= k >> _murmur_r
        k = (k * _murmur_m) & 0xFFFFFFFF
        h = (h * _murmur_m) & 0xFFFFFFFF
        h ^= k
        i += 4
    return h, data[i:]


def _murmur2_tail(h: int, tail: bytes) -> int:
    # MurmurHash2 的尾部和最终混合按 SMHasher 参考实现处理，包括 switch 的连续执行。
    if len(tail) >= 3:
        h ^= tail[2] << 16
    if len(tail) >= 2:
        h ^= tail[1] << 8
    if len(tail) >= 1:
        h ^= tail[0]
        h = (h * _murmur_m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * _murmur_m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def _curseforge_fingerprint_stream(stream: BinaryIO, context: OperationContext | None) -> int:
    """
    在同一个已打开文件上执行两遍有界读取，后台导出逐块响应取消。

    CurseForge 的种子包含过滤空白后的总长度，所以不能在未知长度时直接
    完成哈希；重新定位同一描述符，避免两次打开路径之间被替换成界外链接。
    """
    filtered_length = 0
    stream.seek(0)
    while chunk := stream.read(_fingerprint_chunk_bytes):
        if context is not None:
            context.check_cancelled()
        filtered_length += sum(1 for byte in chunk if byte not in _fingerprint_skipped_bytes)
    h = (1 ^ filtered_length) & 0xFFFFFFFF
    carry = b""
    stream.seek(0)
    while chunk := stream.read(_fingerprint_chunk_bytes):
        if context is not None:
            context.check_cancelled()
        filtered = bytes(byte for byte in chunk if byte not in _fingerprint_skipped_bytes)
        if carry:
            filtered = carry + filtered
        h, carry = _murmur2_absorb(h, filtered)
    if carry:
        h = _murmur2_tail(h, carry)
    return h


def curseforge_fingerprint(path: Path) -> int:
    """
    流式计算 CurseForge 文件指纹，跳过协议指定的空白字节。

    :param path: 调用方已确认的本地文件
    :return: MurmurHash2 32 位无符号指纹
    """
    with path.open("rb") as stream:
        return _curseforge_fingerprint_stream(stream, None)


def _extract_minecraft_version(version_id: str) -> str:
    matches = list(_minecraft_version_pattern.finditer(version_id))
    return matches[-1].group(0) if matches else version_id


def _resolve_export_dependencies(target: ResolvedInstanceTarget) -> dict[str, str]:
    """
    从实例版本清单解析 Modrinth 依赖声明。

    实例名由用户或安装参数决定，无法可靠推断 MC 版本与加载器；优先读取版本
    JSON 的 ``inheritsFrom`` 与依赖库坐标，读取失败时明确拒绝导出。
    Forge 的库坐标形如 ``1.20.1-47.2.0``，需要去掉 MC 版本前缀。

    :param target: 已解析的实例目标
    :return: 形如 ``{"minecraft": "1.20.1", "fabric-loader": "0.17.0"}`` 的依赖映射
    """
    manifest_path = target.instance_path / f"{target.instance_directory_name}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise GameServiceError("无法读取实例启动描述", "EXPORT_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise GameServiceError("实例启动描述不是对象", "EXPORT_MANIFEST_INVALID")

    dependencies: dict[str, str] = {}
    minecraft = str(manifest.get("inheritsFrom") or "").strip()
    if not minecraft:
        raw_id = str(manifest.get("id") or "").strip()
        minecraft = _extract_minecraft_version(raw_id) if raw_id else ""
    if minecraft:
        dependencies["minecraft"] = minecraft

    loader_prefixes = {
        "fabric-loader": "net.fabricmc:fabric-loader:",
        "quilt-loader": "org.quiltmc:quilt-loader:",
        "neoforge": "net.neoforged:neoforge:",
        "forge": "net.minecraftforge:forge:",
    }
    libraries = manifest.get("libraries", [])
    if not isinstance(libraries, list) or any(not isinstance(library, dict) for library in libraries):
        raise GameServiceError("实例依赖列表无效", "EXPORT_MANIFEST_INVALID")
    for library in libraries:
        coordinate = str(library.get("name") or "")
        for key, prefix in loader_prefixes.items():
            if not coordinate.startswith(prefix):
                continue
            version = coordinate[len(prefix) :]
            if key == "forge" and "-" in version:
                version = version.rsplit("-", 1)[-1]
            dependencies[key] = version
            break
    return dependencies


def build_ftb_plan(info: dict[str, Any], manifest: dict[str, Any]) -> ModpackPlan:
    """
    将 FTB 版本清单转换为启动器使用的安装计划。

    FTB 清单不打包压缩包，而是逐文件声明下载地址与哈希；``targets`` 声明
    游戏与加载器版本，``serveronly`` 文件在客户端导入时跳过。

    :param info: 整合包详情响应（提供包名与简介）
    :param manifest: 版本清单响应（targets 与 files）
    :return: 统一格式的安装计划
    :raises GameServiceError: 清单缺少 Minecraft 版本声明时抛出
    """
    warnings: list[str] = []
    minecraft_version, loader_type, loader_version = _extract_ftb_targets(manifest, warnings)
    if not minecraft_version:
        raise GameServiceError("FTB 整合包清单缺少 Minecraft 版本", "INVALID_PACK_ARCHIVE")
    entries: list[PackFileEntry] = []
    raw_files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        entry = _ftb_file_entry(raw, warnings)
        if entry is not None:
            entries.append(entry)
    return ModpackPlan(
        format_name="ftb",
        pack_name=str(info.get("name") or manifest.get("name") or "FTB Modpack"),
        summary=str(info.get("synopsis") or ""),
        minecraft_version=minecraft_version,
        loader_type=loader_type,
        loader_version=loader_version,
        files=tuple(entries),
        warnings=tuple(warnings),
    )


def _extract_ftb_targets(manifest: dict[str, Any], warnings: list[str]) -> tuple[str, str, str | None]:
    # 从 targets 提取 Minecraft 版本与加载器（类型+版本），未知组件记录警告。
    minecraft_version = ""
    loader_type = "vanilla"
    loader_version: str | None = None
    targets = manifest.get("targets") if isinstance(manifest.get("targets"), list) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        name = str(target.get("name") or "").strip().casefold()
        version = str(target.get("version") or "").strip()
        if name == "minecraft" or target.get("type") == "game":
            minecraft_version = minecraft_version or version
        elif target.get("type") == "modloader" and name in ModpackFormatPolicy.ftb_loader_names:
            loader_type = ModpackFormatPolicy.ftb_loader_names[name]
            loader_version = version or None
        elif name not in {"java"}:
            warnings.append(f"忽略 FTB 目标组件: {name}")
    return minecraft_version, loader_type, loader_version


def _ftb_file_entry(raw: dict[str, Any], warnings: list[str]) -> PackFileEntry | None:
    # 把单个 FTB 文件声明转换为下载条目；路径不安全、缺地址或仅服务端时返回 None。
    relative = _safe_pack_relative_path(str(raw.get("path") or ""), warnings)
    if relative is None:
        return None
    if not raw.get("url"):
        warnings.append(f"忽略缺少下载地址的 FTB 文件: {raw.get('name') or raw.get('path')}")
        return None
    if raw.get("serveronly"):
        env_client = "unsupported"
    elif raw.get("optional"):
        env_client = "optional"
    else:
        env_client = "required"
    return PackFileEntry(
        target_relative=relative.as_posix(),
        url=str(raw["url"]),
        sha1=str(raw["sha1"]) if raw.get("sha1") else None,
        size=int(raw["size"]) if isinstance(raw.get("size"), int) and raw["size"] >= 0 else None,
        env_client=env_client,
    )


class ModpackCoordinator(_GameState):
    """
    导入整合包：解压文件、安装基础版本、下载文件并创建实例。
    """

    def prepare_modpack(
        self, source: str, source_path: Path | None = None, project_id: str = "", file_id: str = ""
    ) -> dict[str, str]:
        """
        创建进程内整合包快照并提交可取消的预览准备任务，不安装游戏或模组。

        :param source: local、modrinth、curseforge 或 ftb
        :param source_path: 本地压缩包，仅 local 使用
        :param project_id: 在线项目标识
        :param file_id: 在线包版本或文件标识
        :return: preparationId 与后台 operationId
        :raises GameServiceError: 来源、文件或在线标识无效
        """
        if source not in {"local", *_online_pack_sources}:
            raise GameServiceError("不支持的整合包来源", "INVALID_RESOURCE_SOURCE")
        if source == "local":
            if source_path is None or not source_path.is_file():
                raise GameServiceError("请选择可读取的整合包文件", "INVALID_PACK_ARCHIVE")
        elif not project_id or not file_id:
            raise GameServiceError("整合包项目或文件标识缺失", "PACK_ONLINE_FILE_INVALID")
        record = self._modpack_preparations.create(source)

        def worker(context: OperationContext) -> dict[str, str]:
            prepared = False
            try:
                self._modpack_preparations.begin(record, context.cancel_event)
                context.check_cancelled()
                context.progress(2, "正在读取整合包")
                legacy_root = None
                if source == "ftb":
                    plan = self._fetch_ftb_plan(project_id, file_id)
                else:
                    archive = record.directory / "source.zip"
                    if source == "local":
                        assert source_path is not None
                        # 分块复制私有快照，取消后不再读取用户的来源文件。
                        with source_path.open("rb") as reader, archive.open("wb") as writer:
                            while chunk := reader.read(1024 * 1024):
                                context.check_cancelled()
                                writer.write(chunk)
                    else:
                        selected = self._select_online_file(source, project_id, file_id)
                        entry = PackFileEntry(target_relative="source.zip", url=str(selected["url"]))
                        self._download_pack_files([entry], record.directory, context, 3, 45)
                    context.check_cancelled()
                    extracted = record.directory / "contents"
                    safe_extract_zip(archive, extracted)
                    context.check_cancelled()
                    layout_root, detected = _detect_pack_layout(extracted)
                    if detected == "ecl-legacy":
                        legacy_root = layout_root
                        plan = ModpackPlan(format_name=detected, pack_name=source_path.stem if source_path else "ECL")
                    else:
                        plan = build_pack_plan(extracted)
                # 在确认前解析引用，只读取文件详情，不下载清单中的文件内容。
                active_plan = replace(
                    plan, files=tuple(item for item in plan.files if item.env_client != "unsupported")
                )
                resolved, _ = self._resolve_pack_entries(active_plan, context)
                active_entries = iter(resolved)
                entries = tuple(
                    entry if entry.env_client == "unsupported" else next(active_entries) for entry in plan.files
                )
                context.check_cancelled()
                plan = replace(plan, files=entries)
                files = self._prepared_file_entries(entries)
                self._modpack_preparations.complete(record, plan, files, legacy_root)
                prepared = True
                context.progress(100, "整合包安装信息已就绪")
                return {"preparationId": record.preparation_id}
            finally:
                self._modpack_preparations.finish_worker(record, prepared=prepared)

        try:
            operation = self._application_operations.submit("modpack_prepare", worker)
        except Exception:
            self._modpack_preparations.release(record.preparation_id)
            raise
        return {**operation, "preparationId": record.preparation_id}

    @staticmethod
    def _prepared_file_entries(entries: tuple[PackFileEntry, ...]) -> tuple[PreparedModpackFile, ...]:
        """
        确认文件目标在所有平台下均无冲突，并生成当前预览固定身份。

        路径与父目录冲突在准备时拒绝，避免安装到一半才发现文件覆盖目录。
        """
        paths: set[str] = set()
        for entry in entries:
            relative = _safe_pack_relative_path(entry.target_relative, [])
            key = relative.as_posix().casefold() if relative is not None else ""
            parents = set(PurePosixPath(key).parents)
            if not key or key in paths or any(PurePosixPath(path) in parents for path in paths):
                raise GameServiceError("整合包清单存在无效或重复目标路径", "PACK_FILE_PATH_CONFLICT")
            if any(PurePosixPath(key) in PurePosixPath(path).parents for path in paths):
                raise GameServiceError("整合包文件与目录冲突", "PACK_FILE_PATH_CONFLICT")
            paths.add(key)
        return tuple(PreparedModpackFile(uuid4().hex, entry) for entry in entries)

    def get_modpack_preparation(self, preparation_id: str) -> dict[str, object]:
        """
        查询当前进程的安全安装预览。

        :param preparation_id: 准备任务返回的标识
        :return: 状态与预览
        :raises GameServiceError: 预览失效
        """
        return self._modpack_preparations.get(preparation_id)

    def release_modpack_preparation(self, preparation_id: str) -> bool:
        """
        释放界面持有的预览，不取消已经提交的安装。

        :param preparation_id: 要释放的标识
        :return: 是否接受释放
        """
        return self._modpack_preparations.release(preparation_id)

    def install_prepared_modpack(
        self, preparation_id: str, game_path: Path, new_version_id: str, excluded_file_ids: list[str]
    ) -> dict[str, str]:
        """
        根据已经展示的快照和可选文件选择创建新实例。

        :param preparation_id: 已准备的快照标识
        :param game_path: 目标游戏根目录
        :param new_version_id: 新实例名，不允许覆盖
        :param excluded_file_ids: 明确排除的 optional 文件标识
        :return: 安装操作句柄
        :raises GameServiceError: 快照、选择或目标无效
        """
        target = self.resolve_instance(game_path, new_version_id)
        record = self._modpack_preparations.claim(preparation_id, excluded_file_ids, target.instance_path)
        excluded = set(excluded_file_ids)

        def worker(context: OperationContext) -> dict[str, Any]:
            try:
                self._modpack_preparations.begin(record, context.cancel_event)
                context.check_cancelled()
                target.instance_path.parent.mkdir(parents=True, exist_ok=True)
                if record.legacy_root is not None:
                    return self._import_legacy_ecl_pack(record.legacy_root, target, context)
                assert record.plan is not None
                ignored_paths = frozenset(
                    item.entry.target_relative.casefold()
                    for item in record.files
                    if item.file_id in excluded or item.entry.env_client == "unsupported"
                )
                plan = replace(
                    record.plan,
                    files=tuple(
                        item.entry
                        for item in record.files
                        if item.file_id not in excluded and item.entry.env_client != "unsupported"
                    ),
                )
                result = self._install_plan_into_instance(plan, target, context, ignored_paths)
                result["excludedOptionalFiles"] = len(excluded)
                result["unsupportedFiles"] = sum(item.entry.env_client == "unsupported" for item in record.files)
                return result
            finally:
                self._modpack_preparations.finish_worker(record)

        kind = "instance_import" if record.source == "local" else "modpack_online_install"
        try:
            return self._application_operations.submit(kind, worker)
        except Exception:
            self._modpack_preparations.rollback_claim(record)
            raise

    def _fetch_ftb_plan(self, project_id: str, version_id: str) -> ModpackPlan:
        """
        读取 FTB 整合包详情和版本清单，转换为启动器使用的安装计划。

        :param project_id: FTB 整合包 ID
        :param version_id: FTB 版本 ID
        :return: 统一格式的安装计划
        :raises GameServiceError: FTB 在线源不可用或清单无效时抛出
        """
        headers = {"User-Agent": "EuoraCraft-Launcher/resource-workspace"}
        base = ResourceCatalogPolicy.ftb_base_url
        try:
            info_response = _proxied_get(f"{base}/modpack/{project_id}", headers=headers, timeout=15)
            info_response.raise_for_status()
            manifest_response = _proxied_get(f"{base}/modpack/{project_id}/{version_id}", headers=headers, timeout=15)
            manifest_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GameServiceError(f"FTB 在线源不可用：{exc}", "FTB_SOURCE_UNAVAILABLE") from exc
        info = info_response.json()
        manifest = manifest_response.json()
        if not isinstance(manifest, dict) or manifest.get("status") != "success":
            raise GameServiceError("FTB 整合包清单无效", "FTB_SOURCE_UNAVAILABLE")
        return build_ftb_plan(info if isinstance(info, dict) else {}, manifest)

    def _import_legacy_ecl_pack(
        self, extracted: Path, target: ResolvedInstanceTarget, context: OperationContext
    ) -> dict[str, Any]:
        """
        按 ECL 旧格式流程导入：包内容整体即实例，主版本描述重命名为新实例名。

        保留历史行为：不下载文件、不校验基础版本。
        """
        staging = target.instance_path.with_name(f".{target.instance_directory_name}.ecl-import")
        try:
            shutil.copytree(extracted, staging, ignore=shutil.ignore_patterns("ecl-pack.json"))
            manifests = list(staging.glob("*.json"))
            original = next((path for path in manifests if path.name != "ecl-pack.json"), None)
            if original and original.name != f"{target.instance_directory_name}.json":
                original.rename(staging / f"{target.instance_directory_name}.json")
            context.check_cancelled()
            context.finalize(lambda: staging.replace(target.instance_path))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {
            "versionId": target.instance_directory_name,
            "path": str(target.instance_path),
            "format": "ecl-legacy",
            "downloadedFiles": 0,
            "unsupportedFiles": 0,
            "excludedOptionalFiles": 0,
            "overridesFiles": 0,
            "baseVersion": None,
            "warnings": [],
        }

    def _install_plan_into_instance(
        self,
        plan: ModpackPlan,
        target: ResolvedInstanceTarget,
        context: OperationContext,
        ignored_paths: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """
        按安装计划创建实例：读取文件清单、安装基础版本、下载并校验文件，最后一次性保存。

        全程在 staging 目录进行，任何失败都会清理 staging，不影响既有实例。
        """
        if not plan.minecraft_version:
            raise GameServiceError("整合包缺少 Minecraft 版本声明", "INVALID_PACK_ARCHIVE")
        staging = target.instance_path.with_name(f".{target.instance_directory_name}.ecl-import")
        try:
            entries, skipped = self._resolve_pack_entries(plan, context)
            base_name = self._ensure_pack_base_version(plan, target, context)
            context.progress(32, f"正在下载 {len(entries)} 个整合包文件")
            self._download_pack_files(entries, staging, context, 32, 72)
            context.progress(74, "正在校验下载文件")
            self._verify_pack_files(entries, staging, context)
            context.progress(78, "正在复制整合包实例内容")
            overrides_count = self._copy_pack_overrides(plan, staging, context, ignored_paths)
            self._write_pack_version_json(base_name, target, staging)
            context.check_cancelled()
            context.finalize(lambda: staging.replace(target.instance_path))
            return {
                "versionId": target.instance_directory_name,
                "path": str(target.instance_path),
                "format": plan.format_name,
                "downloadedFiles": len(entries),
                "unsupportedFiles": skipped,
                "excludedOptionalFiles": 0,
                "overridesFiles": overrides_count,
                "baseVersion": base_name,
                "warnings": list(plan.warnings),
            }
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _resolve_pack_entries(self, plan: ModpackPlan, context: OperationContext) -> tuple[list[PackFileEntry], int]:
        """
        过滤客户端不适用的条目，并把 CurseForge 条目解析为可下载的直链。

        CurseForge 条目本身不含直链，逐条调用文件详情接口（必要时回退专用下载
        地址接口）填充 URL 与哈希；未配置 API Key 时直接报错。

        :return: 待下载条目列表与环境不适用而跳过的数量
        """
        entries: list[PackFileEntry] = []
        skipped = 0
        total = len(plan.files)
        for index, entry in enumerate(plan.files, 1):
            context.check_cancelled()
            if entry.env_client == "unsupported":
                skipped += 1
                continue
            resolved = entry
            if entry.project_id is not None and entry.file_id is not None and not entry.url:
                # _fetch_curseforge_file 来自 ResourceCoordinator，经 GameService 聚合后可用。
                details = self._fetch_curseforge_file(str(entry.project_id), str(entry.file_id))
                relative = _safe_pack_relative_path(f"mods/{details['filename']}", [])
                if relative is None:
                    raise GameServiceError(
                        f"CurseForge 文件名不安全: {details['filename']}", "PACK_ONLINE_FILE_INVALID"
                    )
                hashes = details.get("hashes") or {}
                resolved = replace(
                    entry,
                    url=str(details["url"]),
                    target_relative=relative.as_posix(),
                    sha1=hashes.get("sha1"),
                    sha512=hashes.get("sha512"),
                    size=details.get("size") if isinstance(details.get("size"), int) else entry.size,
                )
                context.progress(8 + 10 * index / max(total, 1), f"正在解析整合包文件 {index}/{total}")
            elif not entry.url:
                raise GameServiceError(f"整合包文件缺少下载地址: {entry.target_relative}", "PACK_ONLINE_FILE_INVALID")
            entries.append(resolved)
        return entries, skipped

    def _ensure_pack_base_version(
        self, plan: ModpackPlan, target: ResolvedInstanceTarget, context: OperationContext
    ) -> str:
        """
        保障整合包依赖的基础版本与加载器就绪，返回实例应继承的基础版本名。

        缺失时经同步安装核心自动安装；未声明加载器版本时使用安装协调器解析
        当前游戏版本下的最新版本。安装不附带 Fabric API，避免与整合包自带
        的 API 模组重复。

        :raises GameServiceError: 加载器版本无法解析或安装后基础版本仍缺失时抛出
        """
        mc_version = plan.minecraft_version
        base_name = mc_version
        loader_version = self._resolve_install_loader_version(
            plan.loader_type, mc_version, plan.loader_version, "official", context.cancel_event
        )
        if plan.loader_type != "vanilla":
            base_name = f"{plan.loader_type}-{loader_version}-{mc_version}"
        base_json = target.instance_path.parent / base_name / f"{base_name}.json"
        if base_json.is_file():
            context.progress(30, f"基础版本 {base_name} 已就绪")
            return base_name
        context.progress(20, f"正在自动安装基础版本 {base_name}")

        def report(phase: str, message: str, **_details: Any) -> None:
            context.progress(26, f"安装基础版本：{message}")

        java_path = self._resolve_java_path(None) if plan.loader_type in {"forge", "neoforge"} else None
        self.install_blocking(
            task_id=f"pack-base-{context.operation_id[:8]}",
            version_id=mc_version,
            save_name=base_name,
            loader=plan.loader_type,
            loader_version=loader_version,
            fabric_api_version=None,
            game_path=target.minecraft_root_path,
            source="official",
            java_path=java_path,
            report=report,
            cancel_event=context.cancel_event,
            include_fabric_api=False,
        )
        if not base_json.is_file():
            raise GameServiceError(f"基础版本 {base_name} 安装失败", "PACK_BASE_VERSION_MISSING")
        context.progress(30, f"基础版本 {base_name} 安装完成")
        return base_name

    def _download_pack_files(
        self,
        entries: list[PackFileEntry],
        staging: Path,
        context: OperationContext,
        progress_start: float,
        progress_end: float,
    ) -> None:
        """
        用共享下载引擎并发下载整合包文件到 staging 目录。

        下载器登记到活跃下载表以便启动器关闭时中止；取消事件由守护线程
        监听并停止下载器。
        """
        if not entries:
            return
        download_list: list[tuple[str, Path]] = []
        for entry in entries:
            destination = staging.joinpath(*entry.target_relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            download_list.append((str(entry.url), destination))
        span = max(progress_end - progress_start, 0.0)

        def emit_progress(done: int, total: int) -> None:
            if total > 0:
                context.progress(progress_start + done / total * span, "正在下载整合包文件")

        downloader = self._downloader_factory(download_list, progress_callback=emit_progress)
        with self._lock:
            self._active_downloads[context.operation_id] = downloader
        finished = Event()
        watcher = Thread(
            target=self._watch_downloader_cancel,
            args=(context.cancel_event, finished, downloader),
            name=f"ECLPackCancel-{context.operation_id[:8]}",
            daemon=True,
        )
        watcher.start()
        try:
            self._run_downloader_blocking(downloader, None)
            # 下载器不会因单个条目失败而抛错；未声明哈希的条目没有后续校验兜底，
            # 必须在这里确认，否则缺文件的实例会被当成创建成功。
            if context.cancel_event.is_set():
                raise GameServiceError("整合包文件下载已取消", "PACK_DOWNLOAD_CANCELLED")
            if downloader.failed_entries:
                raise GameServiceError(
                    f"有 {len(downloader.failed_entries)} 个整合包文件下载失败，请检查网络后重试",
                    "PACK_FILE_DOWNLOAD_FAILED",
                )
        finally:
            finished.set()
            with self._lock:
                if self._active_downloads.get(context.operation_id) is downloader:
                    self._active_downloads.pop(context.operation_id, None)
            watcher.join(timeout=2)

    def _verify_pack_files(self, entries: list[PackFileEntry], staging: Path, context: OperationContext) -> None:
        """
        校验下载文件的 SHA-1/SHA-512，失败集合整体重试一轮后仍失败则报错。

        :raises GameServiceError: 重试后仍有哈希不匹配（PACK_FILE_HASH_MISMATCH）时抛出
        """
        failed = self._collect_hash_mismatches(entries, staging)
        if not failed:
            return
        retry_entries = [entry for entry in entries if entry.target_relative in failed]
        for relative in failed:
            staging.joinpath(*relative.split("/")).unlink(missing_ok=True)
        self._download_pack_files(retry_entries, staging, context, 74, 76)
        remaining = self._collect_hash_mismatches(retry_entries, staging)
        if remaining:
            raise GameServiceError(
                f"有 {len(remaining)} 个整合包文件校验失败，例如 {remaining[0]}",
                "PACK_FILE_HASH_MISMATCH",
            )

    @staticmethod
    def _collect_hash_mismatches(entries: list[PackFileEntry], staging: Path) -> list[str]:
        # 只校验声明了哈希的条目；文件缺失同样视为校验失败。
        failed: list[str] = []
        for entry in entries:
            if not entry.sha1 and not entry.sha512:
                continue
            target = staging.joinpath(*entry.target_relative.split("/"))
            if not target.is_file():
                failed.append(entry.target_relative)
                continue
            if (entry.sha1 and _file_digest(target, "sha1") != entry.sha1.casefold()) or (
                entry.sha512 and _file_digest(target, "sha512") != entry.sha512.casefold()
            ):
                failed.append(entry.target_relative)
        return failed

    def _copy_pack_overrides(
        self,
        plan: ModpackPlan,
        staging: Path,
        context: OperationContext,
        ignored_paths: frozenset[str] = frozenset(),
    ) -> int:
        """
        把 overrides 内容复制进 staging，返回复制的文件数；无 overrides 返回 0。
        """
        overrides = plan.overrides_dir
        if overrides is None or not overrides.is_dir():
            return 0
        files = [
            path
            for path in overrides.rglob("*")
            if path.is_file() and path.relative_to(overrides).as_posix().casefold() not in ignored_paths
        ]
        for index, source in enumerate(files, 1):
            context.check_cancelled()
            relative = source.relative_to(overrides)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if index % 50 == 0 or index == len(files):
                context.progress(78 + 14 * index / max(len(files), 1), "正在复制整合包实例内容")
        return len(files)

    @staticmethod
    def _write_pack_version_json(base_name: str, target: ResolvedInstanceTarget, staging: Path) -> None:
        """
        保留基础版本的完整启动描述与本地父版本文件，使导入实例符合 Core 文件布局。

        Core 不把父描述的 mainClass 合并到子描述，且加载器目录可能内嵌原版
        JSON/JAR；只写 inheritsFrom 会得到无法启动的实例。复制仅限顶层启动
        元数据与客户端 JAR，不复制基础目录中的模组、配置或用户文件。
        """
        base_directory = target.instance_path.parent / base_name
        metadata = _read_json_file(base_directory / f"{base_name}.json")
        if not isinstance(metadata.get("mainClass"), str) or not metadata["mainClass"].strip():
            raise GameServiceError("基础版本缺少有效的启动描述，请重新安装基础版本", "PACK_BASE_VERSION_INVALID")
        staging.mkdir(parents=True, exist_ok=True)
        for source in base_directory.iterdir():
            if not source.is_file() or source.suffix not in {".json", ".jar"}:
                continue
            if source.name == f"{base_name}.json":
                continue
            name = f"{target.instance_directory_name}.jar" if source.name == f"{base_name}.jar" else source.name
            shutil.copy2(source, staging / name)
        metadata["id"] = target.instance_directory_name
        atomic_write_text(
            staging / f"{target.instance_directory_name}.json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )

    _online_lookup_batch_size = 100

    def _instance_export_snapshot(
        self, game_path: Path, version_id: str, version_isolation: bool | None, context: OperationContext
    ) -> ExportSnapshot:
        """
        按当前隔离策略解析数据目录，版本依赖只从所属实例的启动描述读取。
        """
        isolated = self.resolve_version_isolation(game_path, version_id, version_isolation)
        target = self.resolve_instance(game_path, version_id, isolated)
        manifest = target.instance_path / f"{target.instance_directory_name}.json"
        if manifest.is_symlink() or (manifest.exists() and ExportContentPolicy.is_link(manifest.lstat())):
            raise GameServiceError("实例启动描述不能是链接", "EXPORT_MANIFEST_INVALID")
        if manifest.is_file() and manifest.stat().st_size > ExportContentPolicy.max_manifest_bytes:
            raise GameServiceError("实例启动描述过大", "EXPORT_MANIFEST_TOO_LARGE")
        before = export_source_revision(target, {})
        dependencies = _resolve_export_dependencies(target)
        if export_source_revision(target, {}) != before:
            raise GameServiceError("实例启动描述已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")
        if not dependencies.get("minecraft"):
            raise GameServiceError("无法确定实例的 Minecraft 版本", "EXPORT_DEPENDENCIES_INVALID")
        snapshot = scan_export_files(target, dependencies, context)
        if export_source_revision(target, {}) != before:
            raise GameServiceError("实例启动描述已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")
        return snapshot

    def preview_instance_export(
        self, game_path: Path, version_id: str, version_isolation: bool | None = None
    ) -> dict[str, str]:
        """
        在后台扫描实例实际游戏目录，返回可选择文件的服务器身份。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 所属实例 ID
        :param version_isolation: 临时隔离覆盖，未指定时使用实例和全局设置
        :return: 可取消的扫描操作句柄
        """

        def worker(context: OperationContext) -> dict[str, JsonValue]:
            context.progress(0, "正在读取实例导出信息")
            snapshot = self._instance_export_snapshot(game_path, version_id, version_isolation, context)
            context.check_cancelled()
            return snapshot.payload()

        return self._application_operations.submit("instance_export_preview", worker)

    def export_instance_pack(
        self,
        game_path: Path,
        version_id: str,
        output_path: Path,
        pack_format: str,
        *,
        source_revision: str,
        selected_file_ids: list[str],
        pack_name: str,
        pack_version: str,
        summary: str,
        resource_mode: str,
        version_isolation: bool | None = None,
    ) -> dict[str, str]:
        """
        重新验证用户选择并原子导出标准 mrpack，失败或取消保留原有输出。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 源实例 ID
        :param output_path: 用户选择的输出位置
        :param pack_format: 当前仅支持 modrinth
        :param source_revision: 预览确认的来源身份
        :param selected_file_ids: 服务器生成的有界文件选择，空列表只导出依赖
        :param pack_name: 整合包名称
        :param pack_version: 字符串版本
        :param summary: 整合包简介
        :param resource_mode: references 优先反查所选模组，bundled 直接打包
        :param version_isolation: 未指定时使用实例有效隔离策略
        :return: 后台导出操作句柄，提交不等于完成
        :raises GameServiceError: 格式、来源、选择或输出冲突时抛出
        """
        if pack_format != "modrinth" or resource_mode not in {"references", "bundled"}:
            raise GameServiceError("不支持的整合包格式或资源模式", "INVALID_PACK_FORMAT")
        output = Path(output_path).expanduser().resolve(strict=False)
        if output.suffix.casefold() != ".mrpack":
            raise GameServiceError("导出文件必须使用 .mrpack 后缀", "INVALID_EXPORT_OUTPUT")
        selected_ids = list(selected_file_ids)
        with self._lock:
            if output in self._export_output_paths:
                raise GameServiceError("该输出位置正在导出，请等待任务结束", "EXPORT_OUTPUT_BUSY")

        def worker(context: OperationContext) -> dict[str, JsonValue]:
            with self._lock:
                if output in self._export_output_paths:
                    raise GameServiceError("该输出位置正在导出，请等待任务结束", "EXPORT_OUTPUT_BUSY")
                self._export_output_paths.add(output)
            temporary_file = output.with_name(f".{output.name}.{uuid4().hex}.ecl-tmp")
            try:
                snapshot = self._instance_export_snapshot(game_path, version_id, version_isolation, context)
                files = snapshot.selection(source_revision, selected_ids)
                if any(entry.source_path == output for entry in files):
                    raise GameServiceError("不能用导出结果覆盖已选源文件", "INVALID_EXPORT_OUTPUT")
                online_files: dict[str, OnlineExportFile] = {}
                warnings: list[str] = []
                if resource_mode == "references":
                    context.progress(15, "正在收集已选模组指纹")
                    hashed_mods = self._collect_export_mods(files, snapshot.root, context)
                    context.progress(22, "正在查询已选模组在线来源")
                    online_files, warnings = self._resolve_online_mod_files(hashed_mods, context)
                    unmatched = len(hashed_mods) - len(online_files)
                    if unmatched:
                        warnings.append(f"{unmatched} 个模组未识别在线来源，已直接打包")
                context.report_effect("staged")
                output.parent.mkdir(parents=True, exist_ok=True)
                packed_count = 0
                total_bytes = sum(entry.size for entry in files)
                completed_bytes = 0
                with (
                    temporary_file.open("xb") as output_stream,
                    zipfile.ZipFile(output_stream, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive,
                ):
                    for entry in files:
                        context.check_cancelled()
                        entry.validate(snapshot.root)
                        if entry.relative in online_files:
                            completed_bytes += entry.size
                            continue
                        with entry.source_path.open("rb") as source:
                            if not entry.matches(os.fstat(source.fileno())):
                                raise GameServiceError("导出文件已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")
                            with archive.open(f"overrides/{entry.relative}", "w", force_zip64=True) as destination:
                                while chunk := source.read(1024 * 1024):
                                    context.check_cancelled()
                                    destination.write(chunk)
                                    completed_bytes += len(chunk)
                                    context.progress(
                                        25 + 70 * completed_bytes / max(total_bytes, 1),
                                        "正在打包已选文件",
                                        done=completed_bytes,
                                        total=total_bytes,
                                        progressType="bytes",
                                    )
                            if not entry.matches(os.fstat(source.fileno())):
                                raise GameServiceError("导出期间文件已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")
                        packed_count += 1
                    archive.writestr(
                        "modrinth.index.json",
                        json.dumps(
                            {
                                "formatVersion": 1,
                                "game": "minecraft",
                                "versionId": pack_version,
                                "name": pack_name,
                                "summary": summary,
                                "files": [online_files[key] for key in sorted(online_files)],
                                "dependencies": dict(snapshot.dependencies),
                            },
                            ensure_ascii=False,
                        ),
                    )
                # 已反查为在线条目的文件也必须仍与预览一致。
                for entry in files:
                    entry.validate(snapshot.root)
                snapshot.validate_source()
                context.finalize(lambda: temporary_file.replace(output))
                return {
                    "path": str(output),
                    "format": "modrinth",
                    "onlineFiles": len(online_files),
                    "packedFiles": packed_count,
                    "selectedBytes": total_bytes,
                    "warnings": list(warnings),
                }
            finally:
                try:
                    temporary_file.unlink(missing_ok=True)
                finally:
                    with self._lock:
                        self._export_output_paths.discard(output)

        return self._application_operations.submit("instance_export", worker)

    def _collect_export_mods(
        self, files: tuple[ExportFile, ...], root: Path, context: OperationContext
    ) -> list[ExportMod]:
        """
        只计算已选启用模组的指纹；禁用模组及未选文件不参与联网反查。
        """
        mods: list[ExportMod] = []
        for entry in files:
            context.check_cancelled()
            if entry.category != "mods" or entry.source_path.suffix.casefold() != ".jar":
                continue
            entry.validate(root)
            with entry.source_path.open("rb") as stream:
                if not entry.matches(os.fstat(stream.fileno())):
                    raise GameServiceError("导出文件已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")
                sha1 = hashlib.sha1()
                sha512 = hashlib.sha512()
                while chunk := stream.read(1024 * 1024):
                    context.check_cancelled()
                    sha1.update(chunk)
                    sha512.update(chunk)
                fingerprint = _curseforge_fingerprint_stream(stream, context)
                if not entry.matches(os.fstat(stream.fileno())):
                    raise GameServiceError("导出文件已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")
            mods.append(
                {
                    "relative": entry.relative,
                    "path": entry.source_path,
                    "sha1": sha1.hexdigest(),
                    "sha512": sha512.hexdigest(),
                    "size": entry.size,
                    "fingerprint": fingerprint,
                }
            )
            entry.validate(root)
        return mods

    def _resolve_online_mod_files(
        self, hashed_mods: list[ExportMod], context: OperationContext
    ) -> tuple[dict[str, OnlineExportFile], list[str]]:
        """
        把本地模组反查为 mrpack ``files[]`` 在线条目。

        优先 Modrinth 哈希批量接口（sha1 分批），未命中的再走 CurseForge 指纹
        接口；接口失败或未配置 Key 时记录警告并降级为 overrides 打包。

        :return: (模组相对路径 -> files[] 条目) 与警告列表
        """
        resolved: dict[str, OnlineExportFile] = {}
        warnings: list[str] = []
        if not hashed_mods:
            return resolved, warnings
        mod_source = self.mod_source()
        try:
            for start in range(0, len(hashed_mods), self._online_lookup_batch_size):
                context.check_cancelled()
                batch = hashed_mods[start : start + self._online_lookup_batch_size]
                response = _proxied_post(
                    f"{mod_api_base(mod_source, 'modrinth')}/version_files",
                    json={"hashes": [mod["sha1"] for mod in batch], "algorithm": "sha1"},
                    headers={"User-Agent": mod_user_agent()},
                    timeout=15,
                )
                response.raise_for_status()
                versions = response.json()
                for mod in batch:
                    entry = self._modrinth_file_entry(mod, versions, mod_source)
                    if entry is not None:
                        resolved[mod["relative"]] = entry
        except (httpx.HTTPError, GameServiceError, ValueError, TypeError) as exc:
            context.check_cancelled()
            warnings.append(f"Modrinth 模组反查失败：{exc}")
        remaining = [mod for mod in hashed_mods if mod["relative"] not in resolved]
        if remaining:
            resolved.update(self._resolve_curseforge_matches(remaining, warnings, context))
        return resolved, warnings

    @staticmethod
    def _modrinth_file_entry(mod: ExportMod, versions: Any, mod_source: str) -> OnlineExportFile | None:
        """
        从批量反查响应中取哈希与所选本地模组一致的文件。

        文件地址按模组源重写后才能走镜像下载；无匹配文件视为未命中。

        :param mod: 含相对路径、哈希与指纹的本地模组条目
        :param versions: Modrinth 批量反查响应
        :param mod_source: 本次请求使用的模组源
        :return: mrpack ``files[]`` 条目；未命中时返回 None
        """
        version = versions.get(mod["sha1"]) if isinstance(versions, dict) else None
        files = version.get("files") if isinstance(version, dict) else None
        if not isinstance(files, list):
            return None
        candidate = next(
            (
                item
                for item in files
                if isinstance(item, dict)
                and isinstance(item.get("hashes"), dict)
                and item["hashes"].get("sha1") == mod["sha1"]
            ),
            None,
        )
        if not isinstance(candidate, dict) or not isinstance(candidate.get("url"), str) or not candidate["url"]:
            return None
        return {
            "path": mod["relative"],
            "hashes": {"sha1": mod["sha1"], "sha512": mod["sha512"]},
            "env": {"client": "required", "server": "required"},
            "downloads": [rewrite_mod_file_url(str(candidate["url"]), mod_source)],
            "fileSize": mod["size"],
        }

    def _resolve_curseforge_matches(
        self, mods: list[ExportMod], warnings: list[str], context: OperationContext
    ) -> dict[str, OnlineExportFile]:
        """
        对 Modrinth 未命中的模组做 CurseForge 指纹批量反查。

        模组源为 MCIM 时镜像不需要 API Key；官方源未配置 Key 或接口失败时记录
        警告并返回空，不阻塞导出。
        """
        resolved: dict[str, OnlineExportFile] = {}
        mod_source = self.mod_source()
        try:
            headers = self._curseforge_headers(mod_source)
        except GameServiceError as exc:
            warnings.append(f"CurseForge 指纹反查已跳过：{exc}")
            return resolved
        try:
            for start in range(0, len(mods), self._online_lookup_batch_size):
                context.check_cancelled()
                batch = mods[start : start + self._online_lookup_batch_size]
                response = _proxied_post(
                    f"{mod_api_base(mod_source, 'curseforge')}/fingerprints/432",
                    json={"fingerprints": [mod["fingerprint"] for mod in batch]},
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json().get("data") if isinstance(response.json(), dict) else None
                matches = data.get("exactMatches") if isinstance(data, dict) else None
                for match in matches or []:
                    file_info = match.get("file") if isinstance(match, dict) else None
                    if not isinstance(file_info, dict) or not file_info.get("downloadUrl"):
                        continue
                    fingerprint = file_info.get("fileFingerprint")
                    mod = next((item for item in batch if int(item["fingerprint"]) == int(fingerprint or -1)), None)
                    if mod is None:
                        continue
                    resolved[mod["relative"]] = {
                        "path": mod["relative"],
                        "hashes": {"sha1": mod["sha1"], "sha512": mod["sha512"]},
                        "env": {"client": "required", "server": "required"},
                        "downloads": [rewrite_mod_file_url(str(file_info["downloadUrl"]), mod_source)],
                        "fileSize": mod["size"],
                    }
        except (httpx.HTTPError, GameServiceError, ValueError, TypeError) as exc:
            context.check_cancelled()
            warnings.append(f"CurseForge 指纹反查失败：{exc}")
        return resolved
