# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：从固定 CPython、uv 和 Worker 脚本构建插件专用运行时发行资产。
#
# 公开接口：
#   - main(argv) -> int — 构建运行时 ZIP 与可嵌入启动器的固定哈希清单。
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

default_python_version = "3.12.14"
required_uv_version = "0.12.16"
copy_chunk_bytes = 1024**2
safe_tag_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _uv_executable() -> Path:
    executable_name = "uv.exe" if os.name == "nt" else "uv"
    adjacent_path = Path(sys.executable).with_name(executable_name)
    if adjacent_path.is_file():
        return adjacent_path
    discovered = shutil.which("uv")
    if discovered is None:
        raise RuntimeError("构建插件运行时需要 uv 可执行文件")
    return Path(discovered)


def _check_uv_version(uv_path: Path) -> None:
    result = subprocess.run([str(uv_path), "--version"], capture_output=True, text=True, check=True, timeout=15)
    if re.match(rf"^uv {re.escape(required_uv_version)}(?:\s|$)", result.stdout.strip()) is None:
        raise RuntimeError(f"插件运行时需要 uv {required_uv_version}，实际为 {result.stdout.strip()}")


def _managed_python(uv_path: Path, install_path: Path, python_version: str) -> tuple[Path, Path]:
    """
    把固定 CPython 安装到构建临时目录并定位真实版本目录，避开可移动别名。
    """
    environment = dict(os.environ)
    environment["UV_PYTHON_INSTALL_DIR"] = str(install_path)
    subprocess.run(
        [
            str(uv_path),
            "--no-config",
            "python",
            "install",
            python_version,
            "--install-dir",
            str(install_path),
            "--no-bin",
            "--no-registry",
        ],
        check=True,
        timeout=600,
        env=environment,
    )
    result = subprocess.run(
        [
            str(uv_path),
            "--no-config",
            "python",
            "find",
            "--managed-python",
            "--no-python-downloads",
            "--resolve-links",
            python_version,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    python_path = Path(result.stdout.strip()).resolve()
    try:
        relative_path = python_path.relative_to(install_path.resolve())
    except ValueError as exc:
        raise RuntimeError("uv 返回的 Python 不在构建目录内") from exc
    distribution_path = install_path / relative_path.parts[0]
    return distribution_path, python_path.relative_to(distribution_path)


def _write_archive(
    archive_path: Path, distribution_path: Path, python_relpath: Path, uv_path: Path
) -> tuple[str, str, str]:
    """
    将便携 CPython、独立 uv 与纯标准库 Worker 写入确定路径。
    """
    python_name = (Path("python") / python_relpath).as_posix()
    uv_name = (Path("uv") / uv_path.name).as_posix()
    worker_name = "worker.py"
    worker_path = Path(__file__).with_name("plugin_worker.py")
    files = sorted(
        entry
        for entry in distribution_path.rglob("*")
        if entry.is_file()
        and "site-packages" not in entry.relative_to(distribution_path).parts
        and entry.relative_to(distribution_path).parts[0].casefold() != "include"
    )
    # Linux 发行树存在仅大小写不同的路径（如 share/terminfo 下的 Eterm/eterm），
    # 资产必须保证大小写折叠唯一才能在不敏感文件系统上安全解包，故只保留排序在前的条目。
    seen_folded: set[str] = set()
    unique_files: list[Path] = []
    for entry in files:
        folded = (Path("python") / entry.relative_to(distribution_path)).as_posix().casefold()
        if folded in seen_folded:
            continue
        seen_folded.add(folded)
        unique_files.append(entry)
    for entry in distribution_path.rglob("*"):
        if entry.is_symlink() and entry.is_dir():
            raise RuntimeError(f"运行时包含不支持的目录符号链接: {entry}")
    with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
        for source_path in unique_files:
            name = (Path("python") / source_path.relative_to(distribution_path)).as_posix()
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | (source_path.stat().st_mode & 0o777)) << 16
            with source_path.open("rb") as source, archive.open(entry, "w") as target:
                shutil.copyfileobj(source, target, copy_chunk_bytes)
        uv_entry = zipfile.ZipInfo(uv_name, date_time=(1980, 1, 1, 0, 0, 0))
        uv_entry.compress_type = zipfile.ZIP_DEFLATED
        uv_entry.external_attr = (stat.S_IFREG | (uv_path.stat().st_mode & 0o777)) << 16
        with uv_path.open("rb") as source, archive.open(uv_entry, "w") as target:
            shutil.copyfileobj(source, target, copy_chunk_bytes)
        worker_entry = zipfile.ZipInfo(worker_name, date_time=(1980, 1, 1, 0, 0, 0))
        worker_entry.compress_type = zipfile.ZIP_DEFLATED
        worker_entry.external_attr = (stat.S_IFREG | 0o644) << 16
        with worker_path.open("rb") as source, archive.open(worker_entry, "w") as target:
            shutil.copyfileobj(source, target, copy_chunk_bytes)
    return python_name, uv_name, worker_name


def _sha256(archive_path: Path) -> str:
    digest = hashlib.sha256()
    with archive_path.open("rb") as source:
        while chunk := source.read(copy_chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _force_utf8_output() -> None:
    """
    把标准输出与标准错误切换为 UTF-8，避免 ANSI 代码页控制台无法编码中文。

    GitHub 的 Windows 运行器与部分本地终端以 ANSI 代码页（如 cp1252）作为标准流
    编码，中文 print 与 argparse 帮助会直接抛出 UnicodeEncodeError 使构建失败；
    本身已是 UTF-8 的环境重配为幂等操作，被替换的非 TextIOWrapper 流则跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """
    构建插件运行时 ZIP 与启动器内嵌资产清单。

    CI 必须在打包启动器前执行，以便只把固定摘要与下载地址放进单文件；
    运行时 ZIP 作为独立发行资产上传。无标签构建仅支持本地离线导入。

    :param argv: 可选命令行参数；缺省时从当前进程读取
    :return: 成功为 0，失败由异常终止构建
    """
    _force_utf8_output()
    parser = argparse.ArgumentParser(description="构建插件专用 Python 运行时")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--python-version", default=default_python_version)
    parser.add_argument("--python-root", type=Path, help="测试已有便携 Python 目录时使用")
    parser.add_argument("--python-executable", type=Path, help="相对 --python-root 的可执行文件")
    args = parser.parse_args(argv)
    output_path = args.output_dir.resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    uv_path = _uv_executable()
    _check_uv_version(uv_path)
    with tempfile.TemporaryDirectory(prefix="runtime-build-", dir=output_path) as working_dir:
        working_path = Path(working_dir)
        if args.python_root is not None:
            if args.python_executable is None:
                parser.error("提供 --python-root 时还须提供 --python-executable")
            distribution_path = args.python_root.resolve()
            python_relpath = args.python_executable
            if python_relpath.is_absolute() or not (distribution_path / python_relpath).resolve().is_relative_to(
                distribution_path
            ):
                parser.error("指定的 Python 可执行文件超出运行时目录")
            if not (distribution_path / python_relpath).is_file():
                parser.error("指定的 Python 可执行文件不存在")
        else:
            distribution_path, python_relpath = _managed_python(uv_path, working_path / "managed", args.python_version)
        temporary_archive = working_path / "plugin-runtime.zip"
        python_name, uv_name, worker_name = _write_archive(
            temporary_archive, distribution_path, python_relpath, uv_path
        )
        digest = _sha256(temporary_archive)
        os_tag = platform.system().lower()
        machine_tag = platform.machine().lower().replace("amd64", "x86_64")
        runtime_id = f"pr-{digest[:20]}"
        if safe_tag_pattern.fullmatch(runtime_id) is None:
            raise RuntimeError("生成的运行时标识含非法字符")
        filename = f"plugin-runtime-{args.python_version}-{os_tag}-{machine_tag}-{runtime_id}.zip"
        archive_path = output_path / filename
        temporary_archive.replace(archive_path)
        release_tag = os.environ.get("GITHUB_REF_NAME") if os.environ.get("GITHUB_REF_TYPE") == "tag" else None
        repository = os.environ.get("GITHUB_REPOSITORY", "ECLTeam/EuoraCraft-Launcher")
        download_url = (
            f"https://github.com/{repository}/releases/download/{quote(release_tag)}/{quote(filename)}"
            if release_tag
            else None
        )
        manifest = {
            "runtime_id": runtime_id,
            "sha256": digest,
            "archive_size_bytes": archive_path.stat().st_size,
            "python_relpath": python_name,
            "uv_relpath": uv_name,
            "worker_relpath": worker_name,
            "download_url": download_url,
        }
        manifest_path = args.manifest.resolve()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        partial_manifest = manifest_path.with_name(f".{manifest_path.name}.partial")
        partial_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        partial_manifest.replace(manifest_path)
        print(f"插件运行时资产: {archive_path} ({archive_path.stat().st_size} bytes)")
        print(f"清单: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
