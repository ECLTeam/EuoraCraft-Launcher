# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件系统包标记。
#
# 本包不做聚合导出：插件 Worker 以受限导入启动，导入 `ECL.plugins.api`
# 会先执行本模块，任何聚合导出都会把宿主业务依赖带进 Worker 进程。
# 使用方按需从具体模块导入。
# ============================================================
