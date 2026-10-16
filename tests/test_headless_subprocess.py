from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ECL.foundation import EventBus
from ECL.host.single_instance import SingleInstanceService

root_path = Path(__file__).resolve().parents[1]


def _run(argv: list[str], *, guard_imports: bool = False) -> subprocess.CompletedProcess[str]:
    """
    在新进程执行真实入口；测试侧拦截器验证传递导入，超时由 subprocess 回收子进程。
    """
    environment = dict(os.environ, PYTHONUTF8="1")
    if guard_imports:
        script = """
import importlib.abc
import runpy
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        prefixes = ('ECL.launcher', 'ECL.application', 'ECL.host', 'ECL.api', 'ECL.services', 'ECL.plugins',
                    'pytauri', 'pytauri_plugins', 'pytauri_utils', 'pytauri_wheel', 'ECL.utils')
        if any(fullname == name or fullname.startswith(name + '.') for name in prefixes):
            raise AssertionError('禁止载入模块：' + fullname)
sys.meta_path.insert(0, Guard())
sys.argv = ['main.py', *sys.argv[1:]]
runpy.run_path('main.py', run_name='__main__')
"""
        command = [sys.executable, "-c", script, *argv]
    else:
        command = [sys.executable, str(root_path / "main.py"), *argv]
    return subprocess.run(
        command,
        cwd=root_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def test_entry_does_not_import_ui_services_or_aggregated_utils(tmp_path) -> None:
    data_path = tmp_path / "unused"
    result = _run(["--headless", "--task=runtime-check", "--data-dir", str(data_path)], guard_imports=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["probe"]["has_ui_modules"] is False
    assert not data_path.exists()


def test_business_files_are_untouched(tmp_path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir()
    names = ("setting.json", "single_instance.json", "pending_debug_maintenance.json", "pending_update.json")
    for name in names:
        (data_path / name).write_text("preserved", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in data_path.iterdir()}
    result = _run(["--headless", "--task=runtime-check", "--data-dir", str(data_path)], guard_imports=True)
    assert result.returncode == 0, result.stderr
    assert {path.name: path.read_bytes() for path in data_path.iterdir()} == before


def test_active_desktop_instance_is_not_notified(tmp_path) -> None:
    events = EventBus()
    received = []
    events.subscribe("launcher:focus_request", received.append)
    service = SingleInstanceService(events, tmp_path, "test")
    service.start()
    try:
        discovery_before = service.discovery_path.read_bytes()
        result = _run(["--headless", "--task=runtime-check", "--data-dir", str(tmp_path)])
        assert result.returncode == 0, result.stderr
        assert not received and not service.take_launch_requests()
        assert service.discovery_path.read_bytes() == discovery_before
    finally:
        service.close()


def test_parallel_tasks_use_independent_results(tmp_path) -> None:
    def run(index):
        result_file = tmp_path / f"result-{index}.json"
        completed = _run(
            [
                "--headless",
                "--task=runtime-check",
                "--data-dir",
                str(tmp_path / "unused"),
                "--result-file",
                str(result_file),
            ]
        )
        assert completed.returncode == 0, completed.stderr
        assert not completed.stdout
        return json.loads(result_file.read_text(encoding="utf-8"))

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(3)))
    assert all(result["success"] for result in results)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result-0.json", "result-1.json", "result-2.json"]
