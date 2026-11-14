# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例兼容性读取：外部实例元数据与监视路径解析。
#
# 公开接口：
#   - class InstanceCompatibilityReader — 读取第三方启动器的实例元数据，不对第三方文件执行任何写入。
#       - read_instance(game_path, version_id, vanilla_name, primary_loader, options) -> list[ExternalInstanceMetadata] — 汇总指定实例可用的第三方元数据来源。
#       - describe_sources() -> list[dict[str, str]] — 返回内置和插件提供的兼容来源描述。
#       - resolve_watch_paths(options=…) -> list[tuple[str, Path]] — 返回插件兼容来源要求监听的外部文件。
#       - revision() -> int — 返回插件兼容来源注册表的当前变更序号。
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ECL.plugins.instance_compat import (
    ExternalInstanceMetadata,
    InstanceCompatibilityContext,
    InstanceCompatibilityRegistry,
)
from ECL.utils import get_logger


def _read_text(path: Path) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return path.read_text(encoding="utf-8")


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _find_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if str(current_key).casefold() == key.casefold():
                return current_value
        for current_value in value.values():
            result = _find_key(current_value, key)
            if result is not None:
                return result
    elif isinstance(value, list):
        for current_value in value:
            result = _find_key(current_value, key)
            if result is not None:
                return result
    return None


class InstanceCompatibilityReader:
    """
    读取第三方启动器的实例元数据，不对第三方文件执行任何写入。

    解析失败会转换为来源级警告，使单个损坏配置不会阻断整个版本扫描。
    """

    _IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    _HMCL_ICON_MAP = {
        "grass": "grass",
        "chest": "chest",
        "command": "command",
        "forge": "forge",
        "neo_forge": "neoforge",
        "neoforge": "neoforge",
        "fabric": "fabric",
        "quilt": "quilt",
        "optifine": "optifine",
        "furnace": "iron",
        "craft_table": "quartz",
    }

    def __init__(self, registry: InstanceCompatibilityRegistry | None = None) -> None:
        """
        创建内置读取器，并接入可选的插件兼容来源注册表。

        :param registry: 插件实例兼容提供者注册表
        """
        self._logger = get_logger("InstanceCompatibilityReader")
        self._registry = registry or InstanceCompatibilityRegistry()

    @staticmethod
    def _parse_pcl_ini(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in _read_text(path).splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "[")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().casefold()] = value.strip()
        return values

    def _read_pcl(self, instance_path: Path) -> ExternalInstanceMetadata | None:
        setup_path = instance_path / "PCL" / "Setup.ini"
        if not setup_path.is_file():
            return None
        metadata = ExternalInstanceMetadata(source="pcl", modified_ns=setup_path.stat().st_mtime_ns)
        try:
            values = self._parse_pcl_ini(setup_path)
            description = values.get("custominfo", "").strip()
            if description:
                metadata.fields["description"] = description
            if "isstar" in values:
                favorite = _parse_bool(values["isstar"])
                if favorite is not None:
                    metadata.fields["favorite"] = favorite

            display_type = _non_negative_int(values.get("displaytype"))
            if display_type is not None:
                metadata.fields["hidden"] = display_type == 1
                category = {2: "modpack", 3: "vanilla", 5: "test"}.get(display_type)
                if category:
                    metadata.fields["categoryId"] = category

            custom_logo = instance_path / "PCL" / "Logo.png"
            if custom_logo.is_file() and _parse_bool(values.get("logocustom")) is not False:
                metadata.fields["icon"] = {"type": "external", "value": str(custom_logo), "source": "pcl"}
                metadata.modified_ns = max(metadata.modified_ns, custom_logo.stat().st_mtime_ns)
            else:
                logo_name = values.get("logo", "").casefold()
                logo_map = {
                    "grass": ("builtin", "grass"),
                    "chest": ("builtin", "chest"),
                    "command": ("builtin", "command"),
                    "neoforge": ("loader", "neoforge"),
                    "forge": ("loader", "forge"),
                    "fabric": ("loader", "fabric"),
                    "quilt": ("loader", "quilt"),
                    "optifine": ("loader", "optifine"),
                }
                matched_logo = next((value for key, value in logo_map.items() if key in logo_name), None)
                if matched_logo is not None:
                    metadata.fields["icon"] = {
                        "type": matched_logo[0],
                        "value": matched_logo[1],
                        "source": "pcl",
                    }
            launch_count = _non_negative_int(values.get("versionlaunchcount"))
            if launch_count is not None:
                metadata.stats["launchCount"] = launch_count
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            metadata.warnings.append(f"PCL 配置读取失败: {exc}")
            self._logger.warning("读取 PCL 实例配置失败 %s: %s", setup_path, exc)
        return metadata

    def _read_hmcl(self, instance_path: Path) -> ExternalInstanceMetadata | None:
        settings_path = instance_path / ".hmcl" / "config" / "instance-game-settings.json"
        icon_paths = [instance_path / f"icon{extension}" for extension in self._IMAGE_EXTENSIONS]
        local_icon = next((path for path in icon_paths if path.is_file()), None)
        if not settings_path.is_file() and local_icon is None:
            return None
        modified_ns = max(
            [path.stat().st_mtime_ns for path in (settings_path, local_icon) if path is not None and path.is_file()],
            default=0,
        )
        metadata = ExternalInstanceMetadata(source="hmcl", modified_ns=modified_ns)
        try:
            icon_name = "default"
            if settings_path.is_file():
                data = json.loads(_read_text(settings_path))
                raw_icon = _find_key(data, "icon")
                if isinstance(raw_icon, str) and raw_icon.strip():
                    icon_name = raw_icon.strip().casefold()
            if local_icon is not None and icon_name == "default":
                metadata.fields["icon"] = {"type": "external", "value": str(local_icon), "source": "hmcl"}
            elif icon_name in self._HMCL_ICON_MAP:
                icon_value = self._HMCL_ICON_MAP[icon_name]
                icon_type = (
                    "loader" if icon_value in {"forge", "neoforge", "fabric", "quilt", "optifine"} else "builtin"
                )
                metadata.fields["icon"] = {"type": icon_type, "value": icon_value, "source": "hmcl"}
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            metadata.warnings.append(f"HMCL 配置读取失败: {exc}")
            self._logger.warning("读取 HMCL 实例配置失败 %s: %s", settings_path, exc)
        return metadata

    def read_instance(
        self,
        game_path: Path,
        version_id: str,
        *,
        vanilla_name: str,
        primary_loader: str,
        options: dict[str, Any] | None = None,
    ) -> list[ExternalInstanceMetadata]:
        """
        汇总指定实例可用的第三方元数据来源。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :param vanilla_name: 扫描得到的原版版本号
        :param primary_loader: 扫描得到的主加载器
        :param options: 按来源标识分组的插件兼容配置
        :return: 按来源分离的只读元数据
        """
        instance_path = game_path / "versions" / version_id
        sources: list[ExternalInstanceMetadata | None] = [
            self._read_pcl(instance_path),
            self._read_hmcl(instance_path),
        ]
        plugin_context = InstanceCompatibilityContext(
            game_path=game_path,
            instance_path=instance_path,
            version_id=version_id,
            vanilla_name=vanilla_name,
            primary_loader=primary_loader,
            options=options or {},
        )
        return [source for source in sources if source is not None] + self._registry.read(plugin_context)

    def describe_sources(self) -> list[dict[str, str]]:
        """
        返回内置和插件提供的兼容来源描述。
        """
        return [
            {"source": "pcl", "title": "PCL / PCL-CE", "plugin": "builtin"},
            {"source": "hmcl", "title": "HMCL", "plugin": "builtin"},
            *self._registry.describe_sources(),
        ]

    def resolve_watch_paths(self, options: dict[str, Any] | None = None) -> list[tuple[str, Path]]:
        """
        返回插件兼容来源要求监听的外部文件。

        :param options: 按来源标识分组的插件兼容配置
        """
        return self._registry.resolve_watch_paths(options)

    @property
    def revision(self) -> int:
        """
        返回插件兼容来源注册表的当前变更序号。
        """
        return self._registry.revision


__all__ = ["ExternalInstanceMetadata", "InstanceCompatibilityReader"]
