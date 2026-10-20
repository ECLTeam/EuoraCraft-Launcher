from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from ECL.plugin_api._windows import WindowsRuntimeError
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugin_api.snapshot import CodeSnapshot
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.supervisor import WorkerSupervisor

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="首版仅 Windows x64")


def _snapshot(tmp_path: Path) -> tuple[CodeSnapshot, Path]:
    code_path = tmp_path / "code"
    code_path.mkdir()
    marker_path = tmp_path / "entered.txt"
    (code_path / "main.py").write_text(
        f"from pathlib import Path\nPath({str(marker_path)!r}).write_text('entered', encoding='utf-8')\n",
        encoding="utf-8",
    )
    return CodeSnapshot.capture(code_path), marker_path


def test_worker_waits_for_permission_before_plugin_code(tmp_path: Path) -> None:
    snapshot, marker = _snapshot(tmp_path)
    supervisor = WorkerSupervisor()
    try:
        hello = supervisor.start()
        assert hello["has_host_modules"] is False
        assert hello["is_in_job"] is True
        assert supervisor.pid != os.getpid()
        assert supervisor.state == "awaiting_authorization"
        assert not marker.exists()
        supervisor._authorize_snapshot(snapshot)
        assert marker.read_text(encoding="utf-8") == "entered"
        reply = supervisor.request("runtime.info")
        assert reply["ok"] is True
        assert reply["data"]["pid"] == supervisor.pid
        assert supervisor.request("frontend.invoke")["error"]["code"] == "PLUGIN_API_METHOD_NOT_FOUND"
        pid = supervisor.pid
    finally:
        supervisor.close()
    assert supervisor.active_count() == 0
    assert not psutil.pid_exists(pid)


def test_close_before_load_never_executes_plugin(tmp_path: Path) -> None:
    _snapshot_value, marker = _snapshot(tmp_path)
    with WorkerSupervisor() as supervisor:
        assert supervisor.state == "awaiting_authorization"
        assert not marker.exists()
    assert not marker.exists()


def test_changed_snapshot_is_not_executed(tmp_path: Path) -> None:
    snapshot, marker = _snapshot(tmp_path)
    (snapshot.root_path / "main.py").write_text("raise RuntimeError('changed')", encoding="utf-8")
    with WorkerSupervisor() as supervisor, pytest.raises(ProtocolError):
        supervisor._authorize_snapshot(snapshot)
    assert not marker.exists()


def test_plugin_exception_is_reported_without_private_exception_text(tmp_path: Path) -> None:
    snapshot, marker = _snapshot(tmp_path)
    (snapshot.root_path / "main.py").write_text("raise ValueError('private-token-must-not-leak')", encoding="utf-8")
    with WorkerSupervisor() as supervisor:
        with pytest.raises(WindowsRuntimeError) as error:
            supervisor._authorize_snapshot(CodeSnapshot.capture(snapshot.root_path))
        assert error.value.error_code == "PLUGIN_WORKER_EXECUTION_FAILED"
        assert "private-token" not in str(error.value)
    assert not marker.exists()


def test_frozen_worker_gate_with_real_artifact(tmp_path: Path) -> None:
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if not artifact:
        pytest.skip("需要显式提供真实发行物，不能以源码测试替代")
    builder = os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka")
    executable = ExecutableLaunchSpec(Path(artifact).resolve(), None, builder)
    snapshot, marker = _snapshot(tmp_path)
    with WorkerSupervisor(executable) as supervisor:
        assert not marker.exists()
        assert supervisor.image_path == executable.executable_path
        supervisor._authorize_snapshot(snapshot)
        assert marker.read_text(encoding="utf-8") == "entered"
        assert supervisor.request("runtime.info")["ok"] is True


def test_frozen_runtime_resolves_its_original_executable(tmp_path: Path) -> None:
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if not artifact:
        pytest.skip("需要真实冻结发行物检查运行时入口，不能仅 mock 构建器字段")
    builder = os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka")
    executable = ExecutableLaunchSpec(Path(artifact).resolve(), None, builder)
    code_path = tmp_path / "code"
    code_path.mkdir()
    report_path = tmp_path / "executable.json"
    # 这是受信测试载荷，专门从实际冻结 VM 检查宿主解析器，不开放新的 Worker 方法。
    (code_path / "main.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "from ECL.plugins.runtime.executable import ExecutableLaunchSpec\n"
        "spec = ExecutableLaunchSpec.current()\n"
        f"Path({str(report_path)!r}).write_text(json.dumps({{"
        "'executable':str(spec.executable_path),'project':str(spec.project_path),"
        "'builder':spec.builder_kind}),encoding='utf-8')\n",
        encoding="utf-8",
    )
    with WorkerSupervisor(executable) as supervisor:
        supervisor._authorize_snapshot(CodeSnapshot.capture(code_path))
    result = json.loads(report_path.read_text(encoding="utf-8"))
    assert Path(result["executable"]).resolve() == executable.executable_path
    assert result["project"] == "None"
    assert result["builder"] == builder
