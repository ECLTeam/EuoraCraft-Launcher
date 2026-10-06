# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 debug_maintenance 模块的自动化测试。
#
# 公开接口：
#   - test_reset_launcher_data_archives_only_declared_targets(tmp_path) -> None
#   - test_clear_plugins_can_be_scheduled_with_data_reset(tmp_path) -> None
#   - test_unknown_debug_maintenance_action_is_rejected(tmp_path) -> None
# ============================================================

import json

import pytest

from ECL.host import maintenance
from ECL.host.maintenance import (
    DebugMaintenanceError,
    MaintenancePolicy,
    apply_pending_debug_maintenance,
    schedule_debug_maintenance,
)


def _write(path, content: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_reset_launcher_data_archives_only_declared_targets(tmp_path) -> None:
    data_path = tmp_path / "ECL_data"
    home_dir = tmp_path / "home"
    _write(data_path / "setting.json", "{}")
    _write(data_path / "info_card.json", "{}")
    _write(data_path / "notice.json", "{}")
    _write(data_path / "accounts" / "legacy.json", "{}")
    _write(data_path / "plugins" / "example" / "plugin.json", "{}")
    _write(data_path / "logs" / "launcher.log")
    _write(home_dir / ".ECL" / "accounts" / "accounts.json", "{}")

    scheduled = schedule_debug_maintenance(data_path, "reset_launcher_data")
    results = apply_pending_debug_maintenance(data_path)

    assert scheduled.restart_required is True
    assert results[0].action == "reset_launcher_data"
    assert set(results[0].archived_targets) == {
        "setting.json",
        "info_card.json",
        "notice.json",
    }
    assert not (data_path / "setting.json").exists()
    assert not (data_path / "info_card.json").exists()
    assert not (data_path / "notice.json").exists()
    assert (data_path / "accounts" / "legacy.json").is_file()
    assert (data_path / "plugins" / "example" / "plugin.json").is_file()
    assert (data_path / "logs" / "launcher.log").is_file()
    assert (home_dir / ".ECL" / "accounts" / "accounts.json").is_file()
    assert results[0].backup_path is not None
    assert (results[0].backup_path / "setting.json").is_file()
    assert not (data_path / MaintenancePolicy.pending_marker_filename).exists()


def test_clear_plugins_can_be_scheduled_with_data_reset(tmp_path) -> None:
    data_path = tmp_path / "ECL_data"
    _write(data_path / "setting.json", "{}")
    _write(data_path / "plugins" / "example" / "plugin.json", "{}")
    _write(data_path / "plugin_config" / "example.json", "{}")

    schedule_debug_maintenance(data_path, "reset_launcher_data")
    schedule_debug_maintenance(data_path, "clear_plugins")
    marker = json.loads((data_path / MaintenancePolicy.pending_marker_filename).read_text(encoding="utf-8"))

    assert marker["actions"] == ["reset_launcher_data", "clear_plugins"]

    results = apply_pending_debug_maintenance(data_path)

    assert [result.action for result in results] == ["reset_launcher_data", "clear_plugins"]
    assert not (data_path / "plugins").exists()
    assert not (data_path / "plugin_config").exists()


def test_claimed_maintenance_task_is_not_replayed(tmp_path) -> None:
    data_path = tmp_path / "ECL_data"
    _write(data_path / "setting.json", "{}")
    schedule_debug_maintenance(data_path, "reset_launcher_data")

    first_results = apply_pending_debug_maintenance(data_path)
    second_results = apply_pending_debug_maintenance(data_path)

    assert len(first_results) == 1
    assert second_results == []
    journal = (data_path / "maintenance-history.jsonl").read_text(encoding="utf-8")
    assert '"status": "completed"' in journal


def test_failed_maintenance_task_is_not_replayed(tmp_path, monkeypatch) -> None:
    data_path = tmp_path / "ECL_data"
    _write(data_path / "setting.json", "{}")
    schedule_debug_maintenance(data_path, "reset_launcher_data")

    def raise_move_error(source, destination):
        raise OSError("模拟移动失败")

    monkeypatch.setattr(maintenance.shutil, "move", raise_move_error)

    with pytest.raises(OSError, match="模拟移动失败"):
        apply_pending_debug_maintenance(data_path)

    assert not (data_path / MaintenancePolicy.pending_marker_filename).exists()
    assert list(data_path.glob(".pending_debug_maintenance.running-*.failed.json"))
    assert apply_pending_debug_maintenance(data_path) == []
    journal = (data_path / "maintenance-history.jsonl").read_text(encoding="utf-8")
    assert '"status": "failed"' in journal


def test_unknown_debug_maintenance_action_is_rejected(tmp_path) -> None:
    with pytest.raises(DebugMaintenanceError):
        schedule_debug_maintenance(tmp_path / "ECL_data", "../../outside")
