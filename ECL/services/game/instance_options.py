# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例 options.txt 的读取与增量保存，规则由 Core 原语提供。
#
# 公开接口：
#   - class InstanceOptionsCoordinator — 解析实例目标后读写实例级 options.txt。
#       - read_options(game_path, version_id, version_isolation=…) -> dict[str, Any] — 读取实例 options.txt，仅返回可结构化编辑的常用键及其取值约束。
#       - patch_options(game_path, version_id, patch, version_isolation=…) -> dict[str, Any] — 按受限 schema 写入指定的 options.txt 键，保留未知行与原有顺序。
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import Any

from ECL.game import InstanceOptionsStore


class InstanceOptionsCoordinator:
    """
    解析实例目标后读写实例级 options.txt，读写规则由 Core 原语提供。

    与其它领域协调器一致采用普通基类，``resolve_instance`` 由
    ``GameService`` 多重继承时经 ``WorkspaceCoordinator`` 提供。
    """

    def _options_path(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> Path:
        return self.resolve_instance(game_path, version_id, version_isolation).game_data_path / "options.txt"

    def read_options(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> dict[str, Any]:
        """
        读取实例 options.txt，仅返回可结构化编辑的常用键及其取值约束。

        :return: 包含 ``options`` 列表与 ``ignoredCount`` 的结果
        """
        return InstanceOptionsStore.read(self._options_path(game_path, version_id, version_isolation))

    def patch_options(
        self,
        game_path: Any,
        version_id: Any,
        patch: dict[str, Any],
        version_isolation: Any = False,
    ) -> dict[str, Any]:
        """
        按受限 schema 写入指定的 options.txt 键，保留未知行与原有顺序。

        :param patch: ``{键: 值}`` 字典，键必须属于可编辑 schema
        :return: 包含写入实例路径与更新键数的结果
        :raises GameDataError: 键不受支持或值不合法时抛出
        """
        return InstanceOptionsStore.patch(self._options_path(game_path, version_id, version_isolation), patch)


__all__ = ["InstanceOptionsCoordinator"]
