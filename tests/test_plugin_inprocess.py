# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证系统插件进程内运行桥：统一基类插件经 InProcessPeer
#   直连 broker 的端到端行为（注册、启用、命令、设置、停止与重启）。
# ============================================================

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ECL.plugins.worker_service import PluginWorkerService


def _make_system_plugin(root: Path, marker: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "sys-demo", "entry_point": "main:DemoPlugin"}),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pathlib import Path\n"
        "from ECL.plugins.sdk import Plugin\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class DemoPlugin(Plugin):\n"
        "    def on_load(self):\n"
        "        self.host.call('settings.define', {'definitions': [{'key': 'greeting', 'type': 'string', 'default': 'hello'}]})\n"
        "        self.define_command('echo', self.echo, input_fields=({'name': 'value', 'type': 'string'},),"
        " output_fields=({'name': 'value', 'type': 'string'},))\n"
        "        MARKER.write_text('loaded')\n"
        "    def echo(self, value):\n"
        "        return {'value': value}\n"
        "    def on_unload(self):\n"
        "        MARKER.write_text('unloaded')\n",
        encoding="utf-8",
    )


def test_system_plugin_runs_in_process_end_to_end(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-demo"
    _make_system_plugin(root, marker)
    service = PluginWorkerService(tmp_path / "data")
    try:
        service.register({"name": "sys-demo", "entry_point": "main:DemoPlugin"}, root, "system")
        assert marker.exists() is False

        service.enable("sys-demo")
        assert marker.read_text(encoding="utf-8") == "loaded"

        # 插件 on_load 期间经进程内对端登记的设置与命令真实生效
        view = service.settings_view("sys-demo")
        assert any(definition["key"] == "greeting" for definition in view["schema"])

        broker = service.broker("sys-demo")
        assert broker.call_command("echo", {"value": "hi"}) == {"value": "hi"}

        info = service.runtime_info("sys-demo")
        assert info["run_state"] == "enabled"
        assert info["pid"] == os.getpid()

        service.stop("sys-demo")
        assert marker.read_text(encoding="utf-8") == "unloaded"
        assert service.runtime_info("sys-demo")["run_state"] == "disabled"
    finally:
        service.close()


def test_system_plugin_restart_creates_fresh_session(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-demo"
    _make_system_plugin(root, marker)
    service = PluginWorkerService(tmp_path / "data")
    try:
        service.register({"name": "sys-demo", "entry_point": "main:DemoPlugin"}, root, "system")
        service.enable("sys-demo")
        service.stop("sys-demo")
        service.enable("sys-demo")
        assert marker.read_text(encoding="utf-8") == "loaded"
        assert service.broker("sys-demo").call_command("echo", {"value": "again"}) == {"value": "again"}
        service.stop("sys-demo")
    finally:
        service.close()


def test_system_plugin_content_change_blocks_running_instance(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-demo"
    _make_system_plugin(root, marker)
    service = PluginWorkerService(tmp_path / "data")
    try:
        service.register({"name": "sys-demo", "entry_point": "main:DemoPlugin"}, root, "system")
        service.enable("sys-demo")
        # 系统插件与本进程共享解释器，但内容身份复核仍拦截磁盘字节变化
        (root / "main.py").write_text("# changed\n", encoding="utf-8")
        with pytest.raises(Exception) as exc:
            service.broker("sys-demo").call_command("echo", {"value": "after-change"})
        assert "PLUGIN_" in str(getattr(exc.value, "error_code", ""))
        service.stop("sys-demo")
    finally:
        service.close()
