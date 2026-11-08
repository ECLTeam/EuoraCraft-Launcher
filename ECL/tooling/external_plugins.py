# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按锁文件准备外置插件依赖，构建插件包并安装到指定的本地目录。
#
# 公开接口：
#   - main — 构建两个普通插件，可选下载已锁定的 wheel 或安装到本地目录。
# ============================================================

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from ECL.plugins.package_archive import build_plugin_package


def _download_wheels(source_path: Path, download_target: str | None = None) -> None:
    """
    只下载锁文件中已固定版本和哈希的二进制依赖，不向宿主环境安装。
    """
    for lock_path in sorted((source_path / "locks").glob("*.txt")):
        target_name = lock_path.stem
        if download_target is not None and target_name != download_target:
            continue
        python_version = target_name.rsplit("-cp", 1)[1]
        wheel_platforms = (
            ("win_amd64",)
            if target_name.startswith("windows-")
            else ("manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64")
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--require-hashes",
                "--no-deps",
                "--implementation=cp",
                f"--python-version={python_version}",
                f"--abi=cp{python_version}",
                *(f"--platform={platform}" for platform in wheel_platforms),
                "--dest",
                str(source_path / "wheels" / target_name),
                "--requirement",
                str(lock_path),
            ],
            check=True,
        )


def main() -> None:
    """
    构建并校验插件归档，可选在空闲的新目录中原子安装插件。

    安装不批准权限，不覆盖已有同名插件，也不改动其他插件。
    """
    parser = argparse.ArgumentParser(description="构建 ECL 外置联机与 QomicEX 兼容插件")
    parser.add_argument("--output", type=Path, default=Path("output/plugins"))
    parser.add_argument("--download", action="store_true", help="按锁文件下载 Windows/Linux 的二进制依赖")
    parser.add_argument(
        "--download-target",
        choices=("windows-x86_64-cp311", "windows-x86_64-cp312", "linux-x86_64-cp312"),
        help="只准备指定目标的依赖，需同时使用 --download",
    )
    parser.add_argument("--install-dir", type=Path, help="可选的实际插件目录，不覆盖现有插件")
    arguments = parser.parse_args()
    if arguments.download_target is not None and not arguments.download:
        parser.error("--download-target 需要同时使用 --download")
    source_root = Path(__file__).resolve().parents[2] / "external_plugins"
    if arguments.install_dir is not None:
        arguments.install_dir.mkdir(parents=True, exist_ok=True)
        for directory in ("ecl_onlinegame", "qomicex_compatible"):
            name = json.loads((source_root / directory / "plugin.json").read_bytes())["name"]
            if (arguments.install_dir / name).exists():
                parser.error(f"目标插件已存在，保留原目录：{name}")
    for directory in ("ecl_onlinegame", "qomicex_compatible"):
        source_path = source_root / directory
        if arguments.download:
            _download_wheels(source_path, arguments.download_target)
        metadata = json.loads((source_path / "plugin.json").read_bytes())
        if metadata.get("pythonDependencies") and not any((source_path / "wheels").glob("*/*.whl")):
            parser.error("联机插件尚未准备 wheel，请先使用 --download")
        with TemporaryDirectory(prefix="ecl-plugin-") as staging_directory:
            staging_path = Path(staging_directory) / metadata["name"]
            shutil.copytree(source_path, staging_path, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            archive_path = arguments.output / f"{metadata['name']}.eclplugin"
            build_plugin_package(staging_path, archive_path)
            print(f"插件包已校验：{archive_path}")
            if arguments.install_dir is not None:
                install_root = arguments.install_dir.resolve()
                target_path = install_root / metadata["name"]
                with TemporaryDirectory(prefix=".ecl-install-", dir=install_root) as installation_directory:
                    prepared_path = Path(installation_directory) / metadata["name"]
                    shutil.copytree(staging_path, prepared_path)
                    prepared_path.replace(target_path)
                print(f"插件已放置：{target_path}")


if __name__ == "__main__":
    main()
