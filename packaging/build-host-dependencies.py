# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在冻结启动器前生成实际生产依赖保护清单，不制作插件运行时资产。
#
# 公开接口：
#   - main() — 生成宿主依赖清单。
# ============================================================

from __future__ import annotations

from pathlib import Path

from ECL.plugins.host_dependencies import build_host_dependency_manifest


def main() -> int:
    """
    将宿主生产依赖清单写入随启动器分发的资源目录。

    :return: 完成清单生成时返回 0
    """
    build_host_dependency_manifest(Path(__file__).resolve().parents[1] / "resources" / "plugin_host_dependencies.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
