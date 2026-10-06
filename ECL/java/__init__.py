# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：JVM 运行时管理层包：对外暴露清单、来源、安装与生命周期门面。
# ============================================================

from .installer import JavaLifecycle, RuntimeLease
from .manager import JavaManager
from .models import JavaError, JavaPolicy, JavaReferenceConfig

__all__ = ["JavaError", "JavaLifecycle", "JavaManager", "JavaPolicy", "JavaReferenceConfig", "RuntimeLease"]
