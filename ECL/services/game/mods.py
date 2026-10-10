# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：模组协调器：模组列表/开关/增删与图标处理。
#
# 公开接口：
#   - class ModCoordinator — 管理 Minecraft 根目录中的本地模组文件。
#       - list_mods(game_path) -> list[dict[str, Any]] — 列出目标 Minecraft 根目录中的 Jar 模组，并解析 jar 元数据。
#       - toggle_mod(game_path, filename) -> bool — 通过 ``.disabled`` 后缀切换模组启用状态。
#       - add_mod(game_path, source_path) -> str — 将本地 Jar 文件原子复制到目标 ``mods`` 目录。
#       - remove_mod(game_path, filename) -> None — 删除目标 ``mods`` 目录中的单个模组文件。
#       - mods_path(game_path) -> Path — 创建并返回目标 Minecraft 根目录中的 ``mods`` 目录。
# ============================================================

from __future__ import annotations

import base64
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ECL.game import LocalModParser, ModDependencyDiagnostics

from .base import GameServiceError, _GameState
from .resources import ResourceCoordinator, _sha512


def _icon_mime(filename: str) -> str:
    # 依据扩展名推断图标的 MIME 类型，未知时回退为 PNG。
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return mime.get(Path(filename).suffix.lower(), "image/png")


class ModCoordinator(_GameState):
    """
    管理 Minecraft 根目录中的本地模组文件。

    所有写操作都限制在目标 ``mods`` 目录内，并使用临时文件完成复制。
    """

    max_icon_bytes = 1024 * 1024

    def list_mods(self, game_path: Any) -> list[dict[str, Any]]:
        """
        列出目标 Minecraft 根目录中的 Jar 模组，并解析 jar 元数据。

        :param game_path: Minecraft 游戏根目录
        :return: 前端本地模组列表所需的完整信息
        """
        return self._list_mods_at(self._normalize_game_path(game_path))

    def _list_mods_at(
        self, data_path: Path, environment: dict[str, str | None] | None = None, manifest: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        直接从已确定的游戏数据目录列出模组，避免再次改变隔离目录路径。
        """
        mods_dir = data_path / "mods"
        if not mods_dir.is_dir():
            return []
        paths = [
            path
            for path in sorted(mods_dir.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and path.name.casefold().endswith((".jar", ".jar.disabled"))
        ]
        parsed_files = tuple(LocalModParser.parse(path) for path in paths)
        diagnostics = ModDependencyDiagnostics.evaluate(parsed_files, environment)
        result = []
        for path, parsed in zip(paths, parsed_files, strict=True):
            enabled = parsed.enabled
            metadata = parsed.legacy_summary()
            digest = _sha512(path)
            recorded = ResourceCoordinator._resource_source(self, manifest or {}, "mod", path.name, digest)
            original_name = str(metadata.get("name") or path.stem.removesuffix(".disabled"))
            mod_id = str(metadata.get("modId") or "")
            wiki_mod = self._mcmod.lookup_by_alias(mod_id, original_name, path.stem.removesuffix(".disabled"))
            wiki = self._mcmod.to_wiki_info(wiki_mod) if wiki_mod is not None else None
            result.append(
                {
                    "filename": path.name,
                    "name": original_name,
                    "display_name": (wiki or {}).get("title") or original_name,
                    "english_name": (wiki or {}).get("englishName") or original_name,
                    "mcmod_url": (wiki or {}).get("url") or "",
                    "version": metadata.get("version") or "",
                    "author": metadata.get("author") or "",
                    "loader_type": metadata.get("loader") or "",
                    "game_version": metadata.get("gameVersion") or "",
                    "project_id": mod_id,
                    "mod_id": mod_id,
                    "source": recorded.get("source", "local"),
                    "source_project_id": recorded.get("projectId"),
                    "source_version_id": recorded.get("versionId"),
                    "sha512": digest,
                    "declared_mod_ids": metadata["declaredModIds"],
                    "provided_mod_ids": metadata["providedModIds"],
                    "dependency_declarations": metadata["dependencyDeclarations"],
                    "diagnostics": diagnostics[path.name],
                    "dependencies": metadata.get("dependencies") or [],
                    "enabled": enabled,
                    "size": path.stat().st_size,
                    "icon_data": self._read_mod_icon(path, metadata.get("icon"), [mod_id, original_name]),
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                }
            )
        return result

    def toggle_mod(self, game_path: Any, filename: Any) -> bool:
        """
        通过 ``.disabled`` 后缀切换模组启用状态。

        :param game_path: Minecraft 游戏根目录
        :param filename: ``mods`` 目录内的文件名
        :return: 切换后的启用状态
        """
        source = self._mod_path_at(self._normalize_game_path(game_path), filename)
        if not source.is_file():
            raise GameServiceError("模组文件不存在", "MOD_NOT_FOUND")
        enabled = not source.name.endswith(".disabled")
        target_name = source.name.removesuffix(".disabled") if not enabled else f"{source.name}.disabled"
        target = self._mod_path_at(self._normalize_game_path(game_path), target_name)
        if target.exists():
            raise GameServiceError("目标模组文件已存在", "MOD_TARGET_EXISTS")
        source.replace(target)
        return not enabled

    def add_mod(self, game_path: Any, source_path: Any) -> str:
        """
        将本地 Jar 文件原子复制到目标 ``mods`` 目录。

        :param game_path: Minecraft 游戏根目录
        :param source_path: 用户选择的源 Jar 文件
        :return: 安装后的文件名
        """
        if not isinstance(source_path, (str, Path)) or not str(source_path).strip():
            raise GameServiceError("未选择模组文件", "INVALID_MOD_SOURCE")
        source = Path(source_path).expanduser().resolve(strict=False)
        if not source.is_file() or source.suffix.casefold() != ".jar":
            raise GameServiceError("模组源文件必须是 Jar 文件", "INVALID_MOD_SOURCE")
        target = self._mod_path_at(self._normalize_game_path(game_path), source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise GameServiceError(f"复制模组失败: {exc}", "MOD_COPY_FAILED") from exc
        return target.name

    def remove_mod(self, game_path: Any, filename: Any) -> None:
        """
        删除目标 ``mods`` 目录中的单个模组文件。

        :param game_path: Minecraft 游戏根目录
        :param filename: ``mods`` 目录内的文件名
        """
        target = self._mod_path_at(self._normalize_game_path(game_path), filename)
        if not target.is_file():
            raise GameServiceError("模组文件不存在", "MOD_NOT_FOUND")
        target.unlink()

    def _read_mod_icon(self, path: Path, icon: Any, fallback_ids: list[str]) -> str:
        """
        从 mod jar 中提取图标并编码为数据 URL。

        先读取元数据指定的图标，找不到时尝试常见文件名。

        :param path: 模组 Jar 文件路径
        :param icon: 元数据解析出的图标路径或其映射
        :param fallback_ids: 用于尝试常见图标路径的 mod ID 或名称
        :return: 图标的数据 URL，未找到或超限时为空字符串
        """
        candidates: list[str] = []
        if isinstance(icon, str) and icon:
            candidates.append(icon)
        for mod_id in fallback_ids:
            if not mod_id:
                continue
            candidates.append(f"assets/{mod_id}/icon.png")
            candidates.append(f"{mod_id}.png")
        candidates.append("icon.png")
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                for name in dict.fromkeys(candidates):
                    entry_name = name.lstrip("/")
                    entry = archive.getinfo(entry_name) if entry_name in names else None
                    if entry is None or entry.file_size <= 0 or entry.file_size > self.max_icon_bytes:
                        continue
                    data = archive.read(entry_name)
                    return f"data:{_icon_mime(entry_name)};base64,{base64.b64encode(data).decode('ascii')}"
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            pass
        return ""

    def mods_path(self, game_path: Any) -> Path:
        """
        创建并返回目标 Minecraft 根目录中的 ``mods`` 目录。

        :param game_path: Minecraft 游戏根目录
        :return: 绝对 ``mods`` 目录路径
        """
        return self._mods_path_at(self._normalize_game_path(game_path))

    @staticmethod
    def _mods_path_at(data_path: Path) -> Path:
        path = (data_path / "mods").resolve(strict=False)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _instance_mod_root(self, game_path: Any, version_id: Any, version_isolation: Any = None) -> Path:
        """
        解析实例实际数据目录，确保模组操作与游戏启动使用同一隔离语义。
        """
        isolated = self.resolve_version_isolation(game_path, version_id, version_isolation)
        return self.resolve_instance(game_path, version_id, isolated).game_data_path

    def list_instance_mods(
        self, game_path: Any, version_id: Any, version_isolation: Any = None
    ) -> list[dict[str, Any]]:
        """
        从实例实际数据目录读取模组、来源身份与启用状态诊断。

        解析与资源清单共用模型，来源证据失效或无法解析时仍保留本地文件。

        :param game_path: Minecraft 根目录
        :param version_id: 实例目录名
        :param version_isolation: 可选的版本隔离覆盖
        :return: 模组清单及不自动修改文件的诊断
        """
        return self._list_mods_at(
            self._instance_mod_root(game_path, version_id, version_isolation),
            ResourceCoordinator._mod_environment(self, game_path, version_id),
            ResourceCoordinator._read_resource_manifest(self, game_path, version_id),
        )

    def toggle_instance_mod(
        self, game_path: Any, version_id: Any, filename: Any, version_isolation: Any = None
    ) -> bool:
        data_path = self._instance_mod_root(game_path, version_id, version_isolation)
        source = self._mod_path_at(data_path, filename)
        if not source.is_file():
            raise GameServiceError("模组文件不存在", "MOD_NOT_FOUND")
        enabled = not source.name.endswith(".disabled")
        target_name = source.name.removesuffix(".disabled") if not enabled else f"{source.name}.disabled"
        target = self._mod_path_at(data_path, target_name)
        if target.exists():
            raise GameServiceError("目标模组文件已存在", "MOD_TARGET_EXISTS")
        source.replace(target)
        return not enabled

    def add_instance_mod(self, game_path: Any, version_id: Any, source_path: Any, version_isolation: Any = None) -> str:
        if not isinstance(source_path, (str, Path)) or not str(source_path).strip():
            raise GameServiceError("未选择模组文件", "INVALID_MOD_SOURCE")
        source = Path(source_path).expanduser().resolve(strict=False)
        if not source.is_file() or source.suffix.casefold() != ".jar":
            raise GameServiceError("模组源文件必须是 Jar 文件", "INVALID_MOD_SOURCE")
        target = self._mod_path_at(self._instance_mod_root(game_path, version_id, version_isolation), source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise GameServiceError(f"复制模组失败: {exc}", "MOD_COPY_FAILED") from exc
        return target.name

    def remove_instance_mod(
        self, game_path: Any, version_id: Any, filename: Any, version_isolation: Any = None
    ) -> None:
        target = self._mod_path_at(self._instance_mod_root(game_path, version_id, version_isolation), filename)
        if not target.is_file():
            raise GameServiceError("模组文件不存在", "MOD_NOT_FOUND")
        target.unlink()

    def instance_mods_path(self, game_path: Any, version_id: Any, version_isolation: Any = None) -> Path:
        return self._mods_path_at(self._instance_mod_root(game_path, version_id, version_isolation))

    @staticmethod
    def _mod_path_at(data_path: Path, filename: Any) -> Path:
        if not isinstance(filename, str) or not filename.strip() or "\0" in filename:
            raise GameServiceError("模组文件名无效", "INVALID_MOD_FILENAME")
        mods_dir = (data_path / "mods").resolve(strict=False)
        target = (mods_dir / filename).resolve(strict=False)
        if target.parent != mods_dir or target.name != filename:
            raise GameServiceError("模组路径超出允许范围", "INVALID_MOD_PATH")
        return target


__all__ = ["ModCoordinator"]
