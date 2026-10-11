from __future__ import annotations

import os
import sys
from pathlib import Path
from time import monotonic, sleep

import psutil
import pytest

from ECL.plugins.runtime.windows import WindowsJob
from ecl_plugin_api._windows import WindowsApi, WindowsRuntimeError


def _wait_file(target_path: Path) -> str:
    deadline = monotonic() + 10
    while not target_path.exists() and monotonic() < deadline:
        sleep(0.01)
    assert target_path.exists()
    return target_path.read_text(encoding="utf-8")


def test_posix_platform_is_explicitly_rejected(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(WindowsRuntimeError) as error:
        WindowsApi()
    assert error.value.error_code == "PLUGIN_RUNTIME_UNSUPPORTED"


@pytest.mark.skipif(sys.platform != "win32", reason="必须使用真实 Windows Job")
def test_process_is_assigned_before_its_first_statement(tmp_path: Path) -> None:
    marker = tmp_path / "entered.txt"
    program = tmp_path / "program.py"
    program.write_text(
        "import pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text('entered')\ntime.sleep(60)\n", encoding="utf-8"
    )
    with WindowsJob() as job:
        process = job.spawn_suspended((sys.executable, str(program), str(marker)), tmp_path, dict(os.environ))
        assert job.contains(process.handle)
        assert job.active_count() == 1
        assert not marker.exists()
        process.resume()
        assert _wait_file(marker) == "entered"
        pid = process.pid
    assert not psutil.pid_exists(pid)


@pytest.mark.skipif(sys.platform != "win32", reason="必须使用真实 Windows Job")
def test_job_recovers_normal_descendant_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child.txt"
    program = tmp_path / "parent.py"
    program.write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x08000000)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    with WindowsJob() as job:
        process = job.spawn_suspended((sys.executable, str(program), str(marker)), tmp_path, dict(os.environ))
        process.resume()
        child_pid = int(_wait_file(marker))
        # venv 重定向器和控制台代理也属于 Job，不能把层数固定为两个。
        assert job.active_count() >= 2
        child_handle = job.api.open_process(child_pid)
        try:
            assert job.contains(child_handle)
        finally:
            job.api.close(child_handle)
        root_pid = process.pid
    assert not psutil.pid_exists(root_pid)
    deadline = monotonic() + 5
    while psutil.pid_exists(child_pid) and monotonic() < deadline:
        sleep(0.01)
    assert not psutil.pid_exists(child_pid)


@pytest.mark.skipif(sys.platform != "win32", reason="必须使用真实 Windows Job")
def test_assignment_failure_never_resumes_root(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "entered.txt"
    program = tmp_path / "program.py"
    program.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('entered')", encoding="utf-8")
    with WindowsJob() as job:

        def fail_assignment(*_args) -> None:
            raise WindowsRuntimeError("测试分配失败", "PLUGIN_JOB_ASSIGN_FAILED")

        monkeypatch.setattr(job.api, "assign", fail_assignment)
        with pytest.raises(WindowsRuntimeError):
            job.spawn_suspended((sys.executable, str(program), str(marker)), tmp_path, dict(os.environ))
        assert not marker.exists()
        assert job.active_count() == 0


@pytest.mark.skipif(sys.platform != "win32", reason="必须使用真实 Windows Job")
def test_job_last_handle_closes_on_abrupt_host_exit(tmp_path: Path) -> None:
    import subprocess

    marker = tmp_path / "pid.txt"
    root_path = Path(__file__).resolve().parents[1]
    source = (
        "import os, sys, pathlib\n"
        "from ECL.plugins.runtime.windows import WindowsJob\n"
        "job = WindowsJob()\n"
        "child = job.spawn_suspended((sys._base_executable, '-c', 'import time; time.sleep(60)'), pathlib.Path.cwd(), dict(os.environ))\n"
        "child.resume()\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "os._exit(0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source, str(marker)],
        cwd=root_path,
        timeout=15,
        capture_output=True,
        creationflags=0x08000000,
    )
    assert result.returncode == 0, result.stderr
    child_pid = int(marker.read_text())
    deadline = monotonic() + 5
    while psutil.pid_exists(child_pid) and monotonic() < deadline:
        sleep(0.01)
    assert not psutil.pid_exists(child_pid)
