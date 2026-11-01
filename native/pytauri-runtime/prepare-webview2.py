# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为原生门禁准备微软固定版本 WebView2，不依赖 CI 镜像的自动更新运行时。
#
# 公开接口：
#   - main — 校验固定下载摘要并展开到全新独立验收目录。
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import urlopen


def _download(url: str, target_path: Path, expected_digest: str) -> None:
    """
    有界下载并验证固定包；不完整或摘要不符的文件保留为未发布临时文件。
    """
    digest = hashlib.sha256()
    total = 0
    deadline = monotonic() + 360
    pending_path = target_path.with_suffix(".partial")
    with urlopen(url, timeout=60) as response, pending_path.open("xb") as output:
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > 512 * 1024 * 1024 or monotonic() >= deadline:
                raise ValueError("固定 WebView2 下载超出大小或时间上限")
            digest.update(block)
            output.write(block)
    if not total or digest.hexdigest() != expected_digest:
        raise ValueError("固定 WebView2 包摘要不一致")
    pending_path.replace(target_path)


def main() -> None:
    """
    将已校验的官方固定运行时展开到独立目录，仅供真实 GUI 门禁显式选择。

    不更新或替换系统安装；已有输出目录会明确拒绝，失败时保留现场。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="准备固定 WebView2 原生验收环境")
    parser.add_argument("--output-directory", type=Path, default=Path("build/webview2-fixed"))
    args = parser.parse_args()
    if sys.platform != "win32":
        raise ValueError("固定 WebView2 仅用于 Windows 验收")
    manifest = json.loads((Path(__file__).resolve().parent / "source.json").read_text(encoding="utf-8"))
    runtime = manifest["webview2_fixed_runtime"]
    url = runtime["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "msedge.sf.dl.delivery.mp.microsoft.com":
        raise ValueError("固定 WebView2 下载必须使用微软官方来源")
    root_path = args.output_directory.resolve()
    root_path.mkdir(parents=True, exist_ok=False)
    archive_path = root_path / "runtime.cab"
    _download(url, archive_path, runtime["sha256"])
    expanded_path = root_path / "runtime"
    expanded_path.mkdir()
    subprocess.run(
        ["expand", str(archive_path), "-F:*", str(expanded_path)], check=True, timeout=300, stdout=subprocess.DEVNULL
    )
    executables = list(expanded_path.rglob("msedgewebview2.exe"))
    if len(executables) != 1:
        raise ValueError("固定 WebView2 展开结果无效")
    browser_path = executables[0].parent
    subprocess.run(
        [
            "icacls",
            str(browser_path),
            "/grant",
            "*S-1-15-2-1:(OI)(CI)(RX)",
            "/grant",
            "*S-1-15-2-2:(OI)(CI)(RX)",
            "/T",
            "/Q",
        ],
        check=True,
        timeout=90,
        stdout=subprocess.DEVNULL,
    )
    (root_path / "install-path.txt").write_text(str(browser_path), encoding="utf-8")
    print(f"固定 WebView2 验收环境已就绪，版本：{manifest['webview2_version']}")


if __name__ == "__main__":
    main()
