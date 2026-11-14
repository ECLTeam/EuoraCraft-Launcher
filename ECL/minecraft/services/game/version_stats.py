# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：版本运行统计：启动次数与运行时长持久化。
#
# 公开接口：
#   - class VersionRunStats
#   - class VersionStatsStore — 管理每个 Minecraft 版本目录中的运行统计文件。
#       - ensure(game_path, version_id) -> VersionRunStats — 确保已识别版本拥有默认统计文件，并返回规范化数据。
#       - read(game_path, version_id) -> VersionRunStats — 读取版本运行统计，文件不存在时同时创建默认文件。
#       - record_launch(game_path, version_id) -> None — 在游戏进程成功创建后累计一次启动。
#       - record_duration(game_path, version_id, duration_seconds) -> None — 在一次受管理运行结束或启动器关闭时累计观察到的时长。
#       - reconcile_external(game_path, version_id, source_stats) -> VersionRunStats — 将第三方累计值按来源快照转换为正增量，重复扫描不会再次累加。
# ============================================================

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, TypedDict

from ECL.utils import atomic_write_text, get_logger


class VersionRunStats(TypedDict):
    launchCount: int
    lastRunDurationSeconds: int
    totalRunDurationSeconds: int
    lastLaunchedAt: str | None
    externalSnapshots: dict[str, dict[str, Any]]


def _default_stats() -> VersionRunStats:
    return {
        "launchCount": 0,
        "lastRunDurationSeconds": 0,
        "totalRunDurationSeconds": 0,
        "lastLaunchedAt": None,
        "externalSnapshots": {},
    }


class VersionStatsStore:
    """
    管理每个 Minecraft 版本目录中的运行统计文件。

    存储层以进程内锁串行化同一启动器产生的并发更新，并通过同目录临时文件原子
    替换目标文件。统计失败只影响统计本身，不应阻止版本扫描或游戏启动。
    """

    file_name = "eclversion.json"

    def __init__(self) -> None:
        self._lock = RLock()
        self._logger = get_logger("VersionStatsStore")

    @staticmethod
    def _stats_path(game_path: Path, version_id: str) -> Path:
        return game_path / "versions" / version_id / VersionStatsStore.file_name

    @staticmethod
    def _normalize(data: object) -> VersionRunStats:
        source = data if isinstance(data, dict) else {}

        def non_negative_int(key: str) -> int:
            value = source.get(key, 0)
            if isinstance(value, bool):
                return 0
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        last_launched_at = source.get("lastLaunchedAt")
        snapshots = source.get("externalSnapshots")
        return {
            "launchCount": non_negative_int("launchCount"),
            "lastRunDurationSeconds": non_negative_int("lastRunDurationSeconds"),
            "totalRunDurationSeconds": non_negative_int("totalRunDurationSeconds"),
            "lastLaunchedAt": last_launched_at.strip()
            if isinstance(last_launched_at, str) and last_launched_at.strip()
            else None,
            "externalSnapshots": snapshots if isinstance(snapshots, dict) else {},
        }

    @staticmethod
    def _latest_timestamp(current: str | None, candidate: Any) -> str | None:
        if not isinstance(candidate, str) or not candidate.strip():
            return current

        def parse(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)

        candidate_time = parse(candidate.strip())
        if candidate_time is None:
            return current
        current_time = parse(current)
        if current_time is not None and current_time >= candidate_time:
            return current
        return candidate_time.isoformat().replace("+00:00", "Z")

    def _read_unlocked(self, stats_path: Path) -> VersionRunStats:
        if not stats_path.is_file():
            return _default_stats()
        try:
            return self._normalize(json.loads(stats_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._logger.warning("读取版本运行统计失败 %s: %s", stats_path, exc)
            return _default_stats()

    def _write_unlocked(self, stats_path: Path, stats: VersionRunStats) -> bool:
        try:
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(stats_path, json.dumps(stats, ensure_ascii=False, indent=2))
            return True
        except OSError as exc:
            self._logger.warning("写入版本运行统计失败 %s: %s", stats_path, exc)
            return False

    def ensure(self, game_path: Path, version_id: str) -> VersionRunStats:
        """
        确保已识别版本拥有默认统计文件，并返回规范化数据。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已通过游戏服务校验的版本目录名称
        :return: 统计文件当前数据；读取失败时返回零值
        """
        stats_path = self._stats_path(game_path, version_id)
        with self._lock:
            stats = self._read_unlocked(stats_path)
            if not stats_path.is_file():
                self._write_unlocked(stats_path, stats)
            return stats

    def read(self, game_path: Path, version_id: str) -> VersionRunStats:
        """
        读取版本运行统计，文件不存在时同时创建默认文件。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已通过游戏服务校验的版本目录名称
        :return: 可直接通过 IPC 返回的统计副本
        """
        return dict(self.ensure(game_path, version_id))

    def record_launch(self, game_path: Path, version_id: str) -> None:
        """
        在游戏进程成功创建后累计一次启动。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已通过游戏服务校验的版本目录名称
        """
        stats_path = self._stats_path(game_path, version_id)
        with self._lock:
            stats = self._read_unlocked(stats_path)
            stats["launchCount"] += 1
            stats["lastLaunchedAt"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self._write_unlocked(stats_path, stats)

    def record_duration(self, game_path: Path, version_id: str, duration_seconds: int) -> None:
        """
        在一次受管理运行结束或启动器关闭时累计观察到的时长。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已通过游戏服务校验的版本目录名称
        :param duration_seconds: 非负整数秒；不足一秒按零秒记录
        """
        stats_path = self._stats_path(game_path, version_id)
        duration = max(0, int(duration_seconds))
        with self._lock:
            stats = self._read_unlocked(stats_path)
            stats["lastRunDurationSeconds"] = duration
            stats["totalRunDurationSeconds"] += duration
            self._write_unlocked(stats_path, stats)

    def reconcile_external(
        self,
        game_path: Path,
        version_id: str,
        source_stats: dict[str, dict[str, Any]],
    ) -> VersionRunStats:
        """
        将第三方累计值按来源快照转换为正增量，重复扫描不会再次累加。

        外部值降低通常表示第三方清空统计或重建实例，此时只更新基线而不减少 ECL
        已汇总的历史值。``pcl`` 来源同时覆盖两种实例格式，因此不会重复计算。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 已通过游戏服务校验的版本目录名称
        :param source_stats: 来源名到第三方累计统计的映射
        :return: 汇总后的运行统计
        """
        stats_path = self._stats_path(game_path, version_id)
        with self._lock:
            stats = self._read_unlocked(stats_path)
            snapshots = dict(stats.get("externalSnapshots") or {})
            changed = False
            for source, current_values in source_stats.items():
                if not isinstance(current_values, dict):
                    continue
                previous = snapshots.get(source)
                previous_values = previous if isinstance(previous, dict) else {}
                snapshot: dict[str, Any] = {}

                launch_count = current_values.get("launchCount")
                if not isinstance(launch_count, bool):
                    try:
                        current_count = max(0, int(launch_count))
                    except (TypeError, ValueError):
                        current_count = None
                    if current_count is not None:
                        previous_count = previous_values.get("launchCount")
                        delta = current_count if previous_count is None else max(0, current_count - int(previous_count))
                        stats["launchCount"] += delta
                        snapshot["launchCount"] = current_count
                        changed = changed or delta > 0 or previous_count != current_count

                total_duration = current_values.get("totalRunDurationSeconds")
                if not isinstance(total_duration, bool):
                    try:
                        current_duration = max(0, int(total_duration))
                    except (TypeError, ValueError):
                        current_duration = None
                    if current_duration is not None:
                        previous_duration = previous_values.get("totalRunDurationSeconds")
                        delta = (
                            current_duration
                            if previous_duration is None
                            else max(0, current_duration - int(previous_duration))
                        )
                        stats["totalRunDurationSeconds"] += delta
                        snapshot["totalRunDurationSeconds"] = current_duration
                        changed = changed or delta > 0 or previous_duration != current_duration

                last_launched_at = current_values.get("lastLaunchedAt")
                latest = self._latest_timestamp(stats.get("lastLaunchedAt"), last_launched_at)
                if latest != stats.get("lastLaunchedAt"):
                    stats["lastLaunchedAt"] = latest
                    changed = True
                if isinstance(last_launched_at, str) and last_launched_at.strip():
                    snapshot["lastLaunchedAt"] = last_launched_at.strip()
                snapshots[source] = snapshot

            stats["externalSnapshots"] = snapshots
            if changed or not stats_path.is_file():
                self._write_unlocked(stats_path, stats)
            return dict(stats)


__all__ = ["VersionRunStats", "VersionStatsStore"]
