# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：提供 Tauri 适配器和桌面端使用的服务。
#
# 说明：Tauri 入口 ``Adapter`` 依赖 ``ECL.api``，为避免导入环，本包不预先
# 导入 ``ECL.host.tauri``，需要时请直接从 ``ECL.host.tauri`` 导入。
# ============================================================

from ECL.host.app_update import AppUpdateError, UpdateApplier, clear_stale_pending_update
from ECL.host.background_media import BackgroundMediaService
from ECL.host.dev_channel import DevChannelError, DevChannelService
from ECL.host.frontend_events import (
    FrontendEventPolicy,
    subscribe_all_frontend_events,
    subscribe_frontend_event,
)
from ECL.host.info_card import InfoCardManager
from ECL.host.maintenance import (
    MaintenanceResult,
    ScheduledMaintenance,
    apply_pending_debug_maintenance,
    schedule_debug_maintenance,
)
from ECL.host.single_instance import SingleInstanceService, probe_running_instance

__all__ = [
    "AppUpdateError",
    "BackgroundMediaService",
    "DevChannelError",
    "DevChannelService",
    "FrontendEventPolicy",
    "InfoCardManager",
    "MaintenanceResult",
    "ScheduledMaintenance",
    "SingleInstanceService",
    "UpdateApplier",
    "apply_pending_debug_maintenance",
    "clear_stale_pending_update",
    "probe_running_instance",
    "schedule_debug_maintenance",
    "subscribe_all_frontend_events",
    "subscribe_frontend_event",
]
