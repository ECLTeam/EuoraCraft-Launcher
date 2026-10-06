# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：调试维护任务：重置数据/清理插件的计划与执行。
#
# 公开接口：
#   - class ScheduledMaintenance — 待执行的维护任务。
#   - class MaintenanceResult — 维护结果。
#   - schedule_debug_maintenance(data_path, action) -> ScheduledMaintenance — 安排一次仅在下次启动时执行的受限调试维护操作。
#   - apply_pending_debug_maintenance(data_path) -> list[MaintenanceResult] — 执行已安排的调试维护操作，并将受影响数据移入可恢复备份。
# ============================================================

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ECL.utils import DebugMaintenanceError, atomic_write_text


class MaintenancePolicy:
    """
    调试维护任务的标记文件和可操作目标白名单。

    白名单只覆盖启动器数据目录内的设置与插件，禁止维护逻辑触及账户数据。
    """

    pending_marker_filename = ".pending_debug_maintenance.json"
    targets_by_action: dict[str, tuple[str, ...]] = {
        "reset_launcher_data": ("setting.json", "info_card.json", "notice.json"),
        "clear_plugins": ("plugins", "plugin_config"),
    }


@dataclass(frozen=True, slots=True)
class ScheduledMaintenance:
    """
    待执行的维护任务。
    """

    action: str
    targets: tuple[str, ...]
    restart_required: bool = True


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """
    维护结果。
    """

    action: str
    archived_targets: tuple[str, ...]
    backup_path: Path | None


def _get_data_root(data_path: Path | str) -> Path:
    root = Path(data_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_safe_target(root: Path, relative_path: str) -> Path:
    target = (root / relative_path).resolve()
    if target == root or root not in target.parents:
        raise DebugMaintenanceError(f"维护目标超出数据目录: {relative_path}")
    return target


def _maintenance_backup_path(root: Path, task_id: str, action: str, relative_path: str) -> Path:
    return root / "backups" / "debug-maintenance" / task_id / action / relative_path


def _maintenance_journal_path(root: Path) -> Path:
    return root / "maintenance-history.jsonl"


def _append_maintenance_record(root: Path, record: dict[str, object]) -> None:
    journal_path = _maintenance_journal_path(root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        journal.write("\n")


def _claim_pending_marker(marker_path: Path) -> tuple[Path, str] | None:
    task_id = uuid4().hex
    claimed_path = marker_path.with_name(f"{marker_path.stem}.running-{task_id}{marker_path.suffix}")
    try:
        marker_path.replace(claimed_path)
    except FileNotFoundError:
        return None
    return claimed_path, task_id


def _read_pending_actions(marker_path: Path) -> list[str]:
    if not marker_path.is_file():
        return []
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, str) and action in MaintenancePolicy.targets_by_action]


def schedule_debug_maintenance(data_path: Path | str, action: str) -> ScheduledMaintenance:
    """
    安排一次仅在下次启动时执行的受限调试维护操作。

    :param data_path: 启动器数据目录
    :param action: 需要执行的操作类型
    """
    if action not in MaintenancePolicy.targets_by_action:
        raise DebugMaintenanceError(f"不支持的维护操作: {action}")
    root = _get_data_root(data_path)
    marker_path = root / MaintenancePolicy.pending_marker_filename
    actions = _read_pending_actions(marker_path)
    if action not in actions:
        actions.append(action)
    marker_data = {"actions": actions, "scheduled_at": datetime.now(UTC).isoformat()}
    atomic_write_text(marker_path, json.dumps(marker_data, ensure_ascii=False, indent=2))
    return ScheduledMaintenance(action, MaintenancePolicy.targets_by_action[action])


def apply_pending_debug_maintenance(data_path: Path | str) -> list[MaintenanceResult]:
    """
    执行已安排的调试维护操作，并将受影响数据移动到可恢复备份。

    :param data_path: 启动器数据目录
    :return: 本次成功执行的维护结果；不存在待处理任务时返回空列表

    维护操作只允许影响 ``data_path`` 内的白名单目标，绝不访问用户主目录中的账户数据。
    待处理标记通过原子重命名领取，避免多个启动器进程重复执行同一任务。
    """
    root = _get_data_root(data_path)
    marker_path = root / MaintenancePolicy.pending_marker_filename
    claimed = _claim_pending_marker(marker_path)
    if claimed is None:
        return []
    claimed_path, task_id = claimed
    actions = _read_pending_actions(claimed_path)
    if not actions:
        invalid_path = claimed_path.with_name(f"{claimed_path.stem}.invalid{claimed_path.suffix}")
        claimed_path.replace(invalid_path)
        _append_maintenance_record(
            root,
            {
                "actions": [],
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "invalid",
                "task_id": task_id,
            },
        )
        return []

    results: list[MaintenanceResult] = []
    try:
        for action in actions:
            archived_targets: list[str] = []
            backup_path: Path | None = None
            for relative_path in MaintenancePolicy.targets_by_action[action]:
                target = _get_safe_target(root, relative_path)
                if not target.exists():
                    continue
                destination = _maintenance_backup_path(root, task_id, action, relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(destination))
                archived_targets.append(relative_path)
                backup_path = _maintenance_backup_path(root, task_id, action, "")
            results.append(MaintenanceResult(action, tuple(archived_targets), backup_path))
    except OSError as exc:
        failed_path = claimed_path.with_name(f"{claimed_path.stem}.failed{claimed_path.suffix}")
        claimed_path.replace(failed_path)
        _append_maintenance_record(
            root,
            {
                "actions": actions,
                "error": str(exc),
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "failed",
                "task_id": task_id,
            },
        )
        raise
    else:
        claimed_path.unlink(missing_ok=True)
        _append_maintenance_record(
            root,
            {
                "actions": actions,
                "archived_targets": {result.action: list(result.archived_targets) for result in results},
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "completed",
                "task_id": task_id,
            },
        )
    return results
