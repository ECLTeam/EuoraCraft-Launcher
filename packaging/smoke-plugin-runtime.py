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
import json
import subprocess
import tempfile
from pathlib import Path

from ECL.plugins.environment_pool import PluginEnvironmentPool, PluginEnvironmentSpec
from ECL.plugins.runtime_assets import PluginRuntimeStore, load_plugin_runtime_asset
from ECL.plugins.worker_process import PluginWorkerProcess


def main(argv: list[str] | None = None) -> int:
    """
    验证发布资产中的 Python、uv 和插件空依赖环境可以启动。

    所有写入位于构建目录的临时子目录，退出时由临时目录清理。

    :param argv: 可选命令行参数；缺省时从当前进程读取
    :return: 成功为 0，任一验证失败直接抛出并使 CI 失败
    """
    parser = argparse.ArgumentParser(description="验证插件专用运行时资产")
    parser.add_argument("--asset-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--python-version", default="3.12.14")
    args = parser.parse_args(argv)
    asset_paths = list(args.asset_dir.glob("plugin-runtime-*.zip"))
    if len(asset_paths) != 1:
        parser.error("预期恰有一个插件运行时资产")
    asset = load_plugin_runtime_asset(args.manifest)
    with tempfile.TemporaryDirectory(prefix="plugin-smoke-", dir=args.asset_dir) as temporary_directory:
        data_path = Path(temporary_directory) / "data"
        paths = PluginRuntimeStore(data_path).ensure(asset, offline_pack=asset_paths[0])
        python_version = subprocess.check_output([str(paths.python_path), "--version"], text=True, timeout=20).strip()
        uv_version = subprocess.check_output([str(paths.uv_path), "--version"], text=True, timeout=20).strip()
        if python_version != f"Python {args.python_version}" or not uv_version.startswith("uv 0.12.16 "):
            raise RuntimeError(f"插件运行时版本不匹配: {python_version}, {uv_version}")
        lock_path = Path(temporary_directory) / "empty.lock"
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
        plugin_path = Path(temporary_directory) / "smoke_plugin"
        plugin_path.mkdir()
        (plugin_path / "plugin.json").write_text(
            json.dumps({"name": "smoke", "entry_point": "main:Plugin"}), encoding="utf-8"
        )
        (plugin_path / "main.py").write_text(
            "import os\n"
            "class Plugin:\n"
            "    enabled = False\n"
            "    def on_enable(self):\n"
            "        self.enabled = True\n"
            "    def on_disable(self):\n"
            "        self.enabled = False\n"
            "    def identity(self):\n"
            "        return {'pid': os.getpid(), 'enabled': self.enabled}\n",
            encoding="utf-8",
        )
        with PluginWorkerProcess(worker_python, paths.worker_path, plugin_path) as worker:
            worker.enable()
            active = worker.call("identity")
            worker.disable()
            inactive = worker.call("identity")
            worker.enable()
            active_again = worker.call("identity")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
