# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_disabled_state 模块的自动化测试。
#
# 公开接口：
#   - test_disabled_plugin_is_skipped_on_initialize(tmp_path) -> None — 被禁用的插件不应被实例化，状态应为 disabled。
#   - test_disabled_plugin_shows_metadata_from_plugin_json(tmp_path) -> None — 被禁用且未实例化的插件，list_plugins 仍应从 plugin.json 读取元数据。
#   - test_deleted_disabled_plugin_is_pruned_from_state_and_list(tmp_path) -> None — 插件目录已删除时，不应因为历史禁用记录继续显示幽灵条目。
#   - test_uninstall_disabled_plugin_removes_files_state_and_list(tmp_path) -> None — 从界面卸载已禁用插件时，应同时删除目录与禁用记录。
#   - test_disable_persists_to_state_file(tmp_path) -> None — 从前端禁用插件后，状态应写入 plugin_state.json。
#   - test_enable_removes_from_state_file(tmp_path) -> None — 启用已禁用插件后，应从 plugin_state.json 中移除。
#   - test_frontend_ready_is_idempotent_and_reenabled_plugins_get_hook(tmp_path) -> None — on_frontend_ready 重复调用只生效一次；前端就绪后重新启用的插件单独补调。
#   - test_register_route_is_idempotent(tmp_path) -> None — 同一插件重复注册相同路径时，路由列表中只保留一条。
#   - test_close_does_not_mark_all_plugins_disabled(tmp_path) -> None — 框架关闭时不应把已加载插件全部写入 plugin_state.json。
#   - test_auto_enable_false_keeps_plugins_discovered_but_unloaded(tmp_path) -> None — auto_enable=False 时插件仅被发现，不加载也不启用，随后仍可按需启用。
# ============================================================

"""插件禁用状态持久化测试。"""

import json
from pathlib import Path

from ECL.plugins import PluginManager


def _write_plugin(plugin_dir: Path, metadata: dict, source: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (plugin_dir / "main.py").write_text(source, encoding="utf-8")


def test_disabled_plugin_is_skipped_on_initialize(tmp_path) -> None:
    """被禁用的插件不应被实例化，状态应为 disabled。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "disabled_plugin",
        {
            "name": "disabled_plugin",
            "version": "1.0.0",
            "entry_point": "main:DisabledPlugin",
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "disabled_plugin:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class DisabledPlugin(Plugin):\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): return {'ok': True}\n",
    )
    # 预置禁用状态
    state_path = data_path / "plugin_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"disabled": ["disabled_plugin"]}), encoding="utf-8")

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework._status.get("disabled_plugin") == "disabled"
    assert framework.get_plugin("disabled_plugin") is None
    info = {p["name"]: p for p in framework.list_plugins()}
    disabled_info = info["disabled_plugin"]
    assert disabled_info["status"] == "disabled"
    assert disabled_info["version"] == "1.0.0"
    assert disabled_info["title"] == "disabled_plugin"
    assert disabled_info["author"] == ""
    assert disabled_info["dependencies"] == {}
    assert len(disabled_info["permissions"]) == 2


def test_disabled_plugin_shows_metadata_from_plugin_json(tmp_path) -> None:
    """被禁用且未实例化的插件，list_plugins 仍应从 plugin.json 读取元数据。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "meta_plugin",
        {
            "name": "meta_plugin",
            "title": "元数据展示插件",
            "version": "2.3.4",
            "description": "测试禁用后仍可读取元数据",
            "author": "ECLTest",
            "entry_point": "main:MetaPlugin",
            "dependencies": {"dep": ">=1.0.0"},
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "meta_plugin:*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class MetaPlugin(Plugin):\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): return {'ok': True}\n",
    )
    state_path = data_path / "plugin_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"disabled": ["meta_plugin"]}), encoding="utf-8")

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework.get_plugin("meta_plugin") is None
    info = {p["name"]: p for p in framework.list_plugins()}["meta_plugin"]
    assert info["status"] == "disabled"
    assert info["title"] == "元数据展示插件"
    assert info["version"] == "2.3.4"
    assert info["description"] == "测试禁用后仍可读取元数据"
    assert info["author"] == "ECLTest"
    assert info["dependencies"] == {"dep": ">=1.0.0"}
    assert len(info["permissions"]) == 1
    assert info["is_system"] is False


def test_deleted_disabled_plugin_is_pruned_from_state_and_list(tmp_path) -> None:
    """插件目录已删除时，不应因为历史禁用记录继续显示幽灵条目。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    state_path = data_path / "plugin_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"disabled": ["deleted_plugin"]}), encoding="utf-8")

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert "deleted_plugin" not in {plugin["name"] for plugin in framework.list_plugins()}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state == {"disabled": []}


def test_uninstall_disabled_plugin_removes_files_state_and_list(tmp_path) -> None:
    """从界面卸载已禁用插件时，应同时删除目录与禁用记录。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    plugin_dir = data_path / "plugins" / "demo"
    _write_plugin(
        plugin_dir,
        {"name": "demo", "version": "1.0.0", "entry_point": "main:DemoPlugin"},
        "from ECL.plugins import Plugin\nclass DemoPlugin(Plugin): pass\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    assert framework.disable("demo").success is True

    result = framework.uninstall("demo")

    assert result.success is True
    assert result.status == "uninstalled"
    assert not plugin_dir.exists()
    assert "demo" not in {plugin["name"] for plugin in framework.list_plugins()}
    state = json.loads((data_path / "plugin_state.json").read_text(encoding="utf-8"))
    assert state == {"disabled": []}


def test_disable_persists_to_state_file(tmp_path) -> None:
    """从前端禁用插件后，状态应写入 plugin_state.json。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "demo",
        {
            "name": "demo",
            "version": "1.0.0",
            "entry_point": "main:DemoPlugin",
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "demo:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): return {'ok': True}\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework._status.get("demo") == "enabled"
    assert framework.disable("demo").success is True
    state_path = data_path / "plugin_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "demo" in state.get("disabled", [])


def test_enable_removes_from_state_file(tmp_path) -> None:
    """启用已禁用插件后，应从 plugin_state.json 中移除。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "demo",
        {
            "name": "demo",
            "version": "1.0.0",
            "entry_point": "main:DemoPlugin",
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "demo:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): return {'ok': True}\n",
    )
    state_path = data_path / "plugin_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"disabled": ["demo"]}), encoding="utf-8")

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    assert framework._status.get("demo") == "disabled"
    success, _ = framework._enable("demo")
    assert success is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "demo" not in state.get("disabled", [])


def test_frontend_ready_is_idempotent_and_reenabled_plugins_get_hook(tmp_path) -> None:
    """on_frontend_ready 重复调用只生效一次；前端就绪后重新启用的插件单独补调。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "demo",
        {
            "name": "demo",
            "version": "1.0.0",
            "entry_point": "main:DemoPlugin",
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "demo:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        super().__init__(*args, **kwargs)\n"
        "        self.ready_count = 0\n"
        "    def on_frontend_ready(self):\n"
        "        self.ready_count += 1\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    plugin = framework.get_plugin("demo")
    assert plugin is not None
    assert plugin.ready_count == 0

    framework.on_frontend_ready()
    assert plugin.ready_count == 1

    framework.on_frontend_ready()
    assert plugin.ready_count == 1  # 重复调用不生效

    assert framework.disable("demo").success is True
    assert plugin.ready_count == 1

    success, _ = framework._enable("demo")
    assert success is True
    assert plugin.ready_count == 2  # 前端就绪后重新启用补调


def test_register_route_is_idempotent(tmp_path) -> None:
    """同一插件重复注册相同路径时，路由列表中只保留一条。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "demo",
        {
            "name": "demo",
            "version": "1.0.0",
            "entry_point": "main:DemoPlugin",
            "permissions": [
                {"scope": "ui", "action": "write", "resource": "*"},
                {"scope": "events", "action": "emit", "resource": "demo:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_route('/page', 'Page', 'plugin')\n"
        "        self.register_route('/page', 'Page', 'plugin')\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)

    routes = [r for r in framework.get_routes() if r["plugin"] == "demo"]
    assert len(routes) == 1
    assert routes[0]["path"] == "/page"


def test_close_does_not_mark_all_plugins_disabled(tmp_path) -> None:
    """框架关闭时不应把已加载插件全部写入 plugin_state.json。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "demo",
        {
            "name": "demo",
            "version": "1.0.0",
            "entry_point": "main:DemoPlugin",
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "demo:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): return {'ok': True}\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    assert framework._status.get("demo") == "enabled"

    state_path = data_path / "plugin_state.json"
    # 确保已有状态文件，模拟用户之前禁用过某个插件的场景
    state_path.write_text(json.dumps({"disabled": ["other_plugin"]}), encoding="utf-8")

    framework.close()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "demo" not in state.get("disabled", [])
    assert "other_plugin" in state.get("disabled", [])


def test_auto_enable_false_keeps_plugins_discovered_but_unloaded(tmp_path) -> None:
    """auto_enable=False 时插件仅被发现，不加载也不启用，随后仍可按需启用。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    _write_plugin(
        data_path / "plugins" / "lazy_plugin",
        {
            "name": "lazy_plugin",
            "version": "1.0.0",
            "entry_point": "main:LazyPlugin",
            "permissions": [
                {"scope": "events", "action": "emit", "resource": "lazy_plugin:*"},
                {"scope": "commands", "action": "execute", "resource": "*"},
            ],
        },
        "from ECL.plugins import Plugin\n"
        "class LazyPlugin(Plugin):\n"
        "    @Plugin.on_command('hello')\n"
        "    def cmd_hello(self): return {'ok': True}\n",
    )

    safe_framework = PluginManager()
    safe_framework.initialize(data_path, resource_path, auto_enable=False)

    assert safe_framework.get_plugin("lazy_plugin") is None
    assert "lazy_plugin" in safe_framework._candidate_map
    info = {p["name"]: p for p in safe_framework.list_plugins()}
    assert info["lazy_plugin"]["status"] == "unloaded"

    # 安全模式下按需启用应加载并启用该插件
    result = safe_framework.enable("lazy_plugin")
    assert result.status == "enabled"
    assert safe_framework.get_plugin("lazy_plugin") is not None
    assert safe_framework._status.get("lazy_plugin") == "enabled"

    # 对照组：默认行为仍会加载并启用同一插件，证明差异来自 auto_enable 参数。
    default_framework = PluginManager()
    default_framework.initialize(data_path, resource_path)
    assert default_framework.get_plugin("lazy_plugin") is not None
    assert default_framework._status.get("lazy_plugin") == "enabled"
