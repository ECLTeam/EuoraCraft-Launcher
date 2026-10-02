# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：存档协调器：世界清单、详情修改、备份与导入导出。
#
# 公开接口：
#   - class WorldCoordinator — 管理实例实际游戏目录中的世界、备份与数据包元数据。
#       - list_worlds(game_path, version_id, version_isolation=…) -> list[dict[str, Any]] — 读取全部有效存档；损坏存档以错误状态返回而不是阻断列表。
#       - world_detail(game_path, version_id, world_id, version_isolation=…) -> dict[str, Any]
#       - patch_world(game_path, version_id, world_id, patch, version_isolation=…) -> dict[str, Any] — 备份后原子修改存档常用字段，保留所有未知 NBT 字段。
#       - copy_world(game_path, version_id, world_id, new_world_id, version_isolation=…) -> dict[str, str] — 异步复制世界目录，目标目录只允许安全相对名称。
#       - set_world_icon(game_path, version_id, world_id, source_path, version_isolation=…) -> dict[str, str] — 校验并原子写入世界图标，原始图片不被修改。
#       - delete_world(game_path, version_id, world_id, version_isolation=…) -> None
#       - export_world(game_path, version_id, world_id, output_path, version_isolation=…) -> dict[str, str]
#       - import_world(game_path, version_id, source_path, version_isolation=…) -> dict[str, str]
#       - create_world_backup(game_path, version_id, world_id, version_isolation=…, automatic) -> dict[str, Any] — 创建带元数据的 ZIP 备份并只保留最近十个未锁定备份。
#       - start_world_backup(game_path, version_id, world_id, version_isolation=…) -> dict[str, str] — 在统一长任务协调器中创建世界备份并返回任务标识。
#       - list_world_backups(game_path, version_id, world_id) -> list[dict[str, Any]]
#       - lock_world_backup(game_path, version_id, world_id, backup_id, locked) -> dict[str, Any]
#       - delete_world_backup(game_path, version_id, world_id, backup_id) -> None — 把指定备份及其元数据直接删除。
#       - restore_world_backup(game_path, version_id, world_id, backup_id, version_isolation=…) -> dict[str, str]
#       - world_quick_play_capability(game_path, version_id) -> dict[str, Any]
#       - chunkbase_url(game_path, version_id, world_id, version_isolation=…) -> dict[str, str]
# ============================================================

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from PIL import Image, UnidentifiedImageError

from ECL.utils import atomic_write_text
from ECL.utils.nbt import Byte, Compound, Int, Long, load

from .base import GameServiceError
from .operations import OperationContext
from .workspace import delete_path, resolve_relative_id, safe_extract_zip


def _nbt_scalar(value: Any, default: Any = None) -> Any:
    try:
        return value.unpack(json=True) if hasattr(value, "unpack") else value
    except Exception:
        return default


# 存档枚举字段：patch 键 → (NBT 键, 标签类型, 合法取值范围, 错误码, 提示文案)。
class WorldPatchPolicy:
    """
    保存允许修改的世界 NBT 字段规则。
    """

    enum_fields: dict[str, tuple[str, type, range, str, str]] = {
        "difficulty": ("Difficulty", Byte, range(4), "INVALID_WORLD_DIFFICULTY", "难度值无效"),
        "gameMode": ("GameType", Int, range(4), "INVALID_WORLD_GAMEMODE", "游戏模式值无效"),
    }
    # 存档布尔字段：patch 键 → NBT 键。
    boolean_fields: dict[str, str] = {
        "allowCommands": "allowCommands",
        "difficultyLocked": "DifficultyLocked",
        "raining": "raining",
        "thundering": "thundering",
    }


class WorldCoordinator:
    """
    管理实例实际游戏目录中的世界、备份与数据包元数据。
    """

    def _world_root(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> Path:
        return self.resolve_instance(game_path, version_id, version_isolation).data_path / "saves"

    def _world_path(self, game_path: Any, version_id: Any, world_id: Any, version_isolation: Any = False) -> Path:
        root = self._world_root(game_path, version_id, version_isolation)
        return resolve_relative_id(root, world_id)

    @staticmethod
    def _read_world(world_path: Path) -> dict[str, Any]:
        level_path = world_path / "level.dat"
        if not level_path.is_file():
            raise GameServiceError("存档缺少 level.dat", "WORLD_LEVEL_DAT_MISSING")
        try:
            root = load(level_path)
            data = root.get("Data", root)
        except Exception as exc:
            raise GameServiceError(f"读取世界数据失败：{exc}", "WORLD_NBT_INVALID") from exc
        game_type = int(_nbt_scalar(data.get("GameType"), 0) or 0)
        difficulty = int(_nbt_scalar(data.get("Difficulty"), 2) or 0)
        version = data.get("Version") or {}
        version_name = _nbt_scalar(version.get("Name"), "未知") if hasattr(version, "get") else "未知"
        last_played = int(_nbt_scalar(data.get("LastPlayed"), 0) or 0)
        modified = world_path.stat().st_mtime
        return {
            "id": world_path.name,
            "name": str(_nbt_scalar(data.get("LevelName"), world_path.name) or world_path.name),
            "path": str(world_path),
            "iconPath": str(world_path / "icon.png") if (world_path / "icon.png").is_file() else None,
            "gameMode": {0: "生存", 1: "创造", 2: "冒险", 3: "旁观"}.get(game_type, "未知"),
            "gameModeId": game_type,
            "difficulty": {0: "和平", 1: "简单", 2: "普通", 3: "困难"}.get(difficulty, "未知"),
            "difficultyId": difficulty,
            "difficultyLocked": bool(_nbt_scalar(data.get("DifficultyLocked"), False)),
            "allowCommands": bool(_nbt_scalar(data.get("allowCommands"), False)),
            "version": str(version_name),
            "seed": str(_nbt_scalar(data.get("RandomSeed"), "")),
            "spawn": {
                "x": int(_nbt_scalar(data.get("SpawnX"), 0) or 0),
                "y": int(_nbt_scalar(data.get("SpawnY"), 0) or 0),
                "z": int(_nbt_scalar(data.get("SpawnZ"), 0) or 0),
            },
            "weather": {
                "raining": bool(_nbt_scalar(data.get("raining"), False)),
                "thundering": bool(_nbt_scalar(data.get("thundering"), False)),
            },
            "lastPlayedAt": datetime.fromtimestamp(last_played / 1000, UTC).isoformat() if last_played else None,
            "modifiedAt": datetime.fromtimestamp(modified, UTC).isoformat(),
            "createdAt": datetime.fromtimestamp(world_path.stat().st_ctime, UTC).isoformat(),
        }

    def list_worlds(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> list[dict[str, Any]]:
        """
        读取全部有效存档；损坏存档以错误状态返回而不是阻断列表。
        """
        root = self._world_root(game_path, version_id, version_isolation)
        if not root.is_dir():
            return []
        worlds: list[dict[str, Any]] = []
        for path in root.iterdir():
            if not path.is_dir():
                continue
            try:
                worlds.append(self._read_world(path))
            except GameServiceError as exc:
                worlds.append({"id": path.name, "name": path.name, "path": str(path), "error": str(exc)})
        return sorted(worlds, key=lambda item: item.get("modifiedAt") or "", reverse=True)

    def world_detail(
        self, game_path: Any, version_id: Any, world_id: Any, version_isolation: Any = False
    ) -> dict[str, Any]:
        return self._read_world(self._world_path(game_path, version_id, world_id, version_isolation))

    def _assert_world_writable(self, target: Any, world_path: Path) -> None:
        game_key = str(target.game_path).casefold()
        if any(
            str(item.get("gamePath") or "").casefold() == game_key and item.get("versionId") == target.version_id
            for item in self.list_instances()
        ):
            raise GameServiceError("游戏运行时不能修改存档", "INSTANCE_IS_RUNNING")
        lock_path = world_path / "session.lock"
        if lock_path.is_file():
            try:
                with lock_path.open("r+b"):
                    pass
            except OSError as exc:
                raise GameServiceError("存档正在被占用", "WORLD_IS_LOCKED") from exc

    @staticmethod
    def _atomic_save_nbt(document: Any, destination: Path) -> None:
        temp = destination.with_name(f".{destination.name}.ecl-tmp")
        try:
            document.save(temp, gzipped=True)
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)

    def patch_world(
        self,
        game_path: Any,
        version_id: Any,
        world_id: Any,
        patch: dict[str, Any],
        version_isolation: Any = False,
    ) -> dict[str, Any]:
        """
        备份后原子修改存档常用字段，保留所有未知 NBT 字段。
        """
        target = self.resolve_instance(game_path, version_id, version_isolation)
        world_path = self._world_path(game_path, version_id, world_id, version_isolation)
        self._assert_world_writable(target, world_path)
        self.create_world_backup(game_path, version_id, world_id, version_isolation, automatic=True)
        level_path = world_path / "level.dat"
        try:
            document = load(level_path)
            data = document.get("Data", document)
            self._apply_world_patch(data, patch)
            self._atomic_save_nbt(document, level_path)
        except GameServiceError:
            raise
        except Exception as exc:
            raise GameServiceError(f"修改世界数据失败：{exc}", "WORLD_UPDATE_FAILED") from exc
        return self._read_world(world_path)

    @staticmethod
    def _apply_world_patch(data: Compound, patch: dict[str, Any]) -> None:
        # 校验并写入存档常用字段；非法取值在落盘前被拒绝。
        for field, (key, tag, valid, code, message) in WorldPatchPolicy.enum_fields.items():
            if field in patch:
                value = int(patch[field])
                if value not in valid:
                    raise GameServiceError(message, code)
                data[key] = tag(value)
        for field, key in WorldPatchPolicy.boolean_fields.items():
            if field in patch:
                data[key] = Byte(1 if patch[field] else 0)
        if "seed" in patch:
            seed = patch["seed"]
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise GameServiceError("种子必须是整数", "INVALID_WORLD_SEED")
            data["RandomSeed"] = Long(seed)
        if "spawn" in patch:
            spawn = patch["spawn"]
            if not isinstance(spawn, dict) or any(
                not isinstance(spawn.get(axis), int) or isinstance(spawn.get(axis), bool) for axis in ("x", "y", "z")
            ):
                raise GameServiceError("出生点坐标必须是整数", "INVALID_WORLD_SPAWN")
            if any(not -30000000 <= int(spawn[axis]) <= 30000000 for axis in ("x", "y", "z")):
                raise GameServiceError("出生点坐标超出世界范围", "INVALID_WORLD_SPAWN")
            data["SpawnX"] = Int(spawn["x"])
            data["SpawnY"] = Int(spawn["y"])
            data["SpawnZ"] = Int(spawn["z"])

    def copy_world(
        self, game_path: Any, version_id: Any, world_id: Any, new_world_id: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        """
        异步复制世界目录，目标目录只允许安全相对名称。
        """
        root = self._world_root(game_path, version_id, version_isolation)
        source = resolve_relative_id(root, world_id)
        destination = resolve_relative_id(root, new_world_id, must_exist=False)
        if destination.exists():
            raise GameServiceError("目标世界目录已存在", "WORLD_ALREADY_EXISTS")

        def worker(context: OperationContext) -> dict[str, str]:
            context.progress(10, "正在复制存档")
            shutil.copytree(source, destination)
            context.check_cancelled()
            context.progress(100, "存档复制完成")
            return {"worldId": destination.name}

        return self._game_operations.submit("world_copy", worker)

    def set_world_icon(
        self,
        game_path: Any,
        version_id: Any,
        world_id: Any,
        source_path: Any,
        version_isolation: Any = False,
    ) -> dict[str, str]:
        """
        校验并原子写入世界图标，原始图片不被修改。
        """
        target = self.resolve_instance(game_path, version_id, version_isolation)
        world = self._world_path(game_path, version_id, world_id, version_isolation)
        self._assert_world_writable(target, world)
        source = Path(str(source_path)).expanduser().resolve(strict=True)
        try:
            with Image.open(source) as image:
                image.thumbnail((64, 64))
                converted = image.convert("RGBA")
                temp = world / ".icon.png.ecl-tmp"
                converted.save(temp, "PNG")
                temp.replace(world / "icon.png")
        except (OSError, UnidentifiedImageError) as exc:
            raise GameServiceError("世界图标必须是可读图片", "INVALID_WORLD_ICON") from exc
        return {"path": str(world / "icon.png")}

    def delete_world(self, game_path: Any, version_id: Any, world_id: Any, version_isolation: Any = False) -> None:
        target = self.resolve_instance(game_path, version_id, version_isolation)
        world = self._world_path(game_path, version_id, world_id, version_isolation)
        self._assert_world_writable(target, world)
        delete_path(world)

    def export_world(
        self, game_path: Any, version_id: Any, world_id: Any, output_path: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        world = self._world_path(game_path, version_id, world_id, version_isolation)
        output = Path(str(output_path)).expanduser().resolve(strict=False)

        def worker(context: OperationContext) -> dict[str, str]:
            output.parent.mkdir(parents=True, exist_ok=True)
            temp = output.with_name(f".{output.name}.ecl-tmp")
            try:
                context.progress(10, "正在导出存档")
                with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                    files = [path for path in world.rglob("*") if path.is_file()]
                    for index, path in enumerate(files, 1):
                        context.check_cancelled()
                        archive.write(path, Path(world.name) / path.relative_to(world))
                        context.progress(index * 90 / max(1, len(files)), "正在导出存档")
                temp.replace(output)
                return {"path": str(output)}
            finally:
                temp.unlink(missing_ok=True)

        return self._game_operations.submit("world_export", worker)

    def import_world(
        self, game_path: Any, version_id: Any, source_path: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        """
        提交本地存档目录或 ZIP 的导入任务，归档内容仍由工作线程校验。

        本地入口限制后缀，在线下载共用助手不受临时归档文件名影响。

        :param game_path: 游戏根目录
        :param version_id: 实例标识
        :param source_path: 存档文件夹或 ZIP 文件
        :param version_isolation: 是否使用实例隔离目录
        :return: 可查询的导入任务标识
        :raises GameServiceError: 本地输入不是目录或 ZIP 文件时抛出
        """
        root = self._world_root(game_path, version_id, version_isolation)
        source = Path(str(source_path)).expanduser().resolve(strict=True)
        if not source.is_dir() and (not source.is_file() or source.suffix.casefold() != ".zip"):
            raise GameServiceError("仅支持世界目录或 .zip 文件", "UNSUPPORTED_WORLD_IMPORT")

        def worker(context: OperationContext) -> dict[str, str]:
            return self._import_world_source(root, source, context)

        return self._game_operations.submit("world_import", worker)

    def _import_world_source(
        self,
        root: Path,
        source: Path,
        context: OperationContext | None = None,
    ) -> dict[str, str]:
        # 校验并导入单个世界目录或 ZIP，供本地导入与在线存档下载共用。
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ecl-world-import-", dir=root.parent) as temp_dir:
            temp = Path(temp_dir)
            if source.is_dir():
                copied = temp / source.name
                shutil.copytree(source, copied)
            elif zipfile.is_zipfile(source):
                safe_extract_zip(source, temp)
            else:
                raise GameServiceError("仅支持世界目录或 ZIP 文件", "UNSUPPORTED_WORLD_IMPORT")
            candidates = [path.parent for path in temp.rglob("level.dat")]
            if len(candidates) != 1:
                raise GameServiceError("导入内容必须且只能包含一个有效存档", "INVALID_WORLD_ARCHIVE")
            candidate = candidates[0]
            self._read_world(candidate)
            destination = resolve_relative_id(root, candidate.name, must_exist=False)
            if destination.exists():
                raise GameServiceError("同名存档已存在", "WORLD_ALREADY_EXISTS")
            if context is not None:
                context.check_cancelled()
            shutil.move(str(candidate), destination)
            return {"worldId": destination.name}

    def _backup_root(self, target: Any, world_id: str) -> Path:
        return target.game_path / "ECLBackups" / target.version_id / world_id

    def create_world_backup(
        self,
        game_path: Any,
        version_id: Any,
        world_id: Any,
        version_isolation: Any = False,
        *,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """
        创建带元数据的 ZIP 备份并只保留最近十个未锁定备份。
        """
        target = self.resolve_instance(game_path, version_id, version_isolation)
        world = self._world_path(game_path, version_id, world_id, version_isolation)
        backup_root = self._backup_root(target, world.name)
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_path = backup_root / f"{backup_id}.zip"
        temp = archive_path.with_suffix(".tmp")
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in world.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(world))
        temp.replace(archive_path)
        metadata = {
            "id": backup_id,
            "createdAt": datetime.now(UTC).isoformat(),
            "locked": False,
            "automatic": automatic,
        }
        atomic_write_text(backup_root / f"{backup_id}.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        unlocked = [item for item in self.list_world_backups(game_path, version_id, world.name) if not item["locked"]]
        keep_count = 10
        for old in unlocked[keep_count:]:
            delete_path(Path(old["path"]))
            metadata_path = Path(old["metadataPath"])
            if metadata_path.exists():
                delete_path(metadata_path)
        return metadata

    def start_world_backup(
        self, game_path: Any, version_id: Any, world_id: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        """
        在统一长任务协调器中创建世界备份并返回任务标识。
        """

        def worker(context: OperationContext) -> dict[str, Any]:
            context.progress(5, "正在创建世界备份")
            result = self.create_world_backup(game_path, version_id, world_id, version_isolation)
            context.progress(100, "世界备份已创建")
            return result

        return self._game_operations.submit("world_backup", worker)

    def list_world_backups(self, game_path: Any, version_id: Any, world_id: Any) -> list[dict[str, Any]]:
        target = self.resolve_instance(game_path, version_id)
        backup_root = self._backup_root(target, str(world_id))
        results: list[dict[str, Any]] = []
        if not backup_root.is_dir():
            return results
        for archive in backup_root.glob("*.zip"):
            metadata_path = archive.with_suffix(".json")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            except (OSError, ValueError):
                metadata = {}
            results.append(
                {
                    "id": archive.stem,
                    "createdAt": metadata.get("createdAt"),
                    "locked": bool(metadata.get("locked")),
                    "automatic": bool(metadata.get("automatic")),
                    "size": archive.stat().st_size,
                    "path": str(archive),
                    "metadataPath": str(metadata_path),
                }
            )
        return sorted(results, key=lambda item: item["id"], reverse=True)

    def lock_world_backup(
        self, game_path: Any, version_id: Any, world_id: Any, backup_id: Any, locked: bool
    ) -> dict[str, Any]:
        target = self.resolve_instance(game_path, version_id)
        root = self._backup_root(target, str(world_id))
        archive = resolve_relative_id(root, f"{backup_id}.zip")
        metadata_path = archive.with_suffix(".json")
        metadata = {"id": archive.stem, "createdAt": datetime.now(UTC).isoformat(), "automatic": False}
        if metadata_path.is_file():
            with suppress(OSError, ValueError):
                metadata.update(json.loads(metadata_path.read_text(encoding="utf-8")))
        metadata["locked"] = bool(locked)
        atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))
        return metadata

    def delete_world_backup(self, game_path: Any, version_id: Any, world_id: Any, backup_id: Any) -> None:
        """
        把指定备份及其元数据直接删除。
        """
        target = self.resolve_instance(game_path, version_id)
        root = self._backup_root(target, str(world_id))
        archive = resolve_relative_id(root, f"{backup_id}.zip")
        delete_path(archive)
        metadata = archive.with_suffix(".json")
        if metadata.exists():
            delete_path(metadata)

    def restore_world_backup(
        self, game_path: Any, version_id: Any, world_id: Any, backup_id: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        target = self.resolve_instance(game_path, version_id, version_isolation)
        current = self._world_path(game_path, version_id, world_id, version_isolation)
        self._assert_world_writable(target, current)
        archive = resolve_relative_id(self._backup_root(target, current.name), f"{backup_id}.zip")

        def worker(context: OperationContext) -> dict[str, str]:
            with tempfile.TemporaryDirectory(prefix="ecl-world-restore-", dir=current.parent) as temp_dir:
                restored = Path(temp_dir) / "world"
                restored.mkdir()
                safe_extract_zip(archive, restored)
                self._read_world(restored)
                context.check_cancelled()
                old = current.with_name(f".{current.name}.ecl-restore-old")
                current.replace(old)
                try:
                    restored.replace(current)
                except Exception:
                    old.replace(current)
                    raise
                delete_path(old)
                return {"worldId": current.name}

        return self._game_operations.submit("world_restore", worker)

    def world_quick_play_capability(self, game_path: Any, version_id: Any) -> dict[str, Any]:
        target = self.resolve_instance(game_path, version_id)
        manifest_path = target.instance_path / f"{target.version_id}.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            game_args = (manifest.get("arguments") or {}).get("game") or []
            supported = any("quickPlaySingleplayer" in str(item) for item in game_args)
        except (OSError, UnicodeDecodeError, ValueError):
            supported = False
        return {"supported": supported, "reason": None if supported else "该版本不支持 --quickPlaySingleplayer"}

    def chunkbase_url(
        self, game_path: Any, version_id: Any, world_id: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        world = self.world_detail(game_path, version_id, world_id, version_isolation)
        version = str(world.get("version") or "")
        if not version or any(char.isalpha() for char in version.replace("Java", "")):
            raise GameServiceError("快照或未知版本无法映射到 Chunkbase", "CHUNKBASE_VERSION_UNSUPPORTED")
        return {
            "url": f"https://www.chunkbase.com/apps/seed-map#seed={quote_plus(world['seed'])}&platform=java_{quote_plus(version)}"
        }
