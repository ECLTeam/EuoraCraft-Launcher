# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义无签名 .eclplugin 包格式，并在解压前检查文件路径和哈希。
#
# 公开接口：
#   - class PluginPackageError — 插件归档校验或制作失败。
#   - class PluginPackageInfo — 已校验插件归档的元信息。
#   - inspect_plugin_package(path) -> PluginPackageInfo — 校验归档并返回元信息。
#   - extract_plugin_package(path, target_path) -> PluginPackageInfo — 校验并解包到新目录。
#   - build_plugin_package(source_path, output_path) -> PluginPackageInfo — 制作确定性的插件归档。
# ============================================================

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4

max_archive_bytes = 2 * 1024**3
max_uncompressed_bytes = 4 * 1024**3
max_file_bytes = 2 * 1024**3
max_manifest_bytes = 2 * 1024**2
max_entry_count = 10000
max_compression_ratio = 1000
copy_chunk_bytes = 1024**2
plugin_name_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
sha256_pattern = re.compile(r"[0-9a-f]{64}\Z")


class PluginPackageError(ValueError):
    """
    表示插件归档格式、内容或制作过程不符合 v1 契约。

    调用方应将错误展示为安装失败，不应回退到目录插件安装路径。
    """


@dataclass(frozen=True, slots=True)
class PluginPackageInfo:
    """
    保存已校验归档的插件标识、版本和内容摘要。

    归档不带作者签名；摘要仅用于检测当前归档内部的一致性和版本去重。
    """

    name: str
    version: str
    manifest_sha256: str
    file_count: int
    total_uncompressed_bytes: int


def _checked_name(raw_name: str) -> str:
    """
    约束 ZIP 条目为规范的相对 POSIX 路径，阻断解包路径穿越和跨平台歧义。
    """
    if not raw_name or "\\" in raw_name or "\x00" in raw_name or ":" in raw_name:
        raise PluginPackageError(f"非法归档路径: {raw_name!r}")
    parts = raw_name.split("/")
    if raw_name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise PluginPackageError(f"非法归档路径: {raw_name!r}")
    normalized = PurePosixPath(raw_name).as_posix()
    if normalized != raw_name or normalized.casefold() == "package-signature.json":
        raise PluginPackageError(f"不支持的归档路径: {raw_name!r}")
    return normalized


def _read_json(raw: bytes, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError(f"{label} 不是有效的 UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise PluginPackageError(f"{label} 必须是 JSON 对象")
    return parsed


def _hash_stream(source: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(copy_chunk_bytes):
        size += len(chunk)
        if size > max_file_bytes:
            raise PluginPackageError("单个归档文件超过大小上限")
        digest.update(chunk)
    return digest.hexdigest(), size


def _zip_entries(archive: zipfile.ZipFile) -> tuple[dict[str, zipfile.ZipInfo], int]:
    """
    在读取任何载荷前验证归档结构和解压预算。
    """
    entries = archive.infolist()
    if len(entries) > max_entry_count:
        raise PluginPackageError("插件归档文件数超过上限")
    entries_by_name: dict[str, zipfile.ZipInfo] = {}
    names_by_folded: set[str] = set()
    total_size = 0
    for entry in entries:
        if entry.is_dir():
            raise PluginPackageError("插件归档不能包含显式目录条目")
        name = _checked_name(entry.filename)
        folded_name = name.casefold()
        if folded_name in names_by_folded:
            raise PluginPackageError(f"插件归档包含重复路径: {name}")
        names_by_folded.add(folded_name)
        entry_mode = (entry.external_attr >> 16) & 0o170000
        if entry_mode not in {0, stat.S_IFREG}:
            raise PluginPackageError(f"插件归档包含非普通文件: {name}")
        if entry.flag_bits & 0x1:
            raise PluginPackageError(f"插件归档包含加密文件: {name}")
        if entry.file_size > max_file_bytes:
            raise PluginPackageError(f"归档文件超过大小上限: {name}")
        if entry.file_size > max_compression_ratio * max(entry.compress_size, 1):
            raise PluginPackageError(f"归档文件压缩比异常: {name}")
        total_size += entry.file_size
        if total_size > max_uncompressed_bytes:
            raise PluginPackageError("插件归档解压总量超过上限")
        entries_by_name[name] = entry
    return entries_by_name, total_size


def _manifest_records(
    archive: zipfile.ZipFile, entries_by_name: dict[str, zipfile.ZipInfo]
) -> tuple[bytes, dict[str, tuple[int, str]]]:
    """
    校验清单的文件集合和元数据类型，避免遗漏或覆盖载荷。
    """
    manifest_entry = entries_by_name.get("package-manifest.json")
    plugin_entry = entries_by_name.get("plugin.json")
    if manifest_entry is None or plugin_entry is None:
        raise PluginPackageError("插件归档缺少 plugin.json 或 package-manifest.json")
    if manifest_entry.file_size > max_manifest_bytes or plugin_entry.file_size > max_manifest_bytes:
        raise PluginPackageError("插件清单超过大小上限")
    manifest_bytes = archive.read(manifest_entry)
    manifest = _read_json(manifest_bytes, "package-manifest.json")
    if manifest.get("format_version") != 1 or isinstance(manifest.get("format_version"), bool):
        raise PluginPackageError("不支持的插件归档格式版本")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PluginPackageError("归档清单 files 必须是数组")
    declared: dict[str, tuple[int, str]] = {}
    for item in files:
        if not isinstance(item, dict):
            raise PluginPackageError("归档清单包含非法文件记录")
        raw_name, raw_size, raw_sha256 = item.get("path"), item.get("size"), item.get("sha256")
        if not isinstance(raw_name, str):
            raise PluginPackageError("归档清单文件路径必须是字符串")
        name = _checked_name(raw_name)
        if name == "package-manifest.json" or name in declared:
            raise PluginPackageError(f"归档清单包含重复或自引用文件: {name}")
        if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
            raise PluginPackageError(f"归档清单文件大小无效: {name}")
        if not isinstance(raw_sha256, str) or sha256_pattern.fullmatch(raw_sha256) is None:
            raise PluginPackageError(f"归档清单文件哈希无效: {name}")
        declared[name] = (raw_size, raw_sha256)
    if set(declared) != set(entries_by_name) - {"package-manifest.json"}:
        raise PluginPackageError("归档文件与清单声明不一致")
    return manifest_bytes, declared


def _inspect(archive_path: Path) -> tuple[PluginPackageInfo, tuple[str, ...]]:
    if not archive_path.is_file() or archive_path.suffix.lower() != ".eclplugin":
        raise PluginPackageError("请选择 .eclplugin 文件")
    if archive_path.stat().st_size > max_archive_bytes:
        raise PluginPackageError("插件归档超过大小上限")
    with zipfile.ZipFile(archive_path) as archive:
        entries_by_name, total_size = _zip_entries(archive)
        manifest_bytes, declared = _manifest_records(archive, entries_by_name)
        for name, (declared_size, declared_hash) in declared.items():
            entry = entries_by_name[name]
            if declared_size != entry.file_size:
                raise PluginPackageError(f"归档文件大小与清单不符: {name}")
            with archive.open(entry) as source:
                actual_hash, actual_size = _hash_stream(source)
            if actual_size != declared_size or actual_hash != declared_hash:
                raise PluginPackageError(f"归档文件哈希与清单不符: {name}")
        plugin = _read_json(archive.read(entries_by_name["plugin.json"]), "plugin.json")
        name = plugin.get("name")
        version = plugin.get("version", "0.0.0")
        if not isinstance(name, str) or plugin_name_pattern.fullmatch(name) is None:
            raise PluginPackageError("plugin.json 的 name 无效")
        if not isinstance(version, str) or not version:
            raise PluginPackageError("plugin.json 的 version 无效")
        info = PluginPackageInfo(
            name=name,
            version=version,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            file_count=len(declared),
            total_uncompressed_bytes=total_size,
        )
        return info, tuple(sorted(declared))


def inspect_plugin_package(archive_path: Path) -> PluginPackageInfo:
    """
    校验未签名归档结构、路径和所有载荷文件的 SHA-256。

    本函数不运行插件代码，也不证明包来源；调用方仍须向用户展示来源未验证提示。

    :param archive_path: 本地 `.eclplugin` 文件
    :return: 当前归档的基本信息与清单摘要
    :raises PluginPackageError: 包格式、文件路径或内容校验失败时抛出
    """
    try:
        info, _names = _inspect(Path(archive_path))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PluginPackageError("插件归档读取失败") from exc
    return info


def extract_plugin_package(archive_path: Path, target_path: Path) -> PluginPackageInfo:
    """
    将校验通过的归档解包到尚不存在的新目录。

    只写入非活动目录；若解包失败会删除本次创建的目录，不触及已有插件。

    :param archive_path: 本地 `.eclplugin` 文件
    :param target_path: 尚不存在的目标插件代码目录
    :return: 当前归档的基本信息
    :raises PluginPackageError: 归档无效、目标已存在或解包失败时抛出
    """
    archive_path = Path(archive_path)
    target_path = Path(target_path)
    try:
        info, names = _inspect(archive_path)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PluginPackageError("插件归档读取失败") from exc
    if target_path.exists():
        raise PluginPackageError("插件解包目标已存在")
    target_path.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            manifest_bytes = archive.read("package-manifest.json")
            if hashlib.sha256(manifest_bytes).hexdigest() != info.manifest_sha256:
                raise PluginPackageError("校验后归档清单发生变化")
            manifest = _read_json(manifest_bytes, "package-manifest.json")
            expected_by_name = {item["path"]: (item["size"], item["sha256"]) for item in manifest["files"]}
            for name in names:
                destination = target_path.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                partial_path = destination.with_name(f"{destination.name}.partial")
                with archive.open(name) as source, partial_path.open("xb") as output:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := source.read(copy_chunk_bytes):
                        size += len(chunk)
                        if size > max_file_bytes:
                            raise PluginPackageError("插件解包文件超过大小上限")
                        digest.update(chunk)
                        output.write(chunk)
                    expected_size, expected_hash = expected_by_name[name]
                    if digest.hexdigest() != expected_hash or size != expected_size:
                        raise PluginPackageError(f"解包时文件发生变化: {name}")
                partial_path.replace(destination)
            manifest_partial = target_path / "package-manifest.json.partial"
            manifest_partial.write_bytes(manifest_bytes)
            manifest_partial.replace(target_path / "package-manifest.json")
    except (OSError, RuntimeError, ValueError, StopIteration, zipfile.BadZipFile) as exc:
        shutil.rmtree(target_path)
        if isinstance(exc, PluginPackageError):
            raise
        raise PluginPackageError("插件归档解包失败") from exc
    return info


def build_plugin_package(source_path: Path, output_path: Path) -> PluginPackageInfo:
    """
    从插件源目录生成确定性的未签名 `.eclplugin` 文件。

    输出使用临时文件后替换；源目录中不允许符号链接，防止把目录外文件带进包。

    :param source_path: 含 `plugin.json` 的插件源目录
    :param output_path: 输出 `.eclplugin` 文件，允许替换已有普通文件
    :return: 对新归档重新校验得到的信息
    :raises PluginPackageError: 源目录或输出归档不符合格式时抛出
    """
    source_path = Path(source_path).resolve()
    output_path = Path(output_path)
    if not source_path.is_dir() or output_path.suffix.lower() != ".eclplugin":
        raise PluginPackageError("插件源目录或输出扩展名无效")
    if output_path.resolve().is_relative_to(source_path):
        raise PluginPackageError("输出归档不能位于插件源目录内")
    if any(file_path.is_symlink() for file_path in source_path.rglob("*")):
        raise PluginPackageError("插件源目录不能包含符号链接")
    source_files = sorted(file_path for file_path in source_path.rglob("*") if file_path.is_file())
    records: list[dict[str, str | int]] = []
    names_by_folded: set[str] = set()
    for file_path in source_files:
        name = _checked_name(file_path.relative_to(source_path).as_posix())
        if name == "package-manifest.json" or name.casefold() in names_by_folded:
            raise PluginPackageError(f"插件源目录包含重复或保留文件: {name}")
        names_by_folded.add(name.casefold())
        with file_path.open("rb") as source:
            digest, size = _hash_stream(source)
        records.append({"path": name, "size": size, "sha256": digest})
    if "plugin.json" not in {record["path"] for record in records}:
        raise PluginPackageError("插件源目录缺少 plugin.json")
    manifest_bytes = json.dumps(
        {"format_version": 1, "files": records}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(manifest_bytes) > max_manifest_bytes:
        raise PluginPackageError("归档清单超过大小上限")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(f".{output_path.stem}.{uuid4().hex}.eclplugin")
    try:
        with zipfile.ZipFile(partial_path, "w") as archive:
            for file_path in source_files:
                name = file_path.relative_to(source_path).as_posix()
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_STORED if name.endswith(".whl") else zipfile.ZIP_DEFLATED
                entry.external_attr = (stat.S_IFREG | 0o644) << 16
                with file_path.open("rb") as source, archive.open(entry, "w") as destination:
                    shutil.copyfileobj(source, destination, copy_chunk_bytes)
            manifest_entry = zipfile.ZipInfo("package-manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
            manifest_entry.compress_type = zipfile.ZIP_DEFLATED
            manifest_entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(manifest_entry, manifest_bytes)
        info = inspect_plugin_package(partial_path)
        partial_path.replace(output_path)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        partial_path.unlink(missing_ok=True)
        if isinstance(exc, PluginPackageError):
            raise
        raise PluginPackageError("插件归档制作失败") from exc
    return info
