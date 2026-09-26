# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：运行时环境信息：可执行/资源/数据目录解析（兼容冻结打包）。
#
# 公开接口：
#   - class RuntimeInfo — 运行环境信息。
#   - get_runtime_info() -> RuntimeInfo — 获取当前运行环境的信息。
#   - get_pyproject_data(app_path) -> dict | None — 读取 pyproject.toml 的解析结果。
# ============================================================

import builtins
import os
import sys
import tomllib
from pathlib import Path
from typing import TypedDict

_module_globals = globals()
# PyInstaller 会设置 sys.frozen；Nuitka 不设置，而是注入 __compiled__（模块属性）与
# __nuitka_binary_dir（内建，指向可执行文件或 onefile 负载解压目录）。


def _get_nuitka_binary_dir() -> str | None:
    """
    返回 Nuitka 注入的二进制/负载目录；非 Nuitka 环境返回 None。
    """
    if "__nuitka_binary_dir" in _module_globals:
        return _module_globals["__nuitka_binary_dir"]
    return getattr(builtins, "__nuitka_binary_dir", None)


def _is_nuitka_build() -> bool:
    return bool(_module_globals.get("__compiled__", False)) or bool(getattr(builtins, "__compiled__", False))


class RuntimeInfo(TypedDict):
    """
    运行环境信息。
    """

    is_frozen: bool
    app_path: Path
    resource_path: Path
    data_path: Path


def get_runtime_info(data_path_override: Path | None = None) -> RuntimeInfo:
    """
    获取当前运行环境的信息。

    :param data_path_override: 命令行 --data-dir 指定的数据目录，优先于 ECL_DATA_PATH 环境变量
    :return: 是否冻结打包，以及应用、资源与持久化数据目录路径
    """
    is_frozen = bool(getattr(sys, "frozen", False)) or _is_nuitka_build()
    nuitka_binary_dir = _get_nuitka_binary_dir()
    if nuitka_binary_dir is not None:
        # Nuitka standalone：数据与资源文件位于可执行文件同目录；
        # Nuitka onefile：资源随负载解压到临时目录，而持久化数据目录应定位到原始
        # onefile 可执行文件所在目录（由引导进程注入 NUITKA_ONEFILE_DIRECTORY）。
        onefile_dir = os.environ.get("NUITKA_ONEFILE_DIRECTORY")
        app_path = Path(onefile_dir).resolve() if onefile_dir else Path(sys.executable).resolve().parent
        resource_path = Path(nuitka_binary_dir).resolve()
    elif is_frozen:
        app_path = Path(sys.executable).resolve().parent
        resource_path = Path(getattr(sys, "_MEIPASS", app_path)).resolve()
    else:
        app_path = Path(__file__).resolve().parent.parent.parent
        resource_path = app_path
    # 外部工具（如插件开发工具箱）通过该环境变量将数据目录重定向到沙箱位置，
    # 使开发版启动器与正式安装完全隔离；命令行参数优先于该环境变量。
    if data_path_override is not None:
        data_path = Path(data_path_override).expanduser().resolve()
    else:
        env_data_path = os.environ.get("ECL_DATA_PATH")
        data_path = Path(env_data_path).expanduser().resolve() if env_data_path else app_path / "ECL_data"
    return {
        "is_frozen": is_frozen,
        "app_path": app_path,
        "resource_path": resource_path,
        "data_path": data_path,
    }


def get_pyproject_data(app_path: Path) -> dict | None:
    """
    读取 pyproject.toml 的解析结果。

    :param app_path: 启动器运行目录
    :return: pyproject.toml 内容；读取失败或格式非法时返回 None
    """
    open_path = Path(app_path / "pyproject.toml")
    try:
        with open_path.open(encoding="utf-8") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
