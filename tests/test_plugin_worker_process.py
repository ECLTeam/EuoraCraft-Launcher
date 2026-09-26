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


def test_worker_lifecycle_hooks_are_ordered_and_idempotent(tmp_path: Path) -> None:
    """
    插件生命周期只在独立 Worker 中执行，重复启停不重复调用钩子。
    """
    code_path = tmp_path / "plugin"
    code_path.mkdir()
    (code_path / "plugin.json").write_text('{"name":"demo","entry_point":"main:Plugin"}', encoding="utf-8")
    (code_path / "main.py").write_text(
        "from pathlib import Path\n"
        "class Plugin:\n"
        "    def __init__(self):\n"
        "        self.events = []\n"
        "    def on_load(self):\n"
        "        self.events.append('load')\n"
        "    def on_enable(self):\n"
        "        self.events.append('enable')\n"
        "    def on_disable(self):\n"
        "        self.events.append('disable')\n"
        "    def on_unload(self):\n"
        "        self.events.append('unload')\n"
        "        Path(__file__).with_name('events.txt').write_text(','.join(self.events))\n"
        "    def snapshot(self):\n"
        "        return self.events\n",
        encoding="utf-8",
    )
    with PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path) as worker:
        assert worker.call("snapshot") == ["load"]
        worker.enable()
        worker.enable()
        assert worker.call("snapshot") == ["load", "enable"]
        worker.disable()
        worker.disable()
        assert worker.call("snapshot") == ["load", "enable", "disable"]
        worker.enable()
    assert (code_path / "events.txt").read_text(encoding="utf-8") == "load,enable,disable,enable,disable,unload"


def test_worker_enable_failure_closes_process(tmp_path: Path) -> None:
    """
    启用钩子失败时不保留状态不明的插件进程。
    """
    code_path = tmp_path / "plugin"
    code_path.mkdir()
    (code_path / "plugin.json").write_text('{"name":"demo","entry_point":"main:Plugin"}', encoding="utf-8")
    (code_path / "main.py").write_text(
        "from pathlib import Path\n"
        "class Plugin:\n"
        "    def on_enable(self):\n"
        "        raise RuntimeError('enable failed')\n"
        "    def on_unload(self):\n"
        "        Path(__file__).with_name('unloaded.txt').write_text('yes')\n",
        encoding="utf-8",
    )
    worker = PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path)
    worker.start()
    process = worker._process
    assert process is not None
    with pytest.raises(PluginWorkerError, match="enable failed"):
        worker.enable()
    assert process.poll() is not None
    assert (code_path / "unloaded.txt").read_text(encoding="utf-8") == "yes"


def test_worker_crash_becomes_connection_error_and_is_reaped(tmp_path: Path) -> None:
    """
    子进程突然退出时关闭连接并回收进程，不向调用方泄漏 EOFError。
    """
    code_path = tmp_path / "plugin"
    code_path.mkdir()
    (code_path / "plugin.json").write_text('{"name":"demo","entry_point":"main:Plugin"}', encoding="utf-8")
    (code_path / "main.py").write_text(
        "import os\nclass Plugin:\n    def crash(self):\n        os._exit(11)\n", encoding="utf-8"
    )
    worker = PluginWorkerProcess(Path(sys.executable), _worker_script(), code_path)
    worker.start()
    process = worker._process
    assert process is not None
    with pytest.raises(PluginWorkerError, match="通信失败"):
        worker.call("crash")
    assert process.poll() is not None
    assert worker._process is None
