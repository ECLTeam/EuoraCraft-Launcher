# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在发行 CI 中验证生成的插件运行时 ZIP 能解包、执行并创建虚拟环境。
#
# 公开接口：
#   - main(argv) -> int — 对当前平台的运行时资产执行离线端到端冒烟。
# ============================================================

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil

from ECL.plugins.environment_pool import PluginEnvironmentPool, PluginEnvironmentSpec
from ECL.plugins.runtime_assets import PluginRuntimeStore, load_plugin_runtime_asset
from ECL.plugins.worker_process import PluginWorkerProcess


def _force_utf8_output() -> None:
    """
    把标准输出与标准错误切换为 UTF-8，避免 ANSI 代码页控制台无法编码中文。

    GitHub 的 Windows 运行器与部分本地终端以 ANSI 代码页（如 cp1252）作为标准流
    编码，中文 print 与 argparse 帮助会直接抛出 UnicodeEncodeError 使冒烟失败；
    本身已是 UTF-8 的环境重配为幂等操作，被替换的非 TextIOWrapper 流则跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def _terminate_processes_from(directory: Path) -> None:
    """
    终止仍以目录内可执行文件为映像运行的进程，释放 Windows 文件锁。

    插件 Worker 与 uv 的可执行文件都位于临时目录内；CI 杀软扫描或进程
    退出后的短暂映像锁会让 rmtree 报拒绝访问，先按 exe 前缀匹配并终止。
    """
    prefix = str(directory).casefold()
    for process in psutil.process_iter(["exe"]):
        try:
            exe = process.info["exe"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if exe.casefold().startswith(prefix):
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.terminate()


def _remove_directory_best_effort(directory: Path, *, attempts: int = 4) -> None:
    """
    尽力删除冒烟临时目录，容忍 Windows 的可执行文件映像锁与杀软瞬时占用。

    每轮删除前先终止占用进程，失败后按指数退避重试；全部失败时保留残留
    在构建目录并打印警告，不让已通过的冒烟因清理失败而退出非零。
    """
    for attempt in range(1, attempts + 1):
        _terminate_processes_from(directory)
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts:
                break
            time.sleep(0.5 * 2 ** (attempt - 1))
    print(f"警告: 冒烟临时目录未能完全删除（文件仍被占用），残留于 {directory}", file=sys.stderr)


def _run_smoke_checks(
    temporary_directory: Path,
    asset: Any,
    offline_pack: Path,
    expected_python_version: str,
) -> None:
    """
    在已创建的临时目录内执行全部冒烟检查。

    依次验证运行时内的 Python 与 uv 可执行、空依赖虚拟环境可以离线创建、
    插件 Worker 的完整生命周期（enable/identity/disable）符合预期。

    :param temporary_directory: 冒烟专用临时目录
    :param asset: 已加载的插件运行时资产描述
    :param offline_pack: 运行时离线 ZIP 路径
    :param expected_python_version: 期望的 Python 版本号
    :raises RuntimeError: 任一版本不匹配或 Worker 生命周期不符合预期时抛出
    """
    data_path = temporary_directory / "data"
    paths = PluginRuntimeStore(data_path).ensure(asset, offline_pack=offline_pack)
    python_version = subprocess.check_output([str(paths.python_path), "--version"], text=True, timeout=20).strip()
    uv_version = subprocess.check_output([str(paths.uv_path), "--version"], text=True, timeout=20).strip()
    if python_version != f"Python {expected_python_version}" or not uv_version.startswith("uv 0.12.16 "):
        raise RuntimeError(f"插件运行时版本不匹配: {python_version}, {uv_version}")
    lock_path = temporary_directory / "empty.lock"
    lock_path.write_bytes(b"")
    spec = PluginEnvironmentSpec(
        runtime_id=asset.runtime_id,
        target_tag="current-platform",
        sdk_version="1",
        python_path=paths.python_path,
        uv_path=paths.uv_path,
        lock_path=lock_path,
    )
    worker_python = PluginEnvironmentPool(data_path).ensure(spec)
    worker_version = subprocess.check_output([str(worker_python), "--version"], text=True, timeout=20).strip()
    if worker_version != python_version:
        raise RuntimeError(f"插件虚拟环境 Python 版本不匹配: {worker_version}")
    plugin_path = temporary_directory / "smoke_plugin"
    plugin_path.mkdir()
    (plugin_path / "plugin.json").write_text(
        json.dumps({"name": "smoke", "entry_point": "main:Plugin"}), encoding="utf-8"
    )
    (plugin_path / "main.py").write_text(
        "import os\n"
        "from ecl_plugin_sdk import Plugin as BasePlugin\n"
        "class Plugin(BasePlugin):\n"
        "    enabled = False\n"
        "    def on_enable(self):\n"
        "        self.enabled = True\n"
        "    def on_disable(self):\n"
        "        self.enabled = False\n"
        "    @BasePlugin.on_command('identity')\n"
        "    def identity(self):\n"
        "        return {'pid': os.getpid(), 'enabled': self.enabled}\n",
        encoding="utf-8",
    )
    with PluginWorkerProcess(worker_python, paths.worker_path, plugin_path) as worker:
        if worker.commands != ("identity",):
            raise RuntimeError("插件运行时未公布 SDK 命令")
        worker.enable()
        active = worker.call_command("identity", {})
        worker.disable()
        inactive = worker.call("identity")
        worker.enable()
        active_again = worker.call_command("identity", {})
    if (
        not isinstance(active, dict)
        or not isinstance(active.get("pid"), int)
        or active["pid"] <= 0
        or active.get("enabled") is not True
        or inactive != {"pid": active["pid"], "enabled": False}
        or active_again != active
    ):
        raise RuntimeError("插件 Worker 生命周期冒烟失败")
    worker_pid = active["pid"]
    print(f"插件运行时冒烟通过: {python_version}, {uv_version}, Worker PID {worker_pid}")


def main(argv: list[str] | None = None) -> int:
    """
    验证发布资产中的 Python、uv 和插件空依赖环境可以启动。

    所有写入位于构建目录的临时子目录，退出时尽力清理：Windows 下文件仍被
    占用时终止占用进程并退避重试，无法清理时保留残留并警告，不使已通过的
    冒烟失败。

    :param argv: 可选命令行参数；缺省时从当前进程读取
    :return: 成功为 0，任一验证失败直接抛出并使 CI 失败
    """
    _force_utf8_output()
    parser = argparse.ArgumentParser(description="验证插件专用运行时资产")
    parser.add_argument("--asset-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--python-version", default="3.12.14")
    args = parser.parse_args(argv)
    asset_paths = list(args.asset_dir.glob("plugin-runtime-*.zip"))
    if len(asset_paths) != 1:
        parser.error("预期恰有一个插件运行时资产")
    asset = load_plugin_runtime_asset(args.manifest)
    temporary_directory = Path(tempfile.mkdtemp(prefix="plugin-smoke-", dir=args.asset_dir))
    try:
        _run_smoke_checks(temporary_directory, asset, asset_paths[0], args.python_version)
    finally:
        _remove_directory_best_effort(temporary_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
