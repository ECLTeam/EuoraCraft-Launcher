# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例资料存储：图标/封面/分类/标签等持久化。
#
# 公开接口：
#   - class InstanceProfilePolicy — 保存实例资料字段与内置分类规则。
#   - class InstanceProfileStore — 管理 ECL 自有实例资料、分类和第三方只读元数据的解析结果。
#       - read_profile(game_path, version_id) -> dict[str, Any] — 读取实例自有资料并过滤未知或损坏字段。
#       - patch_profile(game_path, version_id, patch) -> dict[str, Any] — 合并实例资料覆盖字段，显式 ``False`` 会按原值保存。
#       - reset_profile_fields(game_path, version_id, fields) -> dict[str, Any] — 删除指定覆盖字段，使它们重新跟随第三方或自动扫描值。
#       - set_icon(game_path, version_id, icon_type, value=…, source_path=…) -> dict[str, Any] — 设置自动、内置、加载器或本地图片图标。
#       - reorder_pins(entries) -> None — 按前端提交顺序为全部置顶实例写入稳定整数顺序。
#       - get_categories() -> list[dict[str, Any]] — 返回内置分类与用户自定义分类的稳定有序视图。
#       - upsert_category(category_id, name, color, order) -> dict[str, Any] — 新建或更新用户分类，内置分类不可修改。
#       - delete_category(category_id) -> None — 删除用户自定义分类；资料中失效的分类 ID 会在展示时回退到未分类。
#       - enrich_version(game_path, version, compatibility_options) -> dict[str, Any] — 将 ECL 覆盖、第三方来源和运行统计合并进扫描结果。
#       - compatibility_watch_paths(options=…) -> list[tuple[str, Path]] — 返回插件兼容来源要求加入版本缓存快照的外部文件。
#       - compatibility_revision() -> int — 返回实例兼容插件注册表的变更序号。
# ============================================================

from __future__ import annotations

import json
import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from ECL.utils import atomic_write_bytes, atomic_write_text, get_logger

from .instance_compat import ExternalInstanceMetadata, InstanceCompatibilityReader
from .version_stats import VersionStatsStore


class InstanceProfilePolicy:
    """
    保存实例资料字段、外部来源和内置分类规则。
    """

    profile_fields = frozenset(
        {
            "alias",
            "description",
            "favorite",
            "pinned",
            "hidden",
            "categoryId",
            "tags",
            "icon",
            "cover",
            "pinOrder",
        }
    )
    external_sources = frozenset({"auto", "pcl", "hmcl"})
    version_icons = {
        "release": "grass",
        "snapshot": "command",
        "old_beta": "coal",
        "old_alpha": "quartz",
        "april_fools": "iron",
    }
    external_source_pattern = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
    builtin_categories = (
        {"id": "unclassified", "name": "未分类", "color": "#8b95a5", "order": 0, "builtin": True},
        {"id": "vanilla", "name": "原版", "color": "#69a84f", "order": 10, "builtin": True},
        {"id": "modded", "name": "模组", "color": "#8a73c7", "order": 20, "builtin": True},
        {"id": "modpack", "name": "整合包", "color": "#d58b45", "order": 30, "builtin": True},
        {"id": "test", "name": "测试", "color": "#d55d72", "order": 40, "builtin": True},
    )


def _default_profile() -> dict[str, Any]:
    return {"schemaVersion": 1, "preferredExternalSource": "auto"}


def _normalize_external_source(value: Any) -> str:
    normalized = str(value or "auto").strip().casefold()
    if normalized != "auto" and not InstanceProfilePolicy.external_source_pattern.fullmatch(normalized):
        raise ValueError("未知的第三方元数据来源")
    return normalized


def _normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("标签必须是字符串数组")
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in value:
        if not isinstance(raw_tag, str):
            raise ValueError("标签必须是字符串数组")
        tag = raw_tag.strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag[:40])
    return tags[:20]


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    size_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in size_markers:
            return struct.unpack(">HH", data[offset + 5 : offset + 9])[::-1]
        segment_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if segment_length < 2:
            return None
        offset += 2 + segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if not (data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30):
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        return struct.unpack("<HH", data[26:30])
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _image_dimensions(data: bytes, extension: str) -> tuple[int, int] | None:
    if extension == ".png" and len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    if extension == ".gif" and data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if extension == ".bmp" and data.startswith(b"BM") and len(data) >= 26:
        return abs(struct.unpack("<i", data[18:22])[0]), abs(struct.unpack("<i", data[22:26])[0])
    if extension in {".jpg", ".jpeg"}:
        return _jpeg_dimensions(data)
    if extension == ".webp":
        return _webp_dimensions(data)
    return None


class InstanceProfileStore:
    """
    管理 ECL 自有实例资料、分类和第三方只读元数据的解析结果。

    实例资料与图片归实例目录所有；全局分类归应用数据目录所有。所有持久化写入
    都经过原子替换，并由同一进程锁避免并发 patch 丢失字段。
    """

    _IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
    _BUILTIN_ICON_IDS = frozenset({"grass", "chest", "command", "coal", "iron", "quartz"})
    _LOADER_ICON_IDS = frozenset({"vanilla", "forge", "neoforge", "fabric", "quilt", "optifine"})
    _MAX_IMAGE_BYTES = 10 * 1024 * 1024
    _MAX_IMAGE_DIMENSION = 4096

    def __init__(
        self,
        data_path: Path,
        stats_store: VersionStatsStore,
        compatibility_reader: InstanceCompatibilityReader | None = None,
    ) -> None:
        self._data_path = data_path
        self._stats = stats_store
        self._compatibility = compatibility_reader or InstanceCompatibilityReader()
        self._categories_path = data_path / "instance-categories.json"
        self._logger = get_logger("InstanceProfileStore")
        self._lock = RLock()

    @staticmethod
    def _instance_path(game_path: Path, version_id: str) -> Path:
        return game_path / "versions" / version_id

    @classmethod
    def _profile_path(cls, game_path: Path, version_id: str) -> Path:
        return cls._instance_path(game_path, version_id) / ".ecl" / "instance.json"

    def read_profile(self, game_path: Path, version_id: str) -> dict[str, Any]:
        """
        读取实例自有资料并过滤未知或损坏字段。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已校验的版本目录名称
        :return: 包含模式版本和已设置覆盖字段的资料
        """
        profile_path = self._profile_path(game_path, version_id)
        if not profile_path.is_file():
            return _default_profile()
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._logger.warning("读取实例资料失败 %s: %s", profile_path, exc)
            return _default_profile()
        if not isinstance(data, dict):
            return _default_profile()
        profile = _default_profile()
        for key in InstanceProfilePolicy.profile_fields:
            if key in data:
                profile[key] = data[key]
        try:
            profile["preferredExternalSource"] = _normalize_external_source(data.get("preferredExternalSource"))
        except ValueError:
            profile["preferredExternalSource"] = "auto"
        try:
            if "tags" in profile:
                profile["tags"] = _normalize_tags(profile["tags"])
        except ValueError:
            profile.pop("tags", None)
        try:
            if "icon" in profile:
                profile["icon"] = self._normalize_patch_value("icon", profile["icon"])
        except ValueError:
            profile.pop("icon", None)
        return profile

    def _write_profile(self, game_path: Path, version_id: str, profile: dict[str, Any]) -> None:
        profile_path = self._profile_path(game_path, version_id)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(profile_path, json.dumps(profile, ensure_ascii=False, indent=2))

    @classmethod
    def _normalize_icon(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("图标配置无效")
        icon_type = str(value.get("type") or "").strip().casefold()
        icon_value = str(value.get("value") or "").strip().casefold()
        known_icon = (icon_type == "builtin" and icon_value in cls._BUILTIN_ICON_IDS) or (
            icon_type == "loader" and icon_value in cls._LOADER_ICON_IDS
        )
        local_icon = (
            icon_type == "local"
            and Path(icon_value).name == icon_value
            and icon_value.startswith("icon.")
            and Path(icon_value).suffix in cls._IMAGE_EXTENSIONS
        )
        if not known_icon and not local_icon:
            raise ValueError("图标配置无效或不受支持")
        return {"type": icon_type, "value": icon_value}

    @classmethod
    def _normalize_patch_value(cls, key: str, value: Any) -> Any:
        if key in {"alias", "description", "categoryId"}:
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串")
            normalized = value.strip()
            if key == "alias" and not normalized:
                raise ValueError("实例别名不能为空；如需恢复请使用自动")
            limit = 120 if key == "alias" else 1000 if key == "description" else 64
            return normalized[:limit]
        if key in {"favorite", "pinned", "hidden"}:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值")
            return value
        if key == "tags":
            return _normalize_tags(value)
        if key == "pinOrder":
            if isinstance(value, bool):
                raise ValueError("pinOrder 必须是整数")
            return max(0, int(value))
        if key == "preferredExternalSource":
            return _normalize_external_source(value)
        if key == "icon":
            return cls._normalize_icon(value)
        return value

    @classmethod
    def _normalize_patch(cls, patch: dict[str, Any]) -> dict[str, Any]:
        unknown = set(patch) - InstanceProfilePolicy.profile_fields - {"preferredExternalSource"}
        if unknown:
            raise ValueError(f"不支持的实例资料字段: {', '.join(sorted(unknown))}")
        return {key: cls._normalize_patch_value(key, value) for key, value in patch.items()}

    def patch_profile(self, game_path: Path, version_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """
        合并实例资料覆盖字段，显式 ``False`` 会按原值保存。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已校验的版本目录名称
        :param patch: 已经 IPC 类型校验的增量字段
        :return: 写入后的资料
        """
        normalized = self._normalize_patch(patch)
        with self._lock:
            profile = self.read_profile(game_path, version_id)
            profile.update(normalized)
            profile["schemaVersion"] = 1
            self._write_profile(game_path, version_id, profile)
            return profile

    def reset_profile_fields(self, game_path: Path, version_id: str, fields: list[str]) -> dict[str, Any]:
        """
        删除指定覆盖字段，使它们重新跟随第三方或自动扫描值。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已校验的版本目录名称
        :param fields: 需要恢复自动的字段列表
        :return: 写入后的资料
        """
        unknown = set(fields) - InstanceProfilePolicy.profile_fields - {"preferredExternalSource"}
        if unknown:
            raise ValueError(f"不支持的实例资料字段: {', '.join(sorted(unknown))}")
        with self._lock:
            profile = self.read_profile(game_path, version_id)
            for field_name in fields:
                profile.pop(field_name, None)
            if "preferredExternalSource" in fields:
                profile["preferredExternalSource"] = "auto"
            self._write_profile(game_path, version_id, profile)
            return profile

    @classmethod
    def _validate_image(cls, source_path: Path) -> tuple[bytes, str]:
        extension = source_path.suffix.casefold()
        if extension not in cls._IMAGE_EXTENSIONS:
            raise ValueError("实例图标仅支持 PNG、JPEG、WebP、GIF 或 BMP")
        data = source_path.read_bytes()
        if not data or len(data) > cls._MAX_IMAGE_BYTES:
            raise ValueError("实例图标为空或超过 10 MiB")
        dimensions = _image_dimensions(data, extension)
        if dimensions is None:
            raise ValueError("无法识别实例图标内容")
        width, height = dimensions
        if width <= 0 or height <= 0 or width > cls._MAX_IMAGE_DIMENSION or height > cls._MAX_IMAGE_DIMENSION:
            raise ValueError("实例图标尺寸必须在 1 到 4096 像素之间")
        return data, extension

    def set_icon(
        self,
        game_path: Path,
        version_id: str,
        icon_type: str,
        value: str | None = None,
        source_path: Path | None = None,
    ) -> dict[str, Any]:
        """
        设置自动、内置、加载器或本地图片图标。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已校验的版本目录名称
        :param icon_type: ``auto``、``builtin``、``loader`` 或 ``local``
        :param value: 内置图标或加载器标识
        :param source_path: 本地图片来源，仅 ``local`` 使用
        :return: 写入后的实例资料
        """
        normalized_type = icon_type.casefold()
        ecl_directory = self._profile_path(game_path, version_id).parent
        with self._lock:
            profile = self.read_profile(game_path, version_id)
            if normalized_type == "auto":
                profile.pop("icon", None)
                for extension in self._IMAGE_EXTENSIONS:
                    local_path = ecl_directory / f"icon{extension}"
                    if local_path.is_file():
                        local_path.unlink()
            elif normalized_type in {"builtin", "loader"}:
                normalized_value = str(value or "").strip().casefold()
                if not normalized_value:
                    raise ValueError("未选择实例图标")
                profile["icon"] = self._normalize_patch_value(
                    "icon",
                    {"type": normalized_type, "value": normalized_value},
                )
            elif normalized_type == "local":
                if source_path is None or not source_path.is_file():
                    raise ValueError("本地图标文件不存在")
                data, extension = self._validate_image(source_path)
                ecl_directory.mkdir(parents=True, exist_ok=True)
                target = ecl_directory / f"icon{extension}"
                atomic_write_bytes(target, data)
                for old_extension in self._IMAGE_EXTENSIONS - {extension}:
                    old_path = ecl_directory / f"icon{old_extension}"
                    if old_path.is_file():
                        old_path.unlink()
                profile["icon"] = {"type": "local", "value": target.name}
            else:
                raise ValueError("未知的实例图标类型")
            self._write_profile(game_path, version_id, profile)
            return profile

    def reorder_pins(self, entries: list[tuple[Path, str]]) -> None:
        """
        按前端提交顺序为全部置顶实例写入稳定整数顺序。

        :param entries: ``(游戏根目录, 版本目录名)`` 顺序列表
        """
        with self._lock:
            for order, (game_path, version_id) in enumerate(entries):
                profile = self.read_profile(game_path, version_id)
                profile.update({"pinned": True, "pinOrder": order})
                self._write_profile(game_path, version_id, profile)

    def get_categories(self) -> list[dict[str, Any]]:
        """
        返回内置分类与用户自定义分类的稳定有序视图。
        """
        custom: list[dict[str, Any]] = []
        if self._categories_path.is_file():
            try:
                data = json.loads(self._categories_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    custom = [item for item in data if isinstance(item, dict)]
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                self._logger.warning("读取实例分类失败 %s: %s", self._categories_path, exc)
        categories = [dict(item) for item in InstanceProfilePolicy.builtin_categories]
        for item in custom:
            category_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if (
                not category_id
                or not name
                or category_id in {entry["id"] for entry in InstanceProfilePolicy.builtin_categories}
            ):
                continue
            categories.append(
                {
                    "id": category_id,
                    "name": name[:40],
                    "color": str(item.get("color") or "#7d8da6"),
                    "order": max(50, int(item.get("order") or 50)),
                    "builtin": False,
                }
            )
        return sorted(categories, key=lambda item: (item["order"], item["name"].casefold()))

    def _write_custom_categories(self, categories: list[dict[str, Any]]) -> None:
        self._categories_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._categories_path, json.dumps(categories, ensure_ascii=False, indent=2))

    def upsert_category(
        self,
        category_id: str | None,
        name: str,
        color: str,
        order: int,
    ) -> dict[str, Any]:
        """
        新建或更新用户分类，内置分类不可修改。

        :param category_id: 现有自定义分类 ID；缺失时创建新 ID
        :param name: 分类显示名称
        :param color: CSS 十六进制颜色
        :param order: 用户分类排序值
        :return: 保存后的分类
        """
        normalized_name = name.strip()[:40]
        if not normalized_name:
            raise ValueError("分类名称不能为空")
        normalized_color = color.strip().lower()
        if len(normalized_color) not in {4, 7} or not normalized_color.startswith("#"):
            raise ValueError("分类颜色必须是十六进制颜色")
        try:
            int(normalized_color[1:], 16)
        except ValueError as exc:
            raise ValueError("分类颜色必须是十六进制颜色") from exc
        builtin_ids = {item["id"] for item in InstanceProfilePolicy.builtin_categories}
        normalized_id = (category_id or f"custom-{uuid4().hex[:12]}").strip()
        if normalized_id in builtin_ids:
            raise ValueError("内置分类不可修改")
        category = {
            "id": normalized_id,
            "name": normalized_name,
            "color": normalized_color,
            "order": max(50, int(order)),
            "builtin": False,
        }
        with self._lock:
            custom = [item for item in self.get_categories() if not item["builtin"] and item["id"] != normalized_id]
            custom.append(category)
            self._write_custom_categories(custom)
        return category

    def delete_category(self, category_id: str) -> None:
        """
        删除用户自定义分类；资料中失效的分类 ID 会在展示时回退到未分类。

        :param category_id: 自定义分类 ID
        """
        if category_id in {item["id"] for item in InstanceProfilePolicy.builtin_categories}:
            raise ValueError("内置分类不可删除")
        with self._lock:
            custom = [item for item in self.get_categories() if not item["builtin"] and item["id"] != category_id]
            self._write_custom_categories(custom)

    @staticmethod
    def _automatic_category(version: dict[str, Any]) -> str:
        version_type = str(version.get("versionType") or "").casefold()
        if version_type in {"snapshot", "april_fools", "old_alpha", "old_beta"}:
            return "test"
        return "vanilla" if str(version.get("primaryLoader") or "vanilla").casefold() == "vanilla" else "modded"

    @staticmethod
    def _resolve_icon(icon: Any, game_path: Path, version_id: str) -> Any:
        if not isinstance(icon, dict):
            return icon
        resolved = dict(icon)
        if resolved.get("type") == "local":
            resolved["value"] = str(game_path / "versions" / version_id / ".ecl" / str(resolved.get("value") or ""))
        return resolved

    def enrich_version(
        self,
        game_path: Path,
        version: dict[str, Any],
        *,
        compatibility_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        将 ECL 覆盖、第三方来源和运行统计合并进扫描结果。

        :param game_path: Minecraft 游戏根目录
        :param version: Core 扫描得到的基础版本信息
        :param compatibility_options: 按来源标识分组的插件兼容配置
        :return: 可直接供前端展示、筛选和排序的完整实例模型
        """
        result = dict(version)
        version_id = str(version["versionId"])
        profile = self.read_profile(game_path, version_id)
        sources = self._compatibility.read_instance(
            game_path,
            version_id,
            vanilla_name=str(version.get("vanillaName") or version_id),
            primary_loader=str(version.get("primaryLoader") or "Vanilla"),
            options=compatibility_options,
        )
        source_by_name = {source.source: source for source in sources}
        preferred = str(profile.get("preferredExternalSource") or "auto")
        warnings = [warning for source in sources for warning in source.warnings]
        if preferred != "auto" and preferred not in source_by_name:
            warnings.append(f"固定的 {preferred} 元数据来源当前不可用")

        defaults: dict[str, Any] = {
            "alias": version_id,
            "description": "",
            "favorite": False,
            "pinned": False,
            "hidden": False,
            "categoryId": self._automatic_category(version),
            "tags": [],
            "icon": {
                "type": "loader"
                if str(version.get("primaryLoader") or "Vanilla").casefold() != "vanilla"
                else "builtin",
                "value": str(version.get("primaryLoader") or "grass").casefold()
                if str(version.get("primaryLoader") or "Vanilla").casefold() != "vanilla"
                else InstanceProfilePolicy.version_icons.get(str(version.get("versionType") or "").casefold(), "grass"),
            },
            "cover": None,
            "pinOrder": 2**31 - 1,
        }
        field_sources: dict[str, str] = {}
        profile_overrides: list[str] = []
        resolved: dict[str, Any] = {}
        for field_name, default_value in defaults.items():
            if field_name in profile:
                resolved[field_name] = profile[field_name]
                field_sources[field_name] = "ecl"
                profile_overrides.append(field_name)
                continue
            candidates: list[ExternalInstanceMetadata]
            if preferred == "auto":
                candidates = sorted(
                    [source for source in sources if field_name in source.fields],
                    key=lambda source: source.modified_ns,
                    reverse=True,
                )
            else:
                selected = source_by_name.get(preferred)
                candidates = [selected] if selected is not None and field_name in selected.fields else []
            if candidates:
                resolved[field_name] = candidates[0].fields[field_name]
                field_sources[field_name] = candidates[0].source
            else:
                resolved[field_name] = default_value
                field_sources[field_name] = "auto"

        category_ids = {category["id"] for category in self.get_categories()}
        if resolved["categoryId"] not in category_ids:
            resolved["categoryId"] = "unclassified"
            field_sources["categoryId"] = "auto"
        resolved["icon"] = self._resolve_icon(resolved["icon"], game_path, version_id)
        if isinstance(resolved.get("cover"), dict) and resolved["cover"].get("type") == "local":
            resolved["cover"] = {
                **resolved["cover"],
                "value": str(game_path / "versions" / version_id / ".ecl" / str(resolved["cover"].get("value") or "")),
            }

        external_stats = {source.source: source.stats for source in sources if source.stats}
        stats = self._stats.reconcile_external(game_path, version_id, external_stats)
        result.update(resolved)
        result["displayName"] = resolved["alias"]
        result["fieldSources"] = field_sources
        result["profileOverrides"] = profile_overrides
        result["preferredExternalSource"] = preferred
        result["availableSources"] = sorted(source_by_name)
        result["externalSourceOptions"] = self._compatibility.describe_sources()
        result["sourceWarnings"] = warnings
        result.update(stats)
        return result

    def compatibility_watch_paths(self, options: dict[str, Any] | None = None) -> list[tuple[str, Path]]:
        """
        返回插件兼容来源要求加入版本缓存快照的外部文件。

        :param options: 按来源标识分组的插件兼容配置
        """
        return self._compatibility.resolve_watch_paths(options)

    @property
    def compatibility_revision(self) -> int:
        """
        返回实例兼容插件注册表的变更序号。
        """
        return self._compatibility.revision


__all__ = ["InstanceProfilePolicy", "InstanceProfileStore"]
