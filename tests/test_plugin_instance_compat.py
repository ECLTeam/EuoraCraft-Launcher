# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_instance_compat 模块的自动化测试。
#
# 公开接口：
#   - test_plugin_can_register_instance_compatibility_provider(tmp_path) -> None — 普通插件可注册实例元数据读取器与监听路径，禁用时宿主会自动清理扩展点。
# ============================================================

import json

from ECL.plugins import InstanceCompatibilityRegistry, PluginManager
from ECL.services.game.instance_compat import InstanceCompatibilityReader


def test_plugin_can_register_instance_compatibility_provider(tmp_path) -> None:
    """
    普通插件可注册实例元数据读取器与监听路径，禁用时宿主会自动清理扩展点。
    """
    data_path = tmp_path / "data"
    plugin_dir = tmp_path / "resources/resources/system_plugins/compat-demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "compat-demo",
                "entry_point": "main:CompatPlugin",
                "permissions": [{"scope": "instances", "action": "read", "resource": "demo"}],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins import ExternalInstanceMetadata, Plugin\n"
        "class CompatPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_instance_compatibility('demo', 'Demo Launcher', self.read, self.watch)\n"
        "    def read(self, context):\n"
        "        return ExternalInstanceMetadata('demo', 1, fields={'description': context.version_id})\n"
        "    def watch(self, options):\n"
        "        return [options['demo']['index']] if options.get('demo') else []\n",
        encoding="utf-8",
    )
    index_path = tmp_path / "demo-index.json"
    index_path.write_text("{}", encoding="utf-8")
    game_path = tmp_path / ".minecraft"
    (game_path / "versions" / "1.21.8").mkdir(parents=True)

    registry = InstanceCompatibilityRegistry()
    framework = PluginManager(instance_compatibility=registry)
    framework.initialize(data_path, tmp_path / "resources")
    reader = InstanceCompatibilityReader(registry)

    metadata = reader.read_instance(
        game_path,
        "1.21.8",
        vanilla_name="1.21.8",
        primary_loader="Vanilla",
        options={"demo": {"index": index_path}},
    )

    assert metadata[0].source == "demo"
    assert metadata[0].fields == {"description": "1.21.8"}
    assert registry.describe_sources() == [{"source": "demo", "title": "Demo Launcher", "plugin": "compat-demo"}]
    assert registry.resolve_watch_paths({"demo": {"index": index_path}}) == [("demo", index_path.resolve())]

    framework.instance_compatibility.unregister_owner("compat-demo")
    assert registry.describe_sources() == []
    framework.get_plugin("compat-demo").on_enable()
    assert registry.describe_sources()[0]["source"] == "demo"
