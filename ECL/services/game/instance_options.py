# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例 options.txt 的读取与增量保存。
#
# 公开接口：
#   - class InstanceOptionsCoordinator — 结构化读写实例级 options.txt，覆盖常用游戏设置，未知键保持只读。
#       - read_options(game_path, version_id, version_isolation=…) -> dict[str, Any] — 读取实例 options.txt，仅返回可结构化编辑的常用键及其取值约束。
#       - patch_options(game_path, version_id, patch, version_isolation=…) -> dict[str, Any] — 按受限 schema 写入指定的 options.txt 键，保留未知行与原有顺序。
# ============================================================

from __future__ import annotations

from typing import Any

from ECL.utils import atomic_write_text

from .base import GameServiceError


class InstanceOptionsCoordinator:
    """
    结构化读写实例级 options.txt，覆盖常用游戏设置，未知键保持只读。

    与其它领域协调器一致采用普通基类，``resolve_instance`` 由
    ``GameService`` 多重继承时经 ``WorkspaceCoordinator`` 提供。
    """

    option_specs: dict[str, dict[str, Any]] = {
        "language": {"type": "string"},
        "fullscreen": {"type": "bool"},
        "vsync": {"type": "bool"},
        "renderDistance": {"type": "int", "min": 2, "max": 32},
        "guiScale": {"type": "int", "min": 0, "max": 4},
        "fov": {"type": "float", "min": 30.0, "max": 110.0},
        "sensitivity": {"type": "float", "min": 0.0, "max": 1.0},
        "mouseSensitivity": {"type": "float", "min": 0.0, "max": 1.0},
        "gamma": {"type": "float", "min": 0.0, "max": 5.0},
        "difficulty": {"type": "int", "min": 0, "max": 3},
        "particles": {"type": "int", "min": 0, "max": 2},
        "clouds": {"type": "int", "min": 0, "max": 3},
    }
    sound_categories = frozenset(
        {"master", "music", "records", "weather", "block", "hostile", "neutral", "player", "ambient", "voice"}
    )

    @staticmethod
    def _spec(key: str) -> dict[str, Any] | None:
        if key in InstanceOptionsCoordinator.option_specs:
            return InstanceOptionsCoordinator.option_specs[key]
        if (
            key.startswith("soundCategory_")
            and key.removeprefix("soundCategory_") in InstanceOptionsCoordinator.sound_categories
        ):
            return {"type": "float", "min": 0.0, "max": 1.0}
        return None

    def _options_path(self, game_path: Any, version_id: Any, version_isolation: Any = False):
        return self.resolve_instance(game_path, version_id, version_isolation).game_data_path / "options.txt"

    @staticmethod
    def _load_lines(path) -> list[str]:
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    @staticmethod
    def _coerce(kind: str, value: str):
        try:
            if kind == "bool":
                return value.strip().casefold() == "true"
            if kind == "int":
                return int(value)
            if kind == "float":
                return float(value)
            return value
        except ValueError:
            return None

    def read_options(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> dict[str, Any]:
        """
        读取实例 options.txt，仅返回可结构化编辑的常用键及其取值约束。

        :return: 包含 ``options`` 列表与 ``ignoredCount`` 的结果
        """
        path = self._options_path(game_path, version_id, version_isolation)
        entries: list[dict[str, Any]] = []
        ignored = 0
        for line in self._load_lines(path):
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            spec = self._spec(key)
            if spec is None:
                ignored += 1
                continue
            coerced = self._coerce(spec["type"], value.strip())
            if coerced is None:
                ignored += 1
                continue
            entries.append(
                {
                    "key": key,
                    "value": coerced,
                    "type": spec["type"],
                    "min": spec.get("min"),
                    "max": spec.get("max"),
                }
            )
        return {"path": str(path), "options": entries, "ignoredCount": ignored}

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
        :raises GameServiceError: 键不受支持或值不合法时抛出
        """
        if not isinstance(patch, dict) or not patch:
            raise GameServiceError("没有需要修改的游戏选项", "INVALID_GAME_OPTION")
        to_write: dict[str, str] = {}
        for key, value in patch.items():
            spec = self._spec(str(key))
            if spec is None:
                raise GameServiceError("该选项暂不支持修改", "UNSUPPORTED_GAME_OPTION")
            to_write[str(key)] = self._encode(spec, value)
        path = self._options_path(game_path, version_id, version_isolation)
        lines = self._load_lines(path)
        remaining = dict(to_write)
        replaced = 0
        for index, line in enumerate(lines):
            if ":" in line:
                key = line.partition(":")[0]
                if key in remaining:
                    lines[index] = f"{key}:{remaining.pop(key)}"
                    replaced += 1
        appended = 0
        for key, value in remaining.items():
            lines.append(f"{key}:{value}")
            appended += 1
        atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
        return {"path": str(path), "updated": replaced + appended}

    @staticmethod
    def _encode(spec: dict[str, Any], value: Any) -> str:
        kind = spec["type"]
        if kind == "bool":
            if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
                value = value.strip().casefold() == "true"
            if not isinstance(value, bool):
                raise GameServiceError("开关值无效", "INVALID_GAME_OPTION")
            return "true" if value else "false"
        if kind in ("int", "float"):
            try:
                converted = int(value) if kind == "int" else float(value)
            except (TypeError, ValueError) as exc:
                raise GameServiceError("数值格式无效", "INVALID_GAME_OPTION") from exc
            if isinstance(converted, bool):
                raise GameServiceError("数值格式无效", "INVALID_GAME_OPTION")
            minimum = spec.get("min")
            maximum = spec.get("max")
            if minimum is not None and converted < minimum:
                raise GameServiceError(f"数值不能小于 {minimum}", "INVALID_GAME_OPTION")
            if maximum is not None and converted > maximum:
                raise GameServiceError(f"数值不能大于 {maximum}", "INVALID_GAME_OPTION")
            return str(converted)
        return str(value)


__all__ = ["InstanceOptionsCoordinator"]
