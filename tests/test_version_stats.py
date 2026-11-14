# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 version_stats 模块的自动化测试。
#
# 公开接口：
#   - test_version_stats_store_creates_defaults_and_accumulates_runs(tmp_path) -> None
#   - test_version_stats_store_recovers_malformed_file_on_next_write(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json

from ECL.minecraft.services.game.version_stats import VersionStatsStore


def test_version_stats_store_creates_defaults_and_accumulates_runs(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "1.21.8"
    version_path.mkdir(parents=True)
    store = VersionStatsStore()

    assert store.read(game_path, "1.21.8") == {
        "launchCount": 0,
        "lastRunDurationSeconds": 0,
        "totalRunDurationSeconds": 0,
        "lastLaunchedAt": None,
        "externalSnapshots": {},
    }

    store.record_launch(game_path, "1.21.8")
    store.record_launch(game_path, "1.21.8")
    store.record_duration(game_path, "1.21.8", 65)
    store.record_duration(game_path, "1.21.8", 5)

    stats = json.loads((version_path / "eclversion.json").read_text(encoding="utf-8"))
    assert stats["launchCount"] == 2
    assert stats["lastRunDurationSeconds"] == 5
    assert stats["totalRunDurationSeconds"] == 70
    assert stats["lastLaunchedAt"]
    assert stats["externalSnapshots"] == {}


def test_version_stats_store_recovers_malformed_file_on_next_write(tmp_path) -> None:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "broken"
    version_path.mkdir(parents=True)
    stats_file = version_path / "eclversion.json"
    stats_file.write_text("{invalid", encoding="utf-8")
    store = VersionStatsStore()

    assert store.read(game_path, "broken")["launchCount"] == 0

    store.record_launch(game_path, "broken")

    recovered = json.loads(stats_file.read_text(encoding="utf-8"))
    assert recovered["launchCount"] == 1
    assert recovered["lastRunDurationSeconds"] == 0
    assert recovered["totalRunDurationSeconds"] == 0
    assert recovered["lastLaunchedAt"]
    assert recovered["externalSnapshots"] == {}
