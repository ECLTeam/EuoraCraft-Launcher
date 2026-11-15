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
from time import monotonic, sleep

import pytest

from ECL.plugins.contracts import ProtocolError
from ECL.plugins.sdk._rpc import RpcRequest
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


def _broker_call(broker, method, params):
    return broker.handle(RpcRequest(method, params, monotonic() + 5))


def _make_emitter(root: Path, marker: Path, topic: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "sys-emitter", "entry_point": "main:EmitterPlugin"}),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pathlib import Path\n"
        "from ECL.plugins.sdk import Plugin\n"
        f"MARKER = Path({str(marker)!r})\n"
        f"TOPIC = {topic!r}\n"
        "class EmitterPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.define_command('publish', self.publish, input_fields=({'name': 'value', 'type': 'string'},), output_fields=({'name': 'delivered', 'type': 'number'},))\n"
        "    def publish(self, value):\n"
        "        return {'delivered': self.emit_event(TOPIC, {'value': value})}\n"
        "    def on_unload(self):\n"
        "        MARKER.write_text('emitter-unloaded')\n",
        encoding="utf-8",
    )


def _make_listener(root: Path, marker: Path, topic: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "sys-listener", "entry_point": "main:ListenerPlugin"}),
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pathlib import Path\n"
        "from ECL.plugins.sdk import Plugin\n"
        f"MARKER = Path({str(marker)!r})\n"
        f"TOPIC = {topic!r}\n"
        "class ListenerPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.subscription = self.subscribe_event(TOPIC, self.on_event)\n"
        "        MARKER.write_text('listening')\n"
        "    def on_event(self, value):\n"
        "        MARKER.write_text('received:' + str(value))\n",
        encoding="utf-8",
    )


def test_event_bus_delivers_across_system_plugins(tmp_path: Path) -> None:
    marker = tmp_path / "events.txt"
    topic = "sys-emitter.ready"
    emitter_root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-emitter"
    listener_root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-listener"
    _make_emitter(emitter_root, marker, topic)
    _make_listener(listener_root, marker, topic)
    service = PluginWorkerService(tmp_path / "data")
    try:
        service.register({"name": "sys-listener", "entry_point": "main:ListenerPlugin"}, listener_root, "system")
        service.register({"name": "sys-emitter", "entry_point": "main:EmitterPlugin"}, emitter_root, "system")
        service.enable("sys-listener")
        service.enable("sys-emitter")
        assert marker.read_text(encoding="utf-8") == "listening"

        # 发布方收到真实投递计数，订阅方回调异步收到载荷
        result = service.broker("sys-emitter").call_command("publish", {"value": "42"})
        assert result["delivered"] == 1
        deadline = monotonic() + 3
        while "received:" not in marker.read_text(encoding="utf-8"):
            assert monotonic() < deadline
            sleep(0.01)
        assert marker.read_text(encoding="utf-8") == "received:42"

        # 主题越界（占用他人命名空间）被拒绝
        broker = service.broker("sys-emitter")
        with pytest.raises(ProtocolError) as exc:
            _broker_call(broker, "events.emit", {"topic": "sys-listener.stolen", "payload": {}})
        assert exc.value.error_code == "PLUGIN_EVENT_TOPIC_INVALID"

        service.stop("sys-emitter")
        service.stop("sys-listener")
    finally:
        service.close()


def test_emit_requires_own_namespace_and_bounded_payload(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-demo"
    _make_system_plugin(root, marker)
    service = PluginWorkerService(tmp_path / "data")
    try:
        service.register({"name": "sys-demo", "entry_point": "main:DemoPlugin"}, root, "system")
        service.enable("sys-demo")
        broker = service.broker("sys-demo")
        with pytest.raises(ProtocolError) as wrong_prefix:
            _broker_call(broker, "events.emit", {"topic": "other.thing", "payload": {}})
        assert wrong_prefix.value.error_code == "PLUGIN_EVENT_TOPIC_INVALID"
        with pytest.raises(ProtocolError) as too_big:
            _broker_call(broker, "events.emit", {"topic": "sys-demo.big", "payload": {"blob": "x" * 9000}})
        assert too_big.value.error_code == "PLUGIN_SCHEMA_INVALID"
        service.stop("sys-demo")
    finally:
        service.close()


def test_system_plugins_call_each_others_exported_commands(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    root = tmp_path / "resources" / "resources" / "system_plugins" / "sys-demo"
    _make_system_plugin(root, marker)
    service = PluginWorkerService(tmp_path / "data")
    try:
        service.register({"name": "sys-demo", "entry_point": "main:DemoPlugin"}, root, "system")
        service.enable("sys-demo")
        broker = service.broker("sys-demo")
        # 进程内通道直达跨插件发现（无其他插件时返回空列表），未启用目标明确失败
        assert _broker_call(broker, "plugins.list", {}) == {"plugins": []}
        with pytest.raises(ProtocolError) as missing:
            _broker_call(broker, "plugins.call", {"plugin": "missing", "command": "echo", "args": {}})
        assert missing.value.error_code in {"PLUGIN_TARGET_INVALID", "PLUGIN_RESOURCE_NOT_FOUND"}
        service.stop("sys-demo")
    finally:
        service.close()


