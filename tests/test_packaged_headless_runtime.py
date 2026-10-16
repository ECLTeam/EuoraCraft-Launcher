from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_packaged_headless_task_without_graphical_session(tmp_path) -> None:
    configured_executable = os.environ.get("ECL_HEADLESS_SMOKE_EXECUTABLE")
    if not configured_executable:
        pytest.skip("需要显式指定当前构建产物")
    executable_path = Path(configured_executable).resolve()
    assert executable_path.is_file(), "指定的当前构建产物不存在"
    result_file = tmp_path / "result.json"
    data_path = tmp_path / "data"
    data_path.mkdir()
    settings_file = data_path / "setting.json"
    settings_file.write_text("preserved", encoding="utf-8")
    environment = dict(os.environ, PYTHONUTF8="1")
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        environment.pop(key, None)
    completed = subprocess.run(
        [
            str(executable_path),
            "--headless",
            "--task=runtime-check",
            "--data-dir",
            str(data_path),
            "--result-file",
            str(result_file),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["success"] and result["probe"]["is_frozen"]
    assert not result["probe"]["has_ui_modules"]
    assert {item["name"] for item in result["probe"]["dependencies"]} == {"httpx", "pydantic", "psutil"}
    assert settings_file.read_text(encoding="utf-8") == "preserved"
    assert list(data_path.iterdir()) == [settings_file]
    assert not completed.stdout

    # 同一目标不能覆盖，错误参数也不得落入桌面入口。
    rejected = subprocess.run(
        [str(executable_path), "--headless", "--task=runtime-check", "--result-file", str(result_file)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert rejected.returncode == 2
    assert json.loads(result_file.read_text(encoding="utf-8")) == result
