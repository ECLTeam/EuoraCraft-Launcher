from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_build_plugin_runtime_prints_chinese_on_ansi_codepage_console():
    """
    验证插件运行时构建脚本在标准流被强制为 cp1252 时仍能输出中文。

    GitHub 的 Windows 运行器与部分本地终端使用 cp1252 等 ANSI 代码页作为标准流
    编码；脚本必须在入口把输出流切换为 UTF-8，否则第一处中文输出（含 argparse
    帮助文本）就会抛出 UnicodeEncodeError 使构建失败。修复前该用例因子进程
    异常退出而失败。
    """
    build_script_path = Path(__file__).resolve().parents[1] / "packaging" / "build-plugin-runtime.py"
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "cp1252"
    environment.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, str(build_script_path), "--help"],
        capture_output=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"UnicodeEncodeError" not in result.stderr
