# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_permissions 模块的自动化测试。
#
# 公开接口：
#   - test_permission_from_dict_and_matches() -> None
#   - test_permission_prefix_wildcard_matches() -> None — 前缀通配 resource 如 demo:* 应匹配同一命名空间下的所有资源。
#   - test_permission_manager_register_and_check() -> None
#   - test_system_plugin_skips_permission_check(tmp_path) -> None
#   - test_normal_plugin_requires_declared_permission() -> None
#   - test_normal_plugin_with_permission_can_operate(tmp_path) -> None
#   - test_plugin_can_subscribe_to_another_plugin_event(tmp_path) -> None
#   - test_framework_loads_permissions_from_metadata(tmp_path) -> None
#   - test_missing_permission_marks_plugin_permission_denied(tmp_path) -> None
#   - test_instance_error_is_returned_to_plugin_management(tmp_path) -> None
#   - test_sidebar_state_is_emitted_after_frontend_is_ready() -> None
#   - test_system_plugin_ignores_permission_declaration(tmp_path) -> None
# ============================================================

"""插件权限声明与校验测试。"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ECL.foundation import EventBus
from ECL.plugins import Plugin, PluginManager
from ECL.plugins.permissions import Permission, PermissionAction, PermissionManager, PermissionScope


def test_permission_from_dict_and_matches() -> None:
    p = Permission.from_dict({"scope": "events", "action": "emit", "resource": "demo:hello"})
    assert p is not None
    assert p.scope == PermissionScope.EVENTS
    assert p.action == PermissionAction.EMIT
    assert p.resource == "demo:hello"
    assert p.to_dict() == {"scope": "events", "action": "emit", "resource": "demo:hello"}
    assert p.matches(Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "demo:hello"))
    assert not p.matches(Permission(PermissionScope.EVENTS, PermissionAction.SUBSCRIBE, "demo:hello"))
    assert Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "*").matches(p)


def test_permission_prefix_wildcard_matches() -> None:
    """前缀通配 resource 如 demo:* 应匹配同一命名空间下的所有资源。"""
    wildcard = Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "demo:*")
    assert wildcard.matches(Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "demo:hello"))
    assert wildcard.matches(Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "demo:world:sub"))
    assert not wildcard.matches(Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "other:hello"))
    assert not wildcard.matches(Permission(PermissionScope.EVENTS, PermissionAction.SUBSCRIBE, "demo:hello"))


def test_permission_manager_register_and_check() -> None:
    manager = PermissionManager()
    manager.register_plugin_permissions("demo", [{"scope": "events", "action": "emit", "resource": "demo:hello"}])
    allowed = Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "demo:hello")
    denied = Permission(PermissionScope.EVENTS, PermissionAction.EMIT, "demo:other")

    assert manager.has_permission("demo", allowed)
    assert not manager.has_permission("demo", denied)
    manager.check_permission("demo", allowed)
    with pytest.raises(PermissionError):
        manager.check_permission("demo", denied)


def test_system_plugin_skips_permission_check(tmp_path) -> None:
    manager = PermissionManager()
    manager.register_plugin_permissions("sys", [])
    framework = Mock()
    framework._permission_manager = manager
    event_bus = EventBus()
    framework.events = event_bus
    plugin_dir = tmp_path / "sys"
    plugin_dir.mkdir()
    plugin = Plugin(framework, plugin_dir, {"name": "sys"}, is_system=True)

    # 系统插件不会触发权限校验，即使未声明任何权限也能正常调用
    plugin.emit("sys:hello")
    plugin.subscribe("sys:hello", lambda: None)
    plugin.register_command("cmd", lambda: None)
    plugin.load_file("style.css")


def test_normal_plugin_requires_declared_permission() -> None:
    framework = Mock()
    framework._permission_manager = PermissionManager()
    plugin = Plugin(framework, Mock(), {"name": "demo"})

    with pytest.raises(PermissionError):
        plugin.emit("demo:hello")
    with pytest.raises(PermissionError):
        plugin.subscribe("demo:hello", lambda: None)
    with pytest.raises(PermissionError):
        plugin.register_command("hello", lambda: None)
    with pytest.raises(PermissionError):
        plugin.load_file("style.css")


def test_normal_plugin_with_permission_can_operate(tmp_path) -> None:
    manager = PermissionManager()
    manager.register_plugin_permissions(
        "demo",
        [
            {"scope": "events", "action": "emit", "resource": "demo:hello"},
            {"scope": "events", "action": "subscribe", "resource": "demo:hello"},
            {"scope": "commands", "action": "execute", "resource": "hello"},
            {"scope": "filesystem", "action": "read", "resource": "style.css"},
        ],
    )
    framework = Mock()
    framework._permission_manager = manager
    event_bus = EventBus()
    framework.events = event_bus
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "style.css").write_text("body {}", encoding="utf-8")
    plugin = Plugin(framework, plugin_dir, {"name": "demo"})

    plugin.subscribe("demo:hello", lambda: None)
    plugin.emit("demo:hello")
    plugin.register_command("hello", lambda: None)
    assert plugin.load_file("style.css") == "body {}"


def test_plugin_can_subscribe_to_another_plugin_event(tmp_path) -> None:
    event_bus = EventBus()
    manager = PermissionManager()
    manager.register_plugin_permissions(
        "listener",
        [{"scope": "events", "action": "subscribe", "resource": "publisher:updated"}],
    )
    framework = Mock()
    framework._permission_manager = manager
    # 模拟插件管理器统一注册事件订阅（自动携带插件名作为所有者）
    framework.subscribe_event = lambda plugin, event, handler: event_bus.subscribe(event, handler, owner=plugin.name)
    plugin = Plugin(framework, tmp_path, {"name": "listener"})
    received = []

    plugin.subscribe("publisher:updated", received.append)
    event_bus.emit("publisher:updated", {"value": 1})

    assert received == [{"value": 1}]


def _write_plugin(plugin_dir: Path, metadata: dict, source: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (plugin_dir / "main.py").write_text(source, encoding="utf-8")


def test_framework_loads_permissions_from_metadata(tmp_path) -> None:
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "demo",
        {
            "name": "demo",
            "version": "1.0.0",
            "entry_point": "main:DemoPlugin",
            "permissions": [{"scope": "events", "action": "emit", "resource": "demo:hello"}],
        },
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    def on_load(self):\n"
        "        self.emit('demo:hello')\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework._status.get("demo") == "enabled"
    info = {p["name"]: p for p in framework.list_plugins()}["demo"]
    assert info["permissions"] == [{"scope": "events", "action": "emit", "resource": "demo:hello"}]


def test_missing_permission_marks_plugin_permission_denied(tmp_path) -> None:
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "bad",
        {
            "name": "bad",
            "version": "1.0.0",
            "entry_point": "main:BadPlugin",
            "permissions": [],
        },
        "from ECL.plugins import Plugin\n"
        "class BadPlugin(Plugin):\n"
        "    @Plugin.on_event('bad:hello')\n"
        "    def on_hello(self, name): ...\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework._status.get("bad") == "permission_denied"
    info = {p["name"]: p for p in framework.list_plugins()}.get("bad")
    assert info is not None
    assert info["status"] == "permission_denied"
    assert info["error"] is not None
    assert "events" in info["error"]

    result = framework.enable("bad")
    assert not result.success
    assert result.status == "permission_denied"
    assert result.message == info["error"]


def test_instance_error_is_returned_to_plugin_management(tmp_path) -> None:
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "broken",
        {"name": "broken", "version": "1.0.0", "entry_point": "main:BrokenPlugin"},
        "from ECL.plugins import Plugin\n"
        "class BrokenPlugin(Plugin):\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        raise RuntimeError('插件配置内容无效')\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    info = {plugin["name"]: plugin for plugin in framework.list_plugins()}["broken"]
    assert info["status"] == "error"
    assert info["error"] == "插件配置内容无效"

    result = framework.enable("broken")
    assert not result.success
    assert result.status == "error"
    assert result.message == "插件配置内容无效"


def test_sidebar_state_is_emitted_after_frontend_is_ready() -> None:
    event_bus = EventBus()
    framework = PluginManager(event_bus)
    states = []
    event_bus.subscribe("frontend:sidebar_changed", states.append)

    framework.set_sidebar_state(True)
    assert states == []

    framework.on_frontend_ready()
    framework.set_sidebar_state(False)
    framework.set_sidebar_state(False)

    assert states == [{"collapsed": True}, {"collapsed": False}]


def test_system_plugin_ignores_permission_declaration(tmp_path) -> None:
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    system_plugin_dir = resource_path / "resources" / "system_plugins" / "sysdemo"
    _write_plugin(
        system_plugin_dir,
        {
            "name": "sysdemo",
            "version": "1.0.0",
            "entry_point": "main:SysDemoPlugin",
            "permissions": [],
        },
        "from ECL.plugins import Plugin\n"
        "class SysDemoPlugin(Plugin):\n"
        "    @Plugin.on_event('sysdemo:hello')\n"
        "    def on_hello(self, name): ...\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): ...\n",
    )
    state_path = data_path / "plugin_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"disabled": ["sysdemo"]}), encoding="utf-8")

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework._status.get("sysdemo") == "enabled"
    assert framework.get_plugin("sysdemo") is not None
    assert "sysdemo" not in {plugin["name"] for plugin in framework.list_plugins()}
    disabled = framework.disable("sysdemo")
    assert disabled.success is False
    assert disabled.status == "forbidden"
    assert framework._status.get("sysdemo") == "enabled"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"disabled": []}
