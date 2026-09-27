# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：三源 Mod 索引：mods 目录 JAR、崩溃报告模组表与加载器日志的
#   模组清单聚合，供堆栈与规则参数反查肇事模组。
#
# 公开接口：
#   - class CrashModAttribution — 一次归因查询的结果。
#   - class CrashModIndex — 维护包名前缀与模组 ID 到显示名的索引，带指纹缓存。
# ============================================================

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


@dataclass(frozen=True, slots=True)
class CrashModAttribution:
    """
    一次归因查询的结果。

    :param mods: 命中的模组显示名或文件名，去重排序
    :param packages: 未能映射到任何模组的堆栈帧包名候选
    """

    mods: tuple[str, ...]
    packages: tuple[str, ...]


class CrashModIndex:
    """
    维护包名前缀与模组 ID 到显示名的索引，供崩溃归因反查。

    三类来源按可靠度合并：mods 目录 JAR 内的元数据与类路径最可靠；崩溃报告
    内嵌的模组表次之；加载器调试日志的发现记录仅提供文件名。JAR 扫描按目录
    指纹（文件名、大小、修改时间）缓存，目录未变化时复用上次结果；文本来源
    在每次刷新时以幂等方式合并，索引实例的生命周期与分析会话一致。
    """

    max_ingested_entries = 2000

    _fabric_table_entry = re.compile(r"-\s+(?P<id>[\w.-]+)")
    _fabric_table_header = re.compile(r"loading \d+ mods?:", re.IGNORECASE)
    _fml_mod_table = re.compile(r"^\t(?P<id>[\w.-]+)\{[^}]*\}\s*\[(?P<name>[^\]]+)\]", re.MULTILINE)
    _forge_mod_table = re.compile(
        r"^\s*(?P<file>\S+\.jar)\s*\|(?P<name>[^|]*)\|(?P<id>[^|]*)\|",
        re.MULTILINE,
    )
    _loader_mod_file = re.compile(r"\b(?:found|loading) mod file\s+(?P<file>\S+\.jar)", re.IGNORECASE)

    def __init__(self) -> None:
        self._fingerprint_by_dir: dict[Path, tuple[tuple[str, int, int], ...]] = {}
        self._package_names: dict[str, str] = {}
        self._display_by_id: dict[str, str] = {}

    def refresh(self, mods_dirs: Iterable[Path], texts: Iterable[str]) -> None:
        """
        刷新索引：目录指纹变化时重扫 JAR，随后幂等合并文本来源。

        :param mods_dirs: 参与归因的 mods 目录（根目录与版本隔离目录）
        :param texts: 本次分析的日志文本，用于提取内嵌模组清单
        """
        fingerprints = {mods_dir: self._directory_fingerprint(mods_dir) for mods_dir in mods_dirs}
        if any(self._fingerprint_by_dir.get(mods_dir) != fingerprint for mods_dir, fingerprint in fingerprints.items()):
            self._fingerprint_by_dir = fingerprints
            self._package_names.clear()
            self._display_by_id.clear()
            for mods_dir in fingerprints:
                self._scan_dir(mods_dir)
        for text in texts:
            self._ingest_text(text)

    def resolve(self, names: Iterable[str]) -> tuple[str, ...]:
        """
        将模组 ID、类名等线索解析为已安装模组的显示名。

        :param names: 命名捕获组归因线索（模组 ID、完整类名等）
        :return: 去重排序后的显示名，最多 8 个
        """
        mods: list[str] = []
        for name in names:
            for display in self._resolve_name(name):
                if display not in mods:
                    mods.append(display)
        return tuple(sorted(mods)[:8])

    def resolve_frames(self, class_names: Iterable[str]) -> CrashModAttribution:
        """
        解析堆栈帧类名，返回命中的模组与未命中的包名候选。

        :param class_names: 堆栈帧完整类名
        :return: 归因结果；未命中类名按前三段包名计入 ``packages``
        """
        mods: list[str] = []
        packages: list[str] = []
        for raw in class_names:
            normalized = str(raw).replace("/", ".").strip()
            resolved = self._resolve_name(normalized)
            if resolved:
                for display in resolved:
                    if display not in mods:
                        mods.append(display)
                continue
            parts = [part for part in normalized.split(".") if part]
            if len(parts) >= 3:
                package = ".".join(parts[:3])
                if package not in packages:
                    packages.append(package)
        return CrashModAttribution(mods=tuple(sorted(mods)[:8]), packages=tuple(packages[:8]))

    def _resolve_name(self, name: str) -> list[str]:
        # 先按模组 ID 精确匹配，再按类名前缀逐级缩短匹配包名索引。
        normalized = str(name).replace("/", ".").strip()
        if not normalized:
            return []
        display = self._display_by_id.get(normalized.casefold())
        if display:
            return [display]
        parts = [part for part in normalized.split(".") if part]
        for size in (3, 2, 1):
            if len(parts) >= size:
                display = self._package_names.get(".".join(parts[:size]))
                if display:
                    return [display]
        return []

    @staticmethod
    def _directory_fingerprint(mods_dir: Path) -> tuple[tuple[str, int, int], ...]:
        # 以 JAR 文件名、大小与修改时间构成目录指纹，检测替换与增删。
        if not mods_dir.is_dir():
            return ()
        entries: list[tuple[str, int, int]] = []
        try:
            for jar in mods_dir.iterdir():
                if not jar.is_file() or jar.suffix.casefold() != ".jar":
                    continue
                try:
                    stat = jar.stat()
                    entries.append((jar.name.casefold(), stat.st_size, int(stat.st_mtime)))
                except OSError:
                    continue
        except OSError:
            return ()
        return tuple(sorted(entries))

    def _scan_dir(self, mods_dir: Path) -> None:
        # 扫描单个 mods 目录：读取 JAR 元数据并按类路径建立包名前缀索引。
        if not mods_dir.is_dir():
            return
        try:
            jars = [jar for jar in mods_dir.iterdir() if jar.is_file() and jar.suffix.casefold() == ".jar"]
        except OSError:
            return
        for jar in jars:
            try:
                with ZipFile(jar) as archive:
                    display, mod_id = self._mod_metadata(archive, jar.stem)
                    if mod_id:
                        self._display_by_id.setdefault(mod_id.casefold(), display)
                    names = archive.namelist()
                    for name in names[:5000]:
                        if not name.endswith(".class") or name.startswith(("net/minecraft/", "java/", "com/mojang/")):
                            continue
                        parts = PurePosixPath(name).parts
                        if len(parts) >= 4:
                            self._package_names.setdefault(".".join(parts[:3]), display)
            except (BadZipFile, OSError):
                continue

    @staticmethod
    def _mod_metadata(archive: ZipFile, fallback: str) -> tuple[str, str]:
        # 读取 fabric.mod.json 与（Neo）Forge mods.toml 的显示名与模组 ID；
        # 解析失败时回退 JAR 文件名，保证索引仍有可读输出。
        display = fallback
        mod_id = ""
        try:
            names = archive.namelist()
            if "fabric.mod.json" in names:
                metadata = json.loads(archive.read("fabric.mod.json")[: 512 * 1024].decode("utf-8", errors="replace"))
                if isinstance(metadata, dict):
                    raw_id = metadata.get("id")
                    if isinstance(raw_id, str) and raw_id.strip():
                        mod_id = raw_id.strip()[:120]
                    name = metadata.get("name") or metadata.get("id")
                    if isinstance(name, str) and name.strip():
                        display = name.strip()[:120]
            for metadata_name in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if metadata_name not in names:
                    continue
                content = archive.read(metadata_name)[: 512 * 1024].decode("utf-8", errors="replace")
                if not mod_id:
                    match = re.search(r'(?m)^\s*modId\s*=\s*["\']([^"\']+)', content)
                    if match:
                        mod_id = match.group(1).strip()[:120]
                match = re.search(r'(?m)^\s*displayName\s*=\s*["\']([^"\']+)', content)
                if match and display == fallback:
                    display = match.group(1).strip()[:120]
        except (BadZipFile, KeyError, OSError, UnicodeError, json.JSONDecodeError):
            pass
        return display, mod_id

    def _ingest_text(self, text: str) -> None:
        # 从崩溃报告与加载器日志的文本来源合并模组线索，重复条目幂等。
        if len(self._display_by_id) >= self.max_ingested_entries:
            return
        for match in self._forge_mod_table.finditer(text):
            mod_id = match.group("id").strip()
            file_name = match.group("file")
            display = match.group("name").strip() or file_name.removesuffix(".jar")
            if mod_id:
                self._display_by_id.setdefault(mod_id.casefold(), display)
        for match in self._fml_mod_table.finditer(text):
            self._display_by_id.setdefault(
                match.group("id").casefold(),
                match.group("name").strip()[:120] or match.group("id"),
            )
        self._ingest_fabric_block(text)
        for match in self._loader_mod_file.finditer(text):
            stem = match.group("file").removesuffix(".jar")
            self._display_by_id.setdefault(stem.casefold(), stem)

    def _ingest_fabric_block(self, text: str) -> None:
        # 解析 Fabric 启动日志 "Loading N mods:" 之后的连续条目块。
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not self._fabric_table_header.search(line):
                continue
            for candidate in lines[index + 1 :]:
                match = self._fabric_table_entry.match(candidate.strip())
                if match is None:
                    break
                mod_id = match.group("id")
                self._display_by_id.setdefault(mod_id.casefold(), mod_id)
