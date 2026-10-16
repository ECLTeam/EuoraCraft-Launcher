# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：执行内置有限任务，检查实际宿主依赖而不进入业务或 UI 初始化。
#
# 公开接口：
#   - class RuntimeCheckPolicy — 运行环境检查的依赖与模块边界。
#   - execute_task — 执行明确注册的有限任务。
# ============================================================

from __future__ import annotations

import importlib
import platform
import sys

from ECL.foundation.version import __version__
from ECL.runtime.context import HeadlessContext
from ECL.runtime.contracts import DependencyProbe, HeadlessRuntimeError, HeadlessTask, RuntimeProbe


class RuntimeCheckPolicy:
    """
    保存代表性宿主依赖与不允许无头入口导入的模块边界。
    """

    dependencies: tuple[str, ...] = ("httpx", "pydantic", "psutil")
    prohibited_modules: tuple[str, ...] = (
        "ECL.launcher",
        "ECL.application",
        "ECL.host",
        "ECL.api",
        "ECL.services",
        "ECL.plugins",
        "pytauri",
        "pytauri_plugins",
        "pytauri_utils",
        "pytauri_wheel",
    )

    @classmethod
    def has_prohibited_modules(cls) -> bool:
        """
        检查传递导入是否越过无头任务边界。

        :return: 已载入桌面框架或业务入口时返回 True
        """
        return any(
            name == prefix or name.startswith(prefix + ".")
            for name in tuple(sys.modules)
            for prefix in cls.prohibited_modules
        )


def execute_task(context: HeadlessContext) -> RuntimeProbe:
    """
    执行内置环境探测，不动态加载任意用户模块或插件。

    :param context: 只属于本次执行的精简上下文
    :return: 不包含路径或配置的环境探测摘要
    :raises HeadlessRuntimeError: 任务不支持、依赖不可用或导入越界时抛出
    """
    if context.options.task != HeadlessTask.runtime_check:
        raise HeadlessRuntimeError("HEADLESS_TASK_UNSUPPORTED", "无头任务不受支持")
    dependencies: list[DependencyProbe] = []
    for name in RuntimeCheckPolicy.dependencies:
        try:
            dependency = importlib.import_module(name)
        except (ImportError, OSError) as error:
            raise HeadlessRuntimeError("HEADLESS_DEPENDENCY_UNAVAILABLE", f"宿主依赖不可用：{name}") from error
        version = getattr(dependency, "__version__", None)
        if not isinstance(version, str) or not version:
            raise HeadlessRuntimeError("HEADLESS_DEPENDENCY_UNAVAILABLE", f"宿主依赖版本不可用：{name}")
        dependencies.append(DependencyProbe(name, version))
    if RuntimeCheckPolicy.has_prohibited_modules():
        raise HeadlessRuntimeError("HEADLESS_IMPORT_BOUNDARY", "无头任务载入了桌面或业务入口")
    return RuntimeProbe(
        __version__,
        platform.python_implementation(),
        platform.python_version(),
        sys.platform,
        platform.machine(),
        sys.implementation.cache_tag or "unknown",
        context.runtime_info["is_frozen"],
        False,
        tuple(dependencies),
    )
