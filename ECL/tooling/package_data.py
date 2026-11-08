# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：过滤冻结资源中的历史 Python 缓存，不改变 PYZ 收集的运行模块。
#
# 公开接口：
#   - filter_runtime_data — 保留正常资源和 SDK 源码，排除资源树中的 Python 缓存。
# ============================================================

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath


def filter_runtime_data(entries: Sequence[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """
    过滤 PyInstaller 资源清单中可能残留的历史字节码，并保留 SDK 源码等正常资源。

    Windows 与 POSIX 的资源名称统一按路径段检查；返回独立清单，不修改调用方输入。
    编译后的应用模块由 PYZ 独立收集，不属于本函数处理的资源清单。

    :param entries: PyInstaller 的目标名、源文件和类型三元组
    :return: 已排除 __pycache__、pyc 与 pyo 文件的资源清单
    """
    resources = []
    for entry in entries:
        relative_path = PurePosixPath(entry[0].replace("\\", "/"))
        is_python_cache = "__pycache__" in relative_path.parts or relative_path.suffix.lower() in (".pyc", ".pyo")
        if not is_python_cache:
            resources.append(entry)
    return resources
