# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例目标解析：game_path/version_id/隔离语义与相对 ID 校验。
#
# 公开接口：
#   - class ResolvedInstanceTarget — 保存经校验的实例目录和与启动参数一致的实际游戏数据目录。
#   - resolve_instance_target(game_path, version_id, version_isolation=…) -> ResolvedInstanceTarget — 规范化实例目标，并镜像现有 Game Core 的版本隔离目录语义。
#   - resolve_relative_id(parent, relative_id, must_exist) -> Path — 把前端提供的相对资源 ID 安全解析到指定父目录内。
#   - delete_path(path) -> None — 递归删除文件或目录。
#   - safe_extract_zip(archive_path, destination, max_files, max_uncompressed_bytes, max_ratio) -> list[Path] — 校验 ZIP 路径、符号链接和压缩比后解压到目标目录。
#   - class WorkspaceCoordinator — 提供实例工作台通用目录、复制、导入导出、校验和删除操作。
#       - resolve_instance(game_path, version_id, version_isolation=…) -> ResolvedInstanceTarget
#       - quick_launch_arguments(game_path, version_id, quick_target, version_isolation=…) -> list[str] — 校验快速目标并按版本能力生成世界或服务器启动参数。
#       - open_instance_folder(game_path, version_id, folder, version_isolation=…) -> dict[str, str] — 打开实例工作台允许的固定目录，不接受任意绝对路径。
#       - delete_instance(game_path, version_id) -> None — 阻止运行中实例后，仅回收版本目录，不触碰共享资源和第三方配置。
#       - clone_instance(game_path, version_id, new_version_id, version_isolation=…) -> dict[str, str] — 异步复制实例，并重写顶层版本文件名、清理统计和第三方兼容副本。
#       - inspect_instance_files(game_path, version_id, source=…) -> dict[str, Any] — 只读检查版本 JSON、主 JAR 和已声明库文件，不执行下载或写入。
#       - repair_instance_files(game_path, version_id, source=…) -> dict[str, str] — 在用户确认只读清单后，异步调用 Core 文件补全能力。
#       - operation_get(operation_id) -> dict[str, Any]
#       - operation_cancel(operation_id) -> bool
# ============================================================

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ECL.services.operations import OperationContext
from ECL.utils import atomic_write_text

from .base import GameServiceError


@dataclass(frozen=True, slots=True)
class ResolvedInstanceTarget:
    """
    保存经校验的实例目录和与启动参数一致的实际游戏数据目录。
    """

    minecraft_root_path: Path
    instance_directory_name: str
    version_isolation: bool
    instance_path: Path
    game_data_path: Path

    @property
    def root_key(self) -> str:
        """
        返回实际目录身份，保留大小写敏感目录之间的区别。

        :return: 服务端解析的规范根目录
        """
        return str(self.minecraft_root_path)

    @property
    def instance_key(self) -> str:
        """
        返回根目录与磁盘实例组成的身份，不作为文件访问授权。

        :return: 可用于前端比较的实例键
        """
        return f"{self.root_key}\0{self.instance_directory_name}"


def resolve_instance_target(game_path: Any, version_id: Any, version_isolation: Any = False) -> ResolvedInstanceTarget:
    """
    规范化实例目标，并镜像现有 Game Core 的版本隔离目录语义。
    """
    root = Path(str(game_path)).expanduser().resolve(strict=False)
    if root.name.casefold() == "versions":
        root = root.parent
    name = str(version_id or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name or any(char in name for char in ("/", "\\", "\0")):
        raise GameServiceError("实例 ID 格式无效", "INVALID_VERSION_NAME")
    instance_path = (root / "versions" / name).resolve(strict=False)
    versions_path = (root / "versions").resolve(strict=False)
    if instance_path.parent != versions_path:
        raise GameServiceError("实例路径越过游戏目录边界", "INVALID_INSTANCE_TARGET")
    # 保持与当前 LaunchCoordinator / ECL.game PlaceholderReplacer 完全一致。
    data_path = instance_path if bool(version_isolation) else versions_path
    return ResolvedInstanceTarget(root, name, bool(version_isolation), instance_path, data_path)


def resolve_relative_id(parent: Path, relative_id: Any, *, must_exist: bool = True) -> Path:
    """
    把前端提供的相对资源 ID 安全解析到指定父目录内。
    """
    value = str(relative_id or "").strip().replace("\\", "/")
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise GameServiceError("资源 ID 格式无效", "INVALID_RELATIVE_ID")
    parent = parent.resolve(strict=False)
    target = parent.joinpath(*pure.parts).resolve(strict=False)
    if target != parent and parent not in target.parents:
        raise GameServiceError("资源路径越过实例边界", "INVALID_RELATIVE_ID")
    if must_exist and not target.exists():
        raise GameServiceError("目标资源不存在", "RESOURCE_NOT_FOUND")
    return target


def delete_path(path: Path) -> None:
    """
    递归删除文件或目录。
    """
    target = path.resolve(strict=False)
    if not target.exists():
        raise GameServiceError("目标不存在", "RESOURCE_NOT_FOUND")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as exc:
        raise GameServiceError(f"无法删除：{exc}", "DELETE_FAILED") from exc


def safe_extract_zip(
    archive_path: Path,
    destination: Path,
    *,
    max_files: int = 20000,
    max_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024,
    max_ratio: int = 200,
) -> list[Path]:
    """
    校验 ZIP 路径、符号链接和压缩比后解压到目标目录。
    """
    destination = destination.resolve(strict=False)
    extracted: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > max_files:
            raise GameServiceError("压缩包文件数量过多", "ZIP_BOMB_DETECTED")
        total = sum(item.file_size for item in members)
        if total > max_uncompressed_bytes:
            raise GameServiceError("压缩包解压后体积超过安全限制", "ZIP_BOMB_DETECTED")
        compressed = max(1, sum(item.compress_size for item in members))
        if total > 64 * 1024 * 1024 and total / compressed > max_ratio:
            raise GameServiceError("压缩包压缩比异常", "ZIP_BOMB_DETECTED")
        for item in members:
            name = item.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                raise GameServiceError("压缩包包含不安全路径", "ZIP_PATH_TRAVERSAL")
            unix_mode = item.external_attr >> 16
            if unix_mode & 0o170000 == 0o120000:
                raise GameServiceError("压缩包包含符号链接", "ZIP_SYMLINK_REJECTED")
            output = destination.joinpath(*pure.parts).resolve(strict=False)
            if destination not in output.parents and output != destination:
                raise GameServiceError("压缩包路径越过目标目录", "ZIP_PATH_TRAVERSAL")
            extracted.append(output)
        archive.extractall(destination)
    return extracted


class WorkspaceCoordinator:
    """
    提供实例工作台通用目录、复制、导入导出、校验和删除操作。
    """

    def resolve_instance(
        self, game_path: Any, version_id: Any, version_isolation: Any = False
    ) -> ResolvedInstanceTarget:
        return resolve_instance_target(game_path, version_id, version_isolation)

    def quick_launch_arguments(
        self,
        game_path: Any,
        version_id: Any,
        quick_target: dict[str, Any],
        version_isolation: Any = False,
    ) -> list[str]:
        """
        校验快速目标并按版本能力生成世界或服务器启动参数。
        """
        target = self.resolve_instance(game_path, version_id, version_isolation)
        manifest_path = target.instance_path / f"{target.instance_directory_name}.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise GameServiceError("无法读取版本能力清单", "VERSION_JSON_INVALID") from exc
        game_arguments = (manifest.get("arguments") or {}).get("game") or []
        kind = quick_target.get("type")
        if kind == "world":
            world_id = str(quick_target.get("world_id") or "")
            resolve_relative_id(target.game_data_path / "saves", world_id)
            if not any("quickPlaySingleplayer" in str(item) for item in game_arguments):
                raise GameServiceError("该版本不支持快速进入世界", "QUICK_PLAY_WORLD_UNSUPPORTED")
            return ["--quickPlaySingleplayer", world_id]
        if kind == "server":
            address = str(quick_target.get("address") or "").strip()
            if not address or any(char in address for char in ("\0", "\r", "\n", " ")):
                raise GameServiceError("服务器地址无效", "INVALID_SERVER_ADDRESS")
            if any("quickPlayMultiplayer" in str(item) for item in game_arguments):
                return ["--quickPlayMultiplayer", address]
            host, separator, port = address.rpartition(":")
            if separator and port.isdigit() and not address.startswith("["):
                return ["--server", host, "--port", port]
            return ["--server", address]
        raise GameServiceError("未知快速启动目标", "INVALID_QUICK_TARGET")

    def open_instance_folder(
        self, game_path: Any, version_id: Any, folder: str, version_isolation: Any = False
    ) -> dict[str, str]:
        """
        打开实例工作台允许的固定目录，不接受任意绝对路径。
        """
        target = self.resolve_instance(game_path, version_id, version_isolation)
        folders = {
            "instance": target.instance_path,
            "mods": target.game_data_path / "mods",
            "saves": target.game_data_path / "saves",
            "screenshots": target.game_data_path / "screenshots",
            "logs": target.game_data_path / "logs",
            "crash-reports": target.game_data_path / "crash-reports",
        }
        if folder not in folders:
            raise GameServiceError("不支持的实例目录类型", "INVALID_FOLDER_KIND")
        path = folders[folder]
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        return {"path": str(path)}

    def delete_instance(self, game_path: Any, version_id: Any) -> None:
        """
        阻止运行中实例后，仅回收版本目录，不触碰共享资源和第三方配置。
        """
        target = self.resolve_instance(game_path, version_id)
        game_key = target.root_key
        for instance in self.list_instances():
            if (
                bool(instance.get("gamePath"))
                and str(Path(str(instance["gamePath"])).expanduser().resolve(strict=False)) == game_key
                and instance.get("versionId") == target.instance_directory_name
            ):
                raise GameServiceError("游戏正在运行，无法删除实例", "INSTANCE_IS_RUNNING")
        delete_path(target.instance_path)
        self.events.emit(
            "game:instances_changed", {"reason": "instance_deleted", "versionId": target.instance_directory_name}
        )

    def clone_instance(  # noqa: C901 - clone transaction keeps cleanup and atomic commit in one boundary
        self,
        game_path: Any,
        version_id: Any,
        new_version_id: Any,
        version_isolation: Any = False,
    ) -> dict[str, str]:
        """
        异步复制实例，并重写顶层版本文件名、清理统计和第三方兼容副本。
        """
        source = self.resolve_instance(game_path, version_id, version_isolation)
        destination = self.resolve_instance(game_path, new_version_id, version_isolation)
        if not source.instance_path.is_dir():
            raise GameServiceError("源实例不存在", "INSTANCE_NOT_FOUND")
        if destination.instance_path.exists():
            raise GameServiceError("目标实例名称已存在", "INSTANCE_ALREADY_EXISTS")

        def worker(context: OperationContext) -> dict[str, str]:  # noqa: C901
            temp = destination.instance_path.with_name(
                f".{destination.instance_directory_name}.ecl-copy-{context.operation_id}"
            )
            try:
                context.progress(5, "正在复制版本文件")
                shutil.copytree(source.instance_path, temp)
                context.check_cancelled()
                for suffix in (".json", ".jar"):
                    old = temp / f"{source.instance_directory_name}{suffix}"
                    if old.exists():
                        old.rename(temp / f"{destination.instance_directory_name}{suffix}")
                config_path = temp / f"{destination.instance_directory_name}.json"
                if config_path.is_file():
                    try:
                        config = json.loads(config_path.read_text(encoding="utf-8"))
                        if isinstance(config, dict):
                            config["id"] = destination.instance_directory_name
                            atomic_write_text(config_path, json.dumps(config, ensure_ascii=False, indent=2))
                    except (OSError, UnicodeDecodeError, ValueError):
                        pass
                ecl_dir = temp / ".ecl"
                (temp / "eclversion.json").unlink(missing_ok=True)
                for name in ("resources.json", "servers.json"):
                    file_path = ecl_dir / name
                    if file_path.exists():
                        file_path.unlink()
                profile_path = ecl_dir / "instance.json"
                if profile_path.is_file():
                    try:
                        profile = json.loads(profile_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, ValueError):
                        profile = {}
                    if isinstance(profile, dict):
                        original_alias = str(profile.get("alias") or source.instance_directory_name)
                        profile["alias"] = f"{original_alias} 副本"
                        for key in ("favorite", "hidden", "pinned", "pinOrder", "preferredExternalSource"):
                            profile.pop(key, None)
                        atomic_write_text(profile_path, json.dumps(profile, ensure_ascii=False, indent=2))
                for third_party in ("PCL", ".hmcl"):
                    third_party_path = temp / third_party
                    if third_party_path.is_dir():
                        shutil.rmtree(third_party_path)
                    elif third_party_path.exists():
                        third_party_path.unlink()
                context.progress(90, "正在提交实例副本")
                temp.replace(destination.instance_path)
                self.events.emit("game:instances_changed", {"reason": "instance_cloned"})
                return {"versionId": destination.instance_directory_name, "path": str(destination.instance_path)}
            except Exception:
                if temp.exists():
                    shutil.rmtree(temp, ignore_errors=True)
                raise

        return self._application_operations.submit("instance_clone", worker)

    def inspect_instance_files(self, game_path: Any, version_id: Any, source: Any = "official") -> dict[str, Any]:
        """
        只读检查版本 JSON、主 JAR 和已声明库文件，不执行下载或写入。
        """
        target = self.resolve_instance(game_path, version_id)
        json_path = target.instance_path / f"{target.instance_directory_name}.json"
        problems: list[dict[str, Any]] = []
        if not json_path.is_file():
            problems.append({"kind": "missing", "path": str(json_path), "size": 0})
            return {"issues": problems, "downloadBytes": 0, "canRepair": False}
        try:
            manifest = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return {
                "issues": [{"kind": "invalid", "path": str(json_path), "message": str(exc)}],
                "downloadBytes": 0,
                "canRepair": False,
            }
        jar_id = str(manifest.get("jar") or manifest.get("id") or target.instance_directory_name)
        jar_path = target.minecraft_root_path / "versions" / jar_id / f"{jar_id}.jar"
        if not jar_path.is_file():
            downloads = manifest.get("downloads") if isinstance(manifest.get("downloads"), dict) else {}
            client = downloads.get("client") if isinstance(downloads.get("client"), dict) else {}
            problems.append({"kind": "missing", "path": str(jar_path), "size": int(client.get("size") or 0)})
        for library in manifest.get("libraries") or []:
            artifact = ((library.get("downloads") or {}).get("artifact") or {}) if isinstance(library, dict) else {}
            relative = artifact.get("path")
            if not isinstance(relative, str):
                continue
            path = target.minecraft_root_path / "libraries" / relative
            if not path.is_file():
                problems.append({"kind": "missing", "path": str(path), "size": int(artifact.get("size") or 0)})
            elif artifact.get("size") and path.stat().st_size != int(artifact["size"]):
                problems.append({"kind": "damaged", "path": str(path), "size": int(artifact["size"])})
        return {
            "issues": problems,
            "downloadBytes": sum(int(item.get("size") or 0) for item in problems),
            "canRepair": True,
        }

    def repair_instance_files(self, game_path: Any, version_id: Any, source: Any = "official") -> dict[str, str]:
        """
        在用户确认只读清单后，异步调用 Core 文件补全能力。
        """
        target = self.resolve_instance(game_path, version_id)
        normalized_source = self._normalize_source(source)

        def worker(context: OperationContext) -> dict[str, Any]:
            context.progress(5, "正在准备文件补全")
            context.check_cancelled()
            core = self._context(target.minecraft_root_path, normalized_source)
            # Core 检查器在修复阶段允许写入；扫描阶段由 inspect_instance_files 保证纯只读。
            import asyncio

            download_list = core.files_checker.check_files(target.minecraft_root_path, target.instance_directory_name)
            context.check_cancelled()
            if download_list:
                downloader = self._downloader_factory(
                    download_list,
                    progress_callback=lambda done, total: context.progress(
                        10 + (done * 85 / total if total else 0), "正在补全实例文件"
                    ),
                )
                asyncio.run(downloader.run())
                failed_entries = set(downloader.failed_entries)
                if failed_entries:
                    context.check_cancelled()
                    fallback_entries = self._fallback_download_entries(
                        target.minecraft_root_path,
                        target.instance_directory_name,
                        normalized_source,
                        failed_entries,
                        getattr(downloader, "local_failed_paths", set()),
                    )
                    if fallback_entries:
                        completed_count = len(downloader.completed_entries)
                        context.progress(10 + completed_count * 85 / len(download_list), "正在尝试备用下载源")
                        fallback_downloader = self._downloader_factory(
                            fallback_entries,
                            progress_callback=lambda done, total: context.progress(
                                10 + (completed_count + done) * 85 / len(download_list), "正在从备用源补全实例文件"
                            ),
                        )
                        asyncio.run(fallback_downloader.run())
                        fallback_failed_paths = {file_path for _, file_path in fallback_downloader.failed_entries}
                        recovered_paths = {file_path for _, file_path in fallback_entries} - fallback_failed_paths
                        failed_entries = {
                            (url, file_path) for url, file_path in failed_entries if file_path not in recovered_paths
                        }
                context.check_cancelled()
                if failed_entries:
                    raise GameServiceError(
                        f"有 {len(failed_entries)} 个文件补全失败",
                        "GAME_DOWNLOAD_FAILED",
                    )
            context.progress(100, "文件补全完成")
            return {"versionId": target.instance_directory_name, "repaired": len(download_list)}

        return self._application_operations.submit("instance_repair", worker)

    def operation_get(self, operation_id: str) -> dict[str, Any]:
        return self._application_operations.get(operation_id)

    def operation_cancel(self, operation_id: str) -> bool:
        return self._application_operations.cancel(operation_id)
