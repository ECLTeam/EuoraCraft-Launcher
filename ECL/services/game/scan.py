# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例扫描协调器：版本目录扫描、ECL 配置读写与版本目录查询。
#
# 公开接口：
#   - class ScanCoordinator
#       - scan_versions(paths, force, compatibility_options) -> list[dict[str, Any]] — 扫描 Minecraft 目录；目录未变化时复用缓存结果。
#       - read_ecl_config(game_path) -> dict[str, Any] — 读取指定游戏路径下的 ecl.json，文件不存在或损坏时返回空字典。
#       - write_ecl_config(game_path, data) -> None — 写入 ecl.json 到指定游戏路径。
#       - patch_ecl_config(game_path, patch) -> dict[str, Any] — 合并更新 ecl.json 中的部分字段，返回更新后的完整配置。
#       - get_active_version(game_path) -> str | None — 从 ecl.json 读取当前路径下的启动版本；没有则返回 None。
#       - set_active_version(game_path, version_id) -> None — 把当前路径的启动版本写入 ecl.json。
#       - read_version_settings(game_path, version_id) -> dict[str, Any] — 读取版本目录中的独立启动设置（``.ecl/settings.json``）。
#       - write_version_settings(game_path, version_id, data) -> dict[str, Any] — 原子写入版本目录中的独立启动设置。
#       - scan_java(user_java_paths=…) -> list[dict[str, Any]] — 扫描 Java 运行时。
#       - minecraft_versions_classified(source=…) -> dict[str, list[dict[str, Any]]] — 查询并按正式版、快照和旧版本分类 Minecraft 版本。
#       - minecraft_versions(filter_type=…, source=…) -> list[dict[str, Any]] — 查询 Minecraft 版本，可按版本类别过滤。
#       - loader_versions(loader_type, game_version, source=…) -> list[str] — 查询指定游戏版本兼容的加载器版本。
#       - fabric_api_versions(game_version) -> list[str] — 查询指定 Minecraft 版本可用的 Fabric API 版本。
# ============================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Any

import httpx
from pydantic import JsonValue

from ECL.game import InstanceInspection
from ECL.utils import ConfigError, atomic_write_text

from .base import GameServiceError, VersionScanError, _GameState
from .launch_settings import InstanceLaunchOverrides
from .mod_sources import mod_api_base, mod_user_agent
from .workspace import resolve_instance_target


@dataclass(frozen=True, slots=True)
class _JavaInstallation:
    executable_path: str
    full_version: str
    major_version: int
    vendor: str
    runtime_kind: str
    architecture: str
    sources: tuple[str, ...]

    def to_protocol(self) -> dict[str, JsonValue]:
        """
        返回数据时保留旧字段名，并分别给出供应商和运行时种类。
        """
        return {
            "path": self.executable_path,
            "executable_path": self.executable_path,
            "version": self.full_version,
            "major_version": self.major_version,
            "java_type": self.vendor or self.runtime_kind,
            "vendor": self.vendor,
            "runtime_kind": self.runtime_kind,
            "arch": self.architecture,
            "architecture": self.architecture,
            "sources": list(self.sources),
        }


class ScanCoordinator(_GameState):
    isolation_policies = frozenset({"disabled", "modded_only", "non_release_only", "modded_or_non_release", "all"})
    default_isolation_policy = "all"
    non_release_version_types = frozenset({"snapshot", "april_fools", "old_alpha", "old_beta"})
    fabric_api_project = "fabric-api"
    fabric_api_timeout_seconds = 10

    @staticmethod
    def _normalize_scan_paths(value: Any, *, preserve_aliases: bool = False) -> list[Path]:
        """
        校验扫描根目录，并按实际解析路径合并可确认的别名。

        内部扫描可保留原始别名，以便在响应中说明哪些输入路径属于同一根目录。
        不进行无条件大小写转换，无法确认的目录继续区分。
        """
        if isinstance(value, (str, Path)):
            raw_paths = [value]
        elif isinstance(value, list):
            raw_paths = value
        else:
            raise VersionScanError("实例路径必须是字符串或字符串数组", "INVALID_GAME_PATH")

        paths: list[Path] = []
        seen: set[str] = set()
        for raw_path in raw_paths:
            if not isinstance(raw_path, (str, Path)):
                raise VersionScanError("实例路径数组只能包含字符串", "INVALID_GAME_PATH")
            path_value = str(raw_path).strip()
            if not path_value:
                continue
            path = Path(path_value).expanduser()
            if path.name.casefold() == "versions":
                path = path.parent
            path_key = str(path.resolve(strict=False))
            if path_key in seen and not preserve_aliases:
                continue
            seen.add(path_key)
            paths.append(path)
        if not paths:
            raise VersionScanError("至少需要一个有效的实例路径", "INVALID_GAME_PATH")
        return paths

    @staticmethod
    def _normalize_scanned_loader(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            return "Vanilla"
        return value.strip()

    @staticmethod
    def _normalize_scanned_version(
        game_path: Path,
        version_name: str,
        raw_info: Any,
    ) -> dict[str, Any]:
        info = raw_info if isinstance(raw_info, dict) else {}
        version_path_value = info.get("VersionPath")
        version_path = (
            Path(version_path_value)
            if isinstance(version_path_value, str) and version_path_value.strip()
            else game_path / "versions" / version_name
        )
        json_path = version_path / f"{version_name}.json"
        vanilla_version = info.get("VanillaVersion")
        vanilla_name = (
            vanilla_version.strip()
            if isinstance(vanilla_version, str) and vanilla_version.strip() and vanilla_version != "Unknown"
            else version_name
        )
        version_type = str(info.get("VanillaType") or "").strip() or "release"
        primary_loader = ScanCoordinator._normalize_scanned_loader(info.get("LoaderType"))
        inspection = InstanceInspection.inspect(game_path, version_name)
        components = dict(inspection.components)
        if not components and primary_loader != "Vanilla":
            components[primary_loader] = str(info.get("LoaderVersion") or "")
        if primary_loader == "Vanilla" and components:
            primary_loader = next(iter(components))
        component_keys = {name.casefold() for name in components}
        loader_version = str(info.get("LoaderVersion") or "").strip()
        if loader_version == "Unknown":
            loader_version = ""
        loader_version = components.get(primary_loader) or loader_version
        required_java_value = str(info.get("RequestJava") or "").strip()
        required_java = int(required_java_value) if required_java_value.isdigit() else None
        if required_java is None:
            for document in inspection.documents:
                java_version = document.get("javaVersion")
                major = java_version.get("majorVersion") if isinstance(java_version, dict) else None
                if isinstance(major, int) and not isinstance(major, bool) and major > 0:
                    required_java = major
                    break
        target = resolve_instance_target(game_path, version_name)
        return {
            "rootKey": target.root_key,
            "instanceKey": target.instance_key,
            "id": version_name,
            "versionId": version_name,
            "versionType": version_type,
            "path": str(game_path),
            "displayName": version_name,
            "primaryLoader": primary_loader,
            "loaderVersion": loader_version,
            "vanillaName": vanilla_name,
            "requiredJava": required_java,
            "installedComponents": [{"name": name, "version": version} for name, version in components.items()],
            "hasForge": "forge" in component_keys,
            "hasNeoForge": bool(component_keys & {"neoforged", "neoforge"}),
            "hasFabric": bool(component_keys & {"fabric", "legacyfabric", "babric"}),
            "hasQuilt": "quilt" in component_keys,
            "hasOptiFine": "optifine" in component_keys,
            "health": inspection.to_health(),
            "isBroken": not inspection.to_health()["canLaunch"],
            "jsonPath": str(json_path),
            "sourceName": game_path.name or str(game_path),
        }

    @staticmethod
    def _version_path_key(game_path: Path) -> str:
        return str(game_path.resolve(strict=False))

    @staticmethod
    def _version_metadata_snapshot(version_directory: Path) -> list[tuple[str, int, int]]:
        records: list[tuple[str, int, int]] = []
        candidates = list(version_directory.glob("*.json"))
        candidates.extend(version_directory.glob("*.jar"))
        candidates.extend(
            (
                version_directory / "eclversion.json",
                version_directory / ".ecl" / "instance.json",
                version_directory / "PCL" / "Setup.ini",
                version_directory / "PCL" / "Logo.png",
                version_directory / ".hmcl" / "config" / "instance-game-settings.json",
            )
        )
        for pattern in ("icon.*", ".ecl/icon.*"):
            candidates.extend(version_directory.glob(pattern))
        seen: set[str] = set()
        for candidate in candidates:
            relative_path = str(candidate.relative_to(version_directory))
            if relative_path in seen or not candidate.is_file():
                continue
            seen.add(relative_path)
            try:
                stat = candidate.stat()
            except OSError:
                continue
            records.append((relative_path, stat.st_mtime_ns, stat.st_size))
        return records

    def _version_directory_snapshot(
        self,
        game_path: Path,
        compatibility_options: dict[str, Any] | None = None,
    ) -> tuple[tuple[str, int, int], ...]:
        # 主 Jar 的增删和替换也会改变实例健康状态。
        versions_path = game_path / "versions"
        records: list[tuple[str, int, int]] = []

        def append_stat(relative_path: str, path: Path) -> None:
            try:
                stat = path.stat()
            except OSError:
                return
            records.append((relative_path, stat.st_mtime_ns, stat.st_size))

        if not versions_path.is_dir():
            return ()
        append_stat(".", versions_path)
        try:
            version_directories = [entry for entry in versions_path.iterdir() if entry.is_dir()]
        except OSError:
            return tuple(records)
        for version_directory in version_directories:
            append_stat(f"{version_directory.name}/", version_directory)
            try:
                for relative_path, modified_ns, size in ScanCoordinator._version_metadata_snapshot(version_directory):
                    records.append((f"{version_directory.name}/{relative_path}", modified_ns, size))
            except OSError:
                continue
        records.append(("@compat/registry", self._instance_profiles.compatibility_revision, 0))
        for source, external_path in self._instance_profiles.compatibility_watch_paths(compatibility_options):
            append_stat(f"@compat/{source}/{external_path.as_posix()}", external_path)
        return tuple(sorted(records))

    def _watch_version_path(
        self,
        game_path: Path,
        compatibility_options: dict[str, Any] | None = None,
    ) -> str:
        key = self._version_path_key(game_path)
        options = deepcopy(compatibility_options or {})
        snapshot = self._version_directory_snapshot(game_path, options)
        thread: Thread | None = None
        with self._lock:
            previous_snapshot = self._version_watch_snapshots.get(key)
            self._version_watch_paths[key] = game_path
            self._version_watch_compatibility_options[key] = options
            self._version_watch_snapshots[key] = snapshot
            if previous_snapshot is not None and previous_snapshot != snapshot:
                self._version_scan_cache.pop(key, None)
                self._version_watch_pending[key] = monotonic()
            if self._version_watcher_enabled and (
                self._version_watch_thread is None or not self._version_watch_thread.is_alive()
            ):
                self._version_watch_stop.clear()
                thread = Thread(target=self._version_watch_loop, name="ECL-VersionWatcher", daemon=True)
                self._version_watch_thread = thread
        if thread is not None:
            thread.start()
        return key

    def _poll_version_changes(self, now: float | None = None) -> list[str]:
        current_time = monotonic() if now is None else now
        with self._lock:
            watched_paths = list(self._version_watch_paths.items())

        changed_paths: list[str] = []
        for key, game_path in watched_paths:
            with self._lock:
                compatibility_options = self._version_watch_compatibility_options.get(key)
            snapshot = self._version_directory_snapshot(game_path, compatibility_options)
            with self._lock:
                if key not in self._version_watch_paths:
                    continue
                previous_snapshot = self._version_watch_snapshots.get(key)
                if previous_snapshot != snapshot:
                    self._version_watch_snapshots[key] = snapshot
                    self._version_scan_cache.pop(key, None)
                    self._version_watch_pending[key] = current_time
                pending_since = self._version_watch_pending.get(key)
                if pending_since is None or current_time - pending_since < self._version_watch_debounce:
                    continue
                self._version_watch_pending.pop(key, None)
                changed_paths.append(str(game_path.resolve(strict=False)))

        for game_path in changed_paths:
            self.events.emit("game:versions_changed", {"gamePath": game_path})
        return changed_paths

    def _version_watch_loop(self) -> None:
        while not self._version_watch_stop.wait(self._version_watch_interval):
            try:
                self._poll_version_changes()
            except Exception:
                self.logger.exception("监视 Minecraft 版本目录失败")

    @staticmethod
    def _has_instance_marker(version_directory: Path) -> bool:
        """
        判断版本目录是否带实例标志，避免把无关目录当作实例。

        只有存在主 Jar，或存在非空的 ``<目录名>.json`` 时才视为实例目录；
        ``logs``、``mods`` 等运行期目录以及 0 字节的同名 JSON 都会被排除，
        而 JSON 损坏但非空、或只有主 Jar 的目录仍会被保留以继续诊断。

        :param version_directory: ``versions/`` 下的候选子目录
        :return: 是否具备实例标志
        """
        name = version_directory.name
        if (version_directory / f"{name}.jar").is_file():
            return True
        manifest_path = version_directory / f"{name}.json"
        try:
            return manifest_path.is_file() and manifest_path.stat().st_size > 0
        except OSError:
            return False

    def _scan_game_path(
        self,
        game_path: Path,
        compatibility_options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        versions_path = game_path / "versions"
        if not versions_path.is_dir():
            self.logger.debug("跳过不存在的版本目录: %s", versions_path)
            return []
        try:
            versions = self._search_factory(game_path).search_minecraft()
        except (OSError, TypeError, ValueError) as exc:
            self.logger.exception("扫描 Minecraft 版本失败: %s", game_path)
            raise VersionScanError(f"扫描游戏目录失败: {game_path}: {exc}") from exc
        if not isinstance(versions, dict):
            raise VersionScanError(f"版本扫描器返回了无效数据: {game_path}")
        versions = dict(versions)
        for directory in versions_path.iterdir():
            if (
                directory.is_dir()
                and not directory.name.startswith(".")
                and directory.resolve().parent == versions_path.resolve()
                and self._has_instance_marker(directory)
            ):
                versions.setdefault(directory.name, {})
        normalized_versions = [
            self._normalize_scanned_version(game_path, version_name.strip(), info)
            for version_name, info in versions.items()
            if isinstance(version_name, str) and version_name.strip()
        ]
        enriched_versions: list[dict[str, Any]] = []
        for version in normalized_versions:
            version_id = str(version.get("versionId") or "").strip()
            if version_id:
                self._version_stats.ensure(game_path, version_id)
                try:
                    version = self._instance_profiles.enrich_version(
                        game_path,
                        version,
                        compatibility_options=compatibility_options,
                    )
                except (OSError, TypeError, ValueError):
                    self.logger.exception("合并实例资料失败: %s/%s", game_path, version_id)
            enriched_versions.append(version)
        return enriched_versions

    def scan_versions(
        self,
        paths: Any,
        *,
        force: bool = False,
        compatibility_options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        扫描 Minecraft 目录；目录未变化时复用缓存结果。

        :param paths: 用户指定的扫描路径列表
        :param force: 是否忽略有效缓存并重新扫描
        :param compatibility_options: 按来源标识分组的插件兼容配置
        """
        scanned_versions: list[dict[str, Any]] = []
        requested_paths_by_root_key: dict[str, list[str]] = {}
        path_by_root_key: dict[str, Path] = {}
        for requested_path in self._normalize_scan_paths(paths, preserve_aliases=True):
            root_key = self._version_path_key(requested_path)
            path_by_root_key.setdefault(root_key, requested_path)
            aliases = requested_paths_by_root_key.setdefault(root_key, [])
            if str(requested_path) not in aliases:
                aliases.append(str(requested_path))
        normalized_paths = list(path_by_root_key.values())
        self.logger.debug(
            "开始扫描版本目录，共 %d 个路径，强制刷新：%s", len(normalized_paths), ("是" if force else "否")
        )
        for game_path in normalized_paths:
            key = self._watch_version_path(game_path, compatibility_options)
            # 确保每个游戏路径下都有 ecl.json
            self._ensure_ecl_config(game_path)
            with self._lock:
                cached_versions = None if force else self._version_scan_cache.get(key)
            if cached_versions is None:
                self.logger.debug("扫描版本目录: %s", game_path)
                versions = self._scan_game_path(game_path, compatibility_options)
                with self._lock:
                    self._version_scan_cache[key] = deepcopy(versions)
                    self._version_watch_snapshots[key] = self._version_directory_snapshot(
                        game_path, compatibility_options
                    )
                    self._version_watch_pending.pop(key, None)
            else:
                self.logger.debug("扫描版本目录: %s，共 %d 个版本", game_path, len(cached_versions))
                versions = deepcopy(cached_versions)
            scanned_versions.extend(
                {**version, "rootAliases": list(requested_paths_by_root_key[key])} for version in versions
            )
        self.logger.debug("版本扫描完成，共 %d 个实例", len(scanned_versions))
        return sorted(
            scanned_versions,
            key=lambda item: (str(item["sourceName"]).casefold(), str(item["displayName"]).casefold()),
        )

    def _ecl_json_path(self, game_path: Any) -> Path:
        path = self._normalize_game_path(game_path)
        return path / self.ecl_json_name

    def _ensure_ecl_config(self, game_path: Any) -> None:
        # 如果游戏路径下不存在 ecl.json，则创建默认配置文件。
        ecl_path = self._ecl_json_path(game_path)
        with self._lock:
            if ecl_path.is_file():
                return
            try:
                ecl_path.parent.mkdir(parents=True, exist_ok=True)
                default_config = {
                    "activeVersion": "",
                    "lastLaunched": "",
                }
                atomic_write_text(ecl_path, json.dumps(default_config, ensure_ascii=False, indent=2))
                self.logger.debug("初始化默认 ecl.json: %s", ecl_path)
            except OSError as exc:
                self.logger.warning("初始化 ecl.json 失败 %s: %s", ecl_path, exc)

    def read_ecl_config(self, game_path: Any) -> dict[str, Any]:
        """
        读取指定游戏路径下的 ecl.json，文件不存在或损坏时返回空字典。

        :param game_path: Minecraft 游戏根目录
        """
        ecl_path = self._ecl_json_path(game_path)
        if not ecl_path.is_file():
            self.logger.debug("ecl.json 不存在，返回空配置: %s", ecl_path)
            return {}
        try:
            raw = ecl_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            result = data if isinstance(data, dict) else {}
            self.logger.debug(
                "读取 ecl.json 成功：%s，活动版本：%s", ("是" if ecl_path else "否"), result.get("activeVersion", "")
            )
            return result
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self.logger.warning("读取 ecl.json 失败 %s: %s", ecl_path, exc)
            return {}

    def write_ecl_config(self, game_path: Any, data: dict[str, Any]) -> None:
        """
        写入 ecl.json 到指定游戏路径。

        :param game_path: Minecraft 游戏根目录
        :param data: 需要处理或持久化的数据
        """
        if not isinstance(data, dict):
            raise GameServiceError("ecl.json 数据必须是字典", "INVALID_ECL_CONFIG")
        ecl_path = self._ecl_json_path(game_path)
        try:
            with self._lock:
                if ecl_path.is_file():
                    try:
                        existing = json.loads(ecl_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError, UnicodeDecodeError):
                        existing = None
                    if existing == data:
                        self.logger.debug("跳过未变化的 ecl.json 写入: %s", ecl_path)
                        return
                self.logger.debug("写入 ecl.json：%s，活动版本：%s", ecl_path, data.get("activeVersion", ""))
                ecl_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(ecl_path, json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as exc:
            raise GameServiceError(f"写入 ecl.json 失败: {exc}", "ECL_CONFIG_WRITE_FAILED") from exc

    def patch_ecl_config(self, game_path: Any, patch: dict[str, Any]) -> dict[str, Any]:
        """
        合并更新 ecl.json 中的部分字段，返回更新后的完整配置。

        :param game_path: Minecraft 游戏根目录
        :param patch: 需要合并到原配置的字段
        """
        if not isinstance(patch, dict):
            raise GameServiceError("ecl.json 增量数据必须是字典", "INVALID_ECL_CONFIG")
        # 必须把读取、合并和原子替换置于同一临界区；只锁单次写入仍会让并发 patch
        # 基于同一份旧数据生成两个结果，Windows 下还可能同时替换目标文件。
        with self._lock:
            current = self.read_ecl_config(game_path)
            changed = {key: value for key, value in patch.items() if current.get(key) != value}
            if not changed:
                self.logger.debug("跳过未变化的 ecl.json 增量更新: %s", self._ecl_json_path(game_path))
                return current
            self.logger.debug("增量更新 ecl.json：%s，字段：%s", self._ecl_json_path(game_path), list(changed.keys()))
            current.update(changed)
            self.write_ecl_config(game_path, current)
            return current

    def get_active_version(self, game_path: Any) -> str | None:
        """
        从 ecl.json 读取当前路径下的启动版本；没有则返回 None。

        :param game_path: Minecraft 游戏根目录
        """
        config = self.read_ecl_config(game_path)
        version_id = config.get("activeVersion") or config.get("active_version")
        result = str(version_id).strip() if isinstance(version_id, str) and version_id.strip() else None
        self.logger.debug("获取当前路径启动版本: %s -> %s", game_path, result)
        return result

    def set_active_version(self, game_path: Any, version_id: Any) -> None:
        """
        把当前路径的启动版本写入 ecl.json。

        :param game_path: Minecraft 游戏根目录
        :param version_id: Minecraft 版本或实例标识
        """
        name = self._normalize_version_name(version_id, "实例名称")
        self.logger.debug("设置当前路径启动版本: %s -> %s", game_path, name)
        self.patch_ecl_config(game_path, {"activeVersion": name})

    @staticmethod
    def _version_settings_path(game_path: Path, version_id: str) -> Path:
        return game_path / "versions" / version_id / ".ecl" / "settings.json"

    def read_version_settings(self, game_path: Any, version_id: Any) -> dict[str, Any]:
        """
        读取版本目录中的独立启动设置（``.ecl/settings.json``）。

        :param game_path: Minecraft 游戏根目录
        :param version_id: Minecraft 版本或实例标识
        :return: 该版本的启动设置；文件不存在或损坏时返回空字典
        """
        path = self._normalize_game_path(game_path)
        name = self._normalize_version_name(version_id, "实例名称")
        settings_path = self._version_settings_path(path, name)
        if not settings_path.is_file():
            return {}
        try:
            raw = settings_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self.logger.warning("读取版本独立设置失败 %s: %s", settings_path, exc)
            return {}

    def resolve_version_isolation(self, game_path: Any, version_id: Any, requested: Any = None) -> bool:
        """
        返回实例当前应使用的版本隔离状态。

        显式布尔值优先，其次是实例三态覆盖和旧版 ``isolated`` 字段；未指定时
        按全局策略及实例扫描元数据决定。保留显式布尔值，供受控调用和测试临时覆盖。

        :param game_path: Minecraft 游戏根目录
        :param version_id: Minecraft 版本或实例标识
        :param requested: 调用方临时指定的隔离状态
        :return: 当前实例最终应使用的隔离状态
        """
        if isinstance(requested, bool):
            return requested
        settings = self.read_version_settings(game_path, version_id)
        isolation_mode = settings.get("isolationMode")
        if isolation_mode == "enabled":
            return True
        if isolation_mode == "disabled":
            return False
        if isinstance(settings.get("isolated"), bool):
            return settings["isolated"]
        return self._matches_isolation_policy(game_path, version_id, self._isolation_policy())

    def _isolation_policy(self) -> str:
        """
        读取并校验全局隔离策略，配置异常时回退到稳定默认值。

        配置提供器可能访问磁盘或外部配置；读取异常和未知策略都不会传播到扫描
        流程，而是回退到启动器定义的默认隔离策略。
        """
        provider = self._isolation_policy_provider
        if provider is None:
            return self.default_isolation_policy
        try:
            value = provider()
        except (ConfigError, OSError, TypeError, ValueError):
            self.logger.exception("读取默认实例隔离策略失败")
            return self.default_isolation_policy
        if isinstance(value, str) and value in self.isolation_policies:
            return value
        return self.default_isolation_policy

    def _matches_isolation_policy(self, game_path: Any, version_id: Any, policy: str) -> bool:
        """
        按扫描元数据计算全局策略是否要求当前实例隔离。

        仅依据实例的加载器和发布渠道元数据判断，不修改版本设置或扫描缓存。
        """
        if policy == "disabled":
            return False
        if policy == "all":
            return True
        metadata = self._version_isolation_metadata(game_path, version_id)
        is_modded = str(metadata.get("primaryLoader") or "vanilla").casefold() != "vanilla"
        is_non_release = str(metadata.get("versionType") or "release").casefold() in self.non_release_version_types
        if policy == "modded_only":
            return is_modded
        if policy == "non_release_only":
            return is_non_release
        return is_modded or is_non_release

    def _version_isolation_metadata(self, game_path: Any, version_id: Any) -> dict[str, Any]:
        """
        从扫描缓存或本地扫描结果中读取隔离策略所需的最小版本元数据。

        优先使用缓存副本；缓存未命中时进行本地扫描。读取失败仅记录警告并返回
        空结果，让调用方按稳定回退规则处理。
        """
        path = self._normalize_game_path(game_path)
        name = self._normalize_version_name(version_id, "实例名称")
        key = self._version_path_key(path)
        with self._lock:
            cached = deepcopy(self._version_scan_cache.get(key, []))
        versions = cached
        if not versions:
            try:
                versions = self._scan_game_path(path)
            except (OSError, TypeError, ValueError, VersionScanError) as exc:
                self.logger.warning("读取实例隔离元数据失败 %s/%s: %s", path, name, exc)
                return {}
        for version in versions:
            if str(version.get("versionId") or version.get("id") or "") == name:
                return version
        return {}

    def write_version_settings(self, game_path: Any, version_id: Any, data: dict[str, Any]) -> dict[str, Any]:
        """
        原子写入版本目录中的独立启动设置。

        :param game_path: Minecraft 游戏根目录
        :param version_id: Minecraft 版本或实例标识
        :param data: 该版本的启动设置数据
        :return: 写入后的完整设置
        """
        if not isinstance(data, dict):
            raise GameServiceError("版本设置数据必须是字典", "INVALID_ECL_CONFIG")
        if data:
            data = InstanceLaunchOverrides.model_validate(data).model_dump(by_alias=True)
        path = self._normalize_game_path(game_path)
        name = self._normalize_version_name(version_id, "实例名称")
        settings_path = self._version_settings_path(path, name)
        try:
            with self._lock:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                if settings_path.is_file():
                    try:
                        existing = json.loads(settings_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError, UnicodeDecodeError):
                        existing = None
                    if isinstance(existing, dict):
                        if int(existing.get("schemaVersion", 1)) > 2:
                            raise GameServiceError("实例设置来自更高版本，不能覆盖", "SETTINGS_SCHEMA_UNSUPPORTED")
                        unknown = {
                            key: value
                            for key, value in existing.items()
                            if key
                            not in {field.alias or name for name, field in InstanceLaunchOverrides.model_fields.items()}
                            and key != "isolated"
                        }
                        data = {**unknown, **data}
                    if existing == data:
                        self.logger.debug("跳过未变化的版本独立设置写入: %s", settings_path)
                        return deepcopy(data)
                atomic_write_text(settings_path, json.dumps(data, ensure_ascii=False, indent=2))
                self.logger.debug("写入版本独立设置: %s", settings_path)
        except OSError as exc:
            raise GameServiceError(f"写入版本独立设置失败: {exc}", "ECL_CONFIG_WRITE_FAILED") from exc
        return deepcopy(data)

    @staticmethod
    def _java_major_version(version: Any) -> int:
        value = str(version or "").strip()
        if value.startswith("1."):
            value = value[2:]
        match = re.match(r"\d+", value)
        return int(match.group()) if match else 0

    def java_requirement(self, game_path: str, version_id: str) -> int | None:
        """
        从当前元数据和继承链读取 Java 要求，不采信旧界面快照。

        :param game_path: Minecraft 根目录
        :param version_id: 实例目录名
        :return: 已知最低主版本，无法确认时保留未知
        """
        target = self.resolve_instance(game_path, version_id)
        inspection = InstanceInspection.inspect(target.minecraft_root_path, target.instance_directory_name)
        for document in inspection.documents:
            java_version = document.get("javaVersion")
            major = java_version.get("majorVersion") if isinstance(java_version, dict) else None
            if isinstance(major, int) and not isinstance(major, bool) and major > 0:
                return major
        raw = self._search_factory(target.minecraft_root_path).search_minecraft()
        info = raw.get(target.instance_directory_name) if isinstance(raw, dict) else None
        normalized = self._normalize_scanned_version(target.minecraft_root_path, target.instance_directory_name, info)
        required = normalized.get("requiredJava")
        return required if isinstance(required, int) else self._fallback_required_java(normalized.get("vanillaName"))

    def scan_java(self, user_java_paths: list[str] | None = None) -> list[dict[str, Any]]:
        """
        扫描并缓存实际 Java 运行时，独立输出供应商、种类、版本与架构。

        更新服务持有的运行时快照，供后续自动选择使用；扫描器维护其磁盘
        缓存。旧 java_type 字段仅用于协议兼容，不再作为内部运行时种类。

        :param user_java_paths: 用户配置的 Java 搜索路径
        :return: 按主版本排序的运行时清单，缺失供应商保持为空
        """
        user_paths = [path for path in user_java_paths or [] if isinstance(path, str) and path.strip()]
        if self.java_manager is not None:
            inventory = self.java_manager.inventory(force=True, extra_paths=tuple(Path(path) for path in user_paths))
            from ECL.game import JavaRuntime

            self._java_runtimes = [
                JavaRuntime(
                    record.executable_path,
                    record.full_version,
                    record.vendor,
                    record.architecture,
                    record.runtime_kind == "JDK",
                )
                for record in inventory.runtimes
                if record.validation_status == "valid" and record.is_enabled
            ]
            return [
                {
                    **_JavaInstallation(
                        str(record.executable_path),
                        record.full_version,
                        record.major_version,
                        record.vendor,
                        record.runtime_kind,
                        record.architecture,
                        (
                            "user"
                            if str(record.executable_path) in user_paths or record.origin == "manual"
                            else record.origin,
                        ),
                    ).to_protocol(),
                    "runtime_id": record.runtime_id,
                }
                for record in inventory.runtimes
                if record.validation_status == "valid" and record.is_enabled
            ]
        self.logger.debug("开始扫描 Java 运行时，用户自定义路径: %s", user_paths)
        scanner = self._java_scanner_factory(
            cache_file=self._java_cache_file,
            user_java_paths=user_paths,
        )
        self._java_runtimes = scanner.scan()
        self.logger.debug("Java 扫描完成，共发现 %d 个运行时", len(self._java_runtimes))
        installations = []
        for runtime in self._java_runtimes:
            architecture = str(runtime.architecture or "unknown").lower()
            architecture = {
                "amd64": "x64",
                "x86_64": "x64",
                "aarch64": "arm64",
                "i386": "x86",
                "i686": "x86",
            }.get(architecture, architecture)
            path = str(runtime.path)
            installations.append(
                _JavaInstallation(
                    executable_path=path,
                    full_version=str(runtime.version),
                    major_version=self._java_major_version(runtime.version),
                    vendor=runtime.vendor or "",
                    runtime_kind="JDK" if runtime.is_jdk else "JRE",
                    architecture=architecture,
                    sources=("user" if path in user_paths else "system",),
                ).to_protocol()
            )
        return sorted(
            installations,
            key=lambda item: (-item["major_version"], item["java_type"].casefold(), item["path"].casefold()),
        )

    @staticmethod
    def _catalog_item(item: dict[str, Any], version_type: str) -> dict[str, Any]:
        return {
            "id": str(item.get("id") or ""),
            "type": version_type,
            "releaseTime": str(item.get("releaseTime") or ""),
        }

    def minecraft_versions_classified(self, source: Any = "official") -> dict[str, list[dict[str, Any]]]:
        """
        查询并按正式版、快照和旧版本分类 Minecraft 版本。

        :param source: 下载源名称，如 ``official`` 或 ``bmclapi``
        """
        raw = self._query_context(source).games.get_minecraft_versions()
        groups = {
            "release": "Release",
            "snapshot": "Snapshot",
            "april_fools": "FoolDays",
            "old_beta": "Beta",
            "old_alpha": "Alpha",
        }
        catalog: dict[str, list[dict[str, Any]]] = {"all": []}
        type_by_id: dict[str, str] = {}
        for output_name, core_name in groups.items():
            values = [
                self._catalog_item(item, output_name)
                for item in raw.get(core_name, [])
                if isinstance(item, dict) and item.get("id")
            ]
            catalog[output_name] = values
            type_by_id.update({item["id"]: output_name for item in values})
        catalog["all"] = [
            self._catalog_item(item, type_by_id.get(str(item.get("id") or ""), "release"))
            for item in raw.get("All", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return catalog

    def minecraft_versions(self, filter_type: Any = None, source: Any = "official") -> list[dict[str, Any]]:
        """
        查询 Minecraft 版本，可按版本类别过滤。

        :param filter_type: 版本目录筛选类型
        :param source: 下载源名称，如 ``official`` 或 ``bmclapi``
        """
        catalog = self.minecraft_versions_classified(source)
        key = str(filter_type or "all").strip().casefold().replace("-", "_")
        if key not in catalog:
            raise GameServiceError("未知的版本分类", "INVALID_VERSION_FILTER")
        return catalog[key]

    def loader_versions(self, loader_type: Any, game_version: Any, source: Any = "official") -> list[str]:
        """
        查询指定游戏版本兼容的加载器版本。

        :param loader_type: 模组加载器类型
        :param game_version: 目标 Minecraft 游戏版本
        :param source: 下载源名称，如 ``official`` 或 ``bmclapi``
        """
        loader = str(loader_type or "").strip().casefold()
        version = self._normalize_version_name(game_version, "Minecraft 版本")
        games = self._query_context(source).games
        if loader == "fabric":
            result = games.get_fabric_versions(version)
        elif loader == "forge":
            result = games.get_forge_versions(version)
        elif loader in {"neoforge", "neoforged"}:
            result = games.get_neoforged_versions(version)
        elif loader == "quilt":
            result = games.get_quilt_versions(version)
        else:
            raise GameServiceError(f"暂不支持加载器: {loader_type}", "UNSUPPORTED_LOADER")
        if isinstance(result, dict):
            values = result.get("All", result.get("all", []))
        else:
            values = result if isinstance(result, list) else []
        return [
            str(item.get("LoaderVersion") or "").strip()
            for item in values
            if isinstance(item, dict) and item.get("LoaderVersion")
        ]

    def fabric_api_versions(self, game_version: Any) -> list[str]:
        """
        查询指定 Minecraft 版本可用的 Fabric API 版本。

        Fabric API 属于 Modrinth 模组资源，跟随模组源请求，并在远端失败时
        切换到另一模组源重试。

        :param game_version: 目标 Minecraft 游戏版本
        :return: Fabric API 版本号列表，按发布时间降序
        :raises GameServiceError: 两个模组源均请求失败时抛出
        """
        version = self._normalize_version_name(game_version, "Minecraft 版本")
        params = {
            "game_versions": '["' + version + '"]',
            "loaders": '["fabric"]',
        }

        def fetch_versions(mod_source: str) -> list[Any]:
            response = httpx.get(
                f"{mod_api_base(mod_source, 'modrinth')}/project/{self.fabric_api_project}/version",
                params=params,
                headers={"User-Agent": mod_user_agent()},
                timeout=self.fabric_api_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []

        versions = self.mod_request(
            "Fabric API 版本",
            fetch_versions,
            is_valid=lambda payload: isinstance(payload, list),
        )
        return [
            str(item.get("version_number") or "").strip()
            for item in versions
            if isinstance(item, dict) and item.get("version_number")
        ]
