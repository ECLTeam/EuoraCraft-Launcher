# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：整合包格式识别、解析与导入编排：归一为统一计划并装配为可运行实例。
#
# 公开接口：
#   - class PackFileEntry — 整合包内单个待下载文件条目（路径/直链/哈希/环境适用性）。
#   - class ModpackPlan — 归一后的整合包安装计划（版本、加载器、文件清单、overrides）。
#   - class ModpackCoordinator — 导入编排协调器：识别解析、自动安装基础版本、下载校验与实例装配。
#   - detect_pack_format(root) -> str — 按内容特征识别整合包格式标签。
#   - build_pack_plan(root) -> ModpackPlan — 解析指定格式并输出统一安装计划。
# ============================================================

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from threading import Event, Thread
from typing import Any

from ECL.utils import atomic_write_text

from .base import GameServiceError, _GameState
from .operations import OperationContext
from .workspace import ResolvedInstanceTarget, safe_extract_zip

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


def _file_digest(path: Path, algorithm: str) -> str:
    # 流式计算文件摘要，避免大文件一次性载入内存。
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ModpackCoordinator(_GameState):
    """
    编排整合包导入：解压识别、基础版本保障、文件下载与实例装配。
    """

    def import_instance_pack(self, game_path: Any, source_path: Any, new_version_id: Any) -> dict[str, str]:
        """
        安全导入整合包并装配为全新实例。

        支持 Modrinth mrpack、CurseForge manifest 包与 ECL 旧格式包：解压识别后
        自动安装缺失的基础版本与加载器，下载清单声明的模组与文件，经哈希校验
        后装配为继承基础版本的新实例。全程在 staging 目录进行，失败自动清理。

        :param game_path: Minecraft 游戏根目录
        :param source_path: 整合包压缩包路径
        :param new_version_id: 新实例的版本目录名
        :return: 长任务句柄（operationId 与初始状态）
        :raises GameServiceError: 目标实例已存在或压缩包不可读时抛出
        """
        target = self.resolve_instance(game_path, new_version_id)
        source = Path(str(source_path)).expanduser().resolve(strict=True)
        if target.instance_path.exists():
            raise GameServiceError("目标实例已存在", "INSTANCE_ALREADY_EXISTS")
        # 解压临时目录与 staging 都落在 versions 目录下，先确保其存在。
        target.instance_path.parent.mkdir(parents=True, exist_ok=True)

        def worker(context: OperationContext) -> dict[str, Any]:
            with tempfile.TemporaryDirectory(prefix="ecl-pack-import-", dir=target.instance_path.parent) as temp_dir:
                extracted = Path(temp_dir)
                safe_extract_zip(source, extracted)
                detected = detect_pack_format(extracted)
                if detected == "ecl-legacy":
                    return self._import_legacy_ecl_pack(extracted, target, context)
                if detected == "unknown":
                    raise GameServiceError("无法识别整合包格式", "INVALID_PACK_ARCHIVE")
                plan = build_pack_plan(extracted)
                context.progress(8, f"已识别整合包：{plan.pack_name}（{plan.format_name}）")
                result = self._install_plan_into_instance(plan, target, context)
                context.progress(98, "整合包导入完成")
                return result

        return self._game_operations.submit("instance_import", worker)

    def _import_legacy_ecl_pack(
        self, extracted: Path, target: ResolvedInstanceTarget, context: OperationContext
    ) -> dict[str, Any]:
        """
        按 ECL 旧格式流程导入：包内容整体即实例，主版本描述重命名为新实例名。

        保留历史行为：不下载文件、不校验基础版本。
        """
        staging = target.instance_path.with_name(f".{target.version_id}.ecl-import")
        try:
            shutil.copytree(extracted, staging, ignore=shutil.ignore_patterns("ecl-pack.json"))
            manifests = list(staging.glob("*.json"))
            original = next((path for path in manifests if path.name != "ecl-pack.json"), None)
            if original and original.name != f"{target.version_id}.json":
                original.rename(staging / f"{target.version_id}.json")
            context.check_cancelled()
            staging.replace(target.instance_path)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {
            "versionId": target.version_id,
            "path": str(target.instance_path),
            "format": "ecl-legacy",
            "downloadedFiles": 0,
            "skippedFiles": 0,
            "overridesFiles": 0,
            "baseVersion": None,
            "warnings": [],
        }

    def _install_plan_into_instance(
        self, plan: ModpackPlan, target: ResolvedInstanceTarget, context: OperationContext
    ) -> dict[str, Any]:
        """
        按统一计划装配实例：解析条目、保障基础版本、下载校验并原子落位。

        全程在 staging 目录进行，任何失败都会清理 staging，不影响既有实例。
        """
        if not plan.minecraft_version:
            raise GameServiceError("整合包缺少 Minecraft 版本声明", "INVALID_PACK_ARCHIVE")
        staging = target.instance_path.with_name(f".{target.version_id}.ecl-import")
        try:
            entries, skipped = self._resolve_pack_entries(plan, context)
            base_name = self._ensure_pack_base_version(plan, target, context)
            context.progress(32, f"正在下载 {len(entries)} 个整合包文件")
            self._download_pack_files(entries, staging, context, 32, 72)
            context.progress(74, "正在校验下载文件")
            self._verify_pack_files(entries, staging, context)
            context.progress(78, "正在复制整合包实例内容")
            overrides_count = self._copy_pack_overrides(plan, staging, context)
            self._write_pack_version_json(base_name, target, staging)
            context.check_cancelled()
            staging.replace(target.instance_path)
            return {
                "versionId": target.version_id,
                "path": str(target.instance_path),
                "format": plan.format_name,
                "downloadedFiles": len(entries),
                "skippedFiles": skipped,
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
            if plan.format_name == "curseforge":
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

        缺失时经同步安装核心自动安装；fabric/quilt 未声明加载器版本时解析
        当前游戏版本下的最新版本。安装不附带 Fabric API，避免与整合包自带
        的 API 模组重复。

        :raises GameServiceError: 加载器版本无法解析或安装后基础版本仍缺失时抛出
        """
        mc_version = plan.minecraft_version
        base_name = mc_version
        loader_version = plan.loader_version
        if plan.loader_type != "vanilla":
            if not loader_version:
                versions = self.loader_versions(plan.loader_type, mc_version)
                if not versions:
                    raise GameServiceError(
                        f"未找到 {plan.loader_type} 在 {mc_version} 下的可用版本",
                        "PACK_LOADER_VERSION_REQUIRED",
                    )
                loader_version = versions[0]
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
            game_path=target.game_path,
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

    def _copy_pack_overrides(self, plan: ModpackPlan, staging: Path, context: OperationContext) -> int:
        """
        把 overrides 内容复制进 staging，返回复制的文件数；无 overrides 返回 0。
        """
        overrides = plan.overrides_dir
        if overrides is None or not overrides.is_dir():
            return 0
        files = [path for path in overrides.rglob("*") if path.is_file()]
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
        # 写入继承基础版本的实例描述，包内自带同名描述时统一覆盖为新实例名。
        atomic_write_text(
            staging / f"{target.version_id}.json",
            json.dumps({"id": target.version_id, "inheritsFrom": base_name}, ensure_ascii=False, indent=2),
        )
