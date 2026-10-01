# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：兼容游戏领域的旧操作管理导入；任务注册表由应用层统一拥有。
#
# 公开接口：
#   - GameOperationManager — 应用操作管理器的兼容别名。
#   - OperationContext — 可取消任务的执行上下文。
# ============================================================

from __future__ import annotations

from ECL.services.operations import OperationContext, OperationManager

GameOperationManager = OperationManager

__all__ = ["GameOperationManager", "OperationContext"]
