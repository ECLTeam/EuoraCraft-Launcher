from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ECL.plugins.worker_process import PluginWorkerCallError, PluginWorkerError, PluginWorkerProcess


def _worker_script() -> Path:
    return Path(__file__).resolve().parent.parent / "packaging" / "plugin_worker.py"


def _plugin(code_path: Path, value: str) -> None:
    code_path.mkdir()
    (code_path / "plugin.json").write_text('{"name":"demo","entry_point":"main:Plugin"}', encoding="utf-8")
    (code_path / "helper.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
    (code_path / "main.py").write_text(
        "import os\n"
        "import time\n"
        "from helper import VALUE\n"
        "class Plugin:\n"
        "    def read(self):\n"
        "        return {'value': VALUE, 'pid': os.getpid()}\n"
        "    def echo(self, value):\n"
        "        return value\n"
        "    def fail(self):\n"
        "        raise ValueError('expected failure')\n"
        "    def delay(self):\n"
        "        time.sleep(2)\n",
        encoding="utf-8",
    )


def test_workers_keep_module_state_and_processes_separate(tmp_path: Path) -> None:
    """
    同名模块在两个 Worker 各自导入，宿主解释器不接触插件模块。
    """
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    _plugin(first_path, "first")
    _plugin(second_path, "second")
    with (
        PluginWorkerProcess(Path(sys.executable), _worker_script(), first_path) as first,
        PluginWorkerProcess(Path(sys.executable), _worker_script(), second_path) as second,
    ):
        first_result = first.call("read")
        second_result = second.call("read")
        assert first_result == {"value": "first", "pid": first_result["pid"]}
        assert second_result == {"value": "second", "pid": second_result["pid"]}
        assert first_result["pid"] != second_result["pid"] != os.getpid()
        assert second.call("echo", {"nested": [1, True, "ok"]}) == {"nested": [1, True, "ok"]}
    assert "helper" not in sys.modules
    assert "ecl_worker_plugin_main" not in sys.modules


def test_plugin_exception_does_not_destroy_healthy_worker(tmp_path: Path) -> None:
    """
    插件方法失败返回明确错误，后续合法调用仍可继续。
    """
    code_path = tmp_path / "plugin"
    _plugin(code_path, "working")
    with PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path) as worker:
        with pytest.raises(PluginWorkerCallError, match="expected failure"):
            worker.call("fail")
        assert worker.call("read")["value"] == "working"
        with pytest.raises(PluginWorkerCallError, match="方法调用参数无效"):
            worker.call("_private")


def test_worker_timeout_terminates_child(tmp_path: Path) -> None:
    """
    阻塞插件方法超时后不能继续占用 Worker 子进程。
    """
    code_path = tmp_path / "plugin"
    _plugin(code_path, "slow")
    worker = PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path)
    worker.start()
    process = worker._process
    assert process is not None
    with pytest.raises(PluginWorkerError, match="响应超时"):
        worker.call("delay", timeout=0.1)
    assert process.poll() is not None


def test_worker_rejects_invalid_entry_without_host_import(tmp_path: Path) -> None:
    """
    插件入口错误只导致 Worker 启动失败，不会在宿主执行模块。
    """
    code_path = tmp_path / "plugin"
    code_path.mkdir()
    (code_path / "plugin.json").write_text('{"entry_point":"../outside:Plugin"}', encoding="utf-8")
    with pytest.raises(PluginWorkerError, match="插件入口"):
        PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path).start()


def test_worker_rejects_unbounded_timeout(tmp_path: Path) -> None:
    """
    调用方不能把等待时间改为无穷，绕过 Worker 有界调用约束。
    """
    code_path = tmp_path / "plugin"
    _plugin(code_path, "bounded")
    with PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path) as worker:
        with pytest.raises(PluginWorkerError, match="超时必须"):
            worker.call("read", timeout=float("inf"))
        assert worker.call("read")["value"] == "bounded"
