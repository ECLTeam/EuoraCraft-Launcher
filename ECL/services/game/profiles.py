# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例资料协调器：资料读写、图标设置与置顶排序。
#
# 公开接口：
#   - class ProfileCoordinator — 读取和修改实例资料、图标、置顶顺序和分类。
#       - get_instance_profile(game_path, version_id) -> dict[str, Any] — 读取单个实例已持久化的 ECL 覆盖字段。
#       - patch_instance_profile(game_path, version_id, patch) -> dict[str, Any] — 合并保存实例资料并立即失效扫描缓存。
#       - reset_instance_profile(game_path, version_id, fields) -> dict[str, Any] — 删除实例覆盖字段，使其恢复第三方或自动值。
#       - set_instance_icon(game_path, version_id, icon_type, value=…, source_path=…) -> dict[str, Any] — 保存实例图标选择，本地图片会复制到实例自己的 ``.ecl`` 目录。
#       - set_instance_pin_order(entries) -> None — 保存跨游戏路径的置顶实例顺序。
#       - get_instance_categories() -> list[dict[str, Any]] — 返回内置与用户自定义的实例分类。
#       - upsert_instance_category(category_id, name, color, order) -> dict[str, Any] — 新建或更新用户自定义实例分类。
#       - delete_instance_category(category_id) -> None — 删除一个用户自定义实例分类。
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import GameServiceError, _GameState


class ProfileCoordinator(_GameState):
    """
    读取和修改实例资料、图标、置顶顺序和分类。
    """

    def _profile_target(self, game_path: Any, version_id: Any) -> tuple[Path, str]:
        path = self._normalize_game_path(game_path)
        name = self._normalize_version_name(version_id)
        if not (path / "versions" / name).is_dir():
            raise GameServiceError("游戏实例不存在", "VERSION_NOT_FOUND")
        return path, name

    def _notify_profile_changed(self, game_path: Path) -> None:
        key = self._version_path_key(game_path)
        with self._lock:
            self._version_scan_cache.pop(key, None)
        self.events.emit("game:versions_changed", {"gamePath": str(game_path)})

    def get_instance_profile(self, game_path: Any, version_id: Any) -> dict[str, Any]:
        """
        读取单个实例已持久化的 ECL 覆盖字段。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :return: 原始实例资料；未设置字段不会被填入自动值
        """
        path, name = self._profile_target(game_path, version_id)
        return self._instance_profiles.read_profile(path, name)

    def patch_instance_profile(
        self,
        game_path: Any,
        version_id: Any,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        """
        合并保存实例资料并立即失效扫描缓存。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :param patch: 已通过 IPC 模型校验的覆盖字段
        :return: 保存后的原始实例资料
        """
        path, name = self._profile_target(game_path, version_id)
        try:
            result = self._instance_profiles.patch_profile(path, name, patch)
        except (OSError, TypeError, ValueError) as exc:
            raise GameServiceError(str(exc), "INSTANCE_PROFILE_WRITE_FAILED") from exc
        self._notify_profile_changed(path)
        return result

    def reset_instance_profile(
        self,
        game_path: Any,
        version_id: Any,
        fields: list[str],
    ) -> dict[str, Any]:
        """
        删除实例覆盖字段，使其恢复第三方或自动值。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :param fields: 需要恢复自动的字段名称
        :return: 保存后的原始实例资料
        """
        path, name = self._profile_target(game_path, version_id)
        try:
            result = self._instance_profiles.reset_profile_fields(path, name, fields)
        except (OSError, ValueError) as exc:
            raise GameServiceError(str(exc), "INSTANCE_PROFILE_WRITE_FAILED") from exc
        self._notify_profile_changed(path)
        return result

    def set_instance_icon(
        self,
        game_path: Any,
        version_id: Any,
        icon_type: str,
        value: str | None = None,
        source_path: Path | None = None,
    ) -> dict[str, Any]:
        """
        保存实例图标选择，本地图片会复制到实例自己的 ``.ecl`` 目录。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :param icon_type: 自动、内置、加载器或本地图片类型
        :param value: 内置图标或加载器标识
        :param source_path: 本地图片来源路径
        :return: 保存后的原始实例资料
        """
        path, name = self._profile_target(game_path, version_id)
        try:
            result = self._instance_profiles.set_icon(path, name, icon_type, value, source_path)
        except (OSError, ValueError) as exc:
            raise GameServiceError(str(exc), "INSTANCE_ICON_WRITE_FAILED") from exc
        self._notify_profile_changed(path)
        return result

    def set_instance_pin_order(self, entries: list[dict[str, Any]]) -> None:
        """
        保存跨游戏路径的置顶实例顺序。

        :param entries: 含 ``game_path`` 与 ``version_id`` 的有序目标列表
        """
        targets = [self._profile_target(entry.get("game_path"), entry.get("version_id")) for entry in entries]
        try:
            self._instance_profiles.reorder_pins(targets)
        except (OSError, ValueError) as exc:
            raise GameServiceError(str(exc), "INSTANCE_PROFILE_WRITE_FAILED") from exc
        changed_paths = {path for path, _ in targets}
        for path in changed_paths:
            self._notify_profile_changed(path)

    def get_instance_categories(self) -> list[dict[str, Any]]:
        """
        返回内置与用户自定义的实例分类。
        """
        return self._instance_profiles.get_categories()

    def upsert_instance_category(
        self,
        category_id: str | None,
        name: str,
        color: str,
        order: int,
    ) -> dict[str, Any]:
        """
        新建或更新用户自定义实例分类。

        :param category_id: 可选的现有自定义分类 ID
        :param name: 分类显示名称
        :param color: CSS 十六进制颜色
        :param order: 分类排序值
        :return: 保存后的分类
        """
        try:
            return self._instance_profiles.upsert_category(category_id, name, color, order)
        except (OSError, ValueError) as exc:
            raise GameServiceError(str(exc), "INSTANCE_CATEGORY_WRITE_FAILED") from exc

    def delete_instance_category(self, category_id: str) -> None:
        """
        删除一个用户自定义实例分类。

        :param category_id: 自定义分类 ID
        """
        try:
            self._instance_profiles.delete_category(category_id)
        except (OSError, ValueError) as exc:
            raise GameServiceError(str(exc), "INSTANCE_CATEGORY_WRITE_FAILED") from exc


__all__ = ["ProfileCoordinator"]
