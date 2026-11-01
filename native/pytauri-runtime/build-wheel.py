# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：从固定源码与摘要构建插件窗口运行时，工具和产物仅写入独立目录。
#
# 公开接口：
#   - main — 校验输入、构建两份 Wheel 并写入带摘要的安装清单。
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _run(arguments: list[str], *, cwd: Path | None = None, environment: dict[str, str] | None = None) -> None:
    """
    有界执行外部构建步骤；非零退出不得继续生成成功清单。
    """
    subprocess.run(arguments, cwd=cwd, env=environment, check=True, timeout=3600)


def _capture(arguments: list[str]) -> str:
    """
    有界取得工具身份，不执行 shell 字符串或拼装的脚本。
    """
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def _digest(file_path: Path) -> str:
    return hashlib.sha256(file_path.read_bytes()).hexdigest()


def _assert_digest(file_path: Path, expected: str) -> None:
    if _digest(file_path) != expected:
        raise ValueError(f"构建输入摘要不一致：{file_path.name}")


def main() -> None:
    """
    验证固定工具链和输入后，在全新源码目录构建并记录实际产物。

    不重置已有源码目录、不安装到全局环境；构建失败时保留现场。
    """
    parser = argparse.ArgumentParser(description="构建固定的插件原生窗口运行时")
    parser.add_argument("--output-directory", type=Path, default=Path("build/native-runtime"))
    parser.add_argument("--source-repository")
    args = parser.parse_args()
    script_path = Path(__file__).resolve().parent
    project_path = script_path.parents[1]
    output_path = (project_path / args.output_directory).resolve()
    source_path = output_path / "source"
    tools_path = output_path / "tools"
    wheels_path = output_path / "wheels"
    if source_path.exists():
        raise ValueError("源码目录已存在；请使用新的输出目录，构建不会重置工作目录")
    if sys.version_info[:2] not in {(3, 11), (3, 12)} or sys.platform not in {"win32", "linux", "darwin"}:
        raise ValueError("需要 Windows/Linux/macOS 和 Python 3.11/3.12")
    manifest = json.loads((script_path / "source.json").read_text(encoding="utf-8"))
    compiler = _capture(["rustc", "--version"])
    if not compiler.startswith(f"rustc {manifest['rust_version']} "):
        raise ValueError("Rust 工具链与固定清单不一致")
    patch_path = script_path / "runtime.patch"
    _assert_digest(patch_path, manifest["patch_sha256"])
    policies = manifest["policies"]
    expected_names = {
        "plugin_isolation.rs",
        "plugin_isolation_unix.rs",
        "plugin_isolation_gtk.rs",
        "plugin_isolation_macos.rs",
    }
    if set(policies) != expected_names:
        raise ValueError("原生策略输入不完整")
    for name, expected in policies.items():
        _assert_digest(script_path / name, expected)
    wheels_path.mkdir(parents=True, exist_ok=False)
    _run(
        [
            "git",
            "clone",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "--no-checkout",
            args.source_repository or manifest["upstream"],
            str(source_path),
        ]
    )
    _run(["git", "-C", str(source_path), "checkout", "--detach", manifest["commit"]])
    if _capture(["git", "-C", str(source_path), "rev-parse", "HEAD"]) != manifest["commit"]:
        raise ValueError("源码身份不一致")
    _run(["git", "-C", str(source_path), "apply", "--check", str(patch_path)])
    _run(["git", "-C", str(source_path), "apply", str(patch_path)])
    for name in policies:
        shutil.copyfile(script_path / name, source_path / "crates/pytauri-core/src" / name)
    _assert_digest(source_path / "Cargo.lock", manifest["cargo_lock_sha256"])
    shutil.copyfile(source_path / "LICENSE", source_path / "python/pytauri-wheel/LICENSE")
    shutil.copyfile(project_path / "LICENSE", source_path / "python/pytauri-wheel/ECL-LICENSE")
    _run([sys.executable, "-m", "venv", str(tools_path)])
    tool_python = tools_path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    _run(
        [
            str(tool_python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--no-deps",
            "-r",
            str(script_path / "build-tools.txt"),
        ]
    )
    environment = dict(os.environ)
    environment.update(
        PYO3_PYTHON=sys.executable,
        CARGO_BUILD_JOBS="2",
        ECL_EXPECTED_WEBVIEW2_VERSION=manifest["webview2_version"],
        ECL_EXPECTED_WKWEBVIEW_VERSION=manifest["wkwebview_version"],
    )
    environment.setdefault("CARGO_TARGET_DIR", str(output_path / "target"))
    _run(
        [
            str(tool_python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "python/pytauri-wheel",
            "python/pytauri",
            "--wheel-dir",
            str(wheels_path),
        ],
        cwd=source_path,
        environment=environment,
    )
    abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheels = [
        *wheels_path.glob(f"pytauri_wheel-{manifest['version']}-{abi}-*.whl"),
        *wheels_path.glob(f"pytauri-{manifest['version']}-py3-none-any.whl"),
    ]
    if len(wheels) != 2:
        raise ValueError("构建产物数量或 ABI 不符")
    requirements = [
        f"{name}=={manifest['version']} --hash=sha256:{_digest(wheel)}"
        for name, wheel in zip(("pytauri-wheel", "pytauri"), wheels, strict=True)
    ]
    (output_path / "install.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    result = {
        "upstream_commit": manifest["commit"],
        "patch_sha256": manifest["patch_sha256"],
        "policies": policies,
        "cargo_lock_sha256": manifest["cargo_lock_sha256"],
        "compiler": compiler,
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python_abi": abi,
        "webview2_version": manifest["webview2_version"],
        "wkwebview_version": manifest["wkwebview_version"],
        "wheels": [{"name": wheel.name, "sha256": _digest(wheel)} for wheel in wheels],
    }
    (output_path / "build-manifest.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"固定运行时构建完成；安装清单位于：{output_path / 'install.txt'}")


if __name__ == "__main__":
    main()
