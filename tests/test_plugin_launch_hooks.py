# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_launch_hooks 模块的自动化测试。
#
# 公开接口：
#   - test_plugin_can_register_launch_hook_and_mutate_launch_context(tmp_path) -> None — 普通插件可注册启动钩子并修改 JVM 参数与环境变量，禁用时宿主自动清理。
#   - test_plugin_without_launch_permission_cannot_register_hook(tmp_path) -> None — 未声明 launch 权限的插件注册启动钩子时 enable 失败。
#   - test_launch_hook_exception_is_isolated(tmp_path) -> None — 单个钩子抛异常不阻断其余钩子。
# ============================================================

import json

from ECL.plugins.launch_hooks import LaunchContext
from ECL.plugins.manager import PluginManager


def test_plugin_can_register_launch_hook_and_mutate_launch_context(tmp_path) -> None:
    """
    普通插件可注册启动钩子并修改 JVM 参数与环境变量，禁用时宿主自动清理。
    """
    data_path = tmp_path / "data"
    plugin_dir = tmp_path / "resources/resources/system_plugins/launch-demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "launch-demo",
                "entry_point": "main:LaunchPlugin",
                "permissions": [{"scope": "launch", "action": "write", "resource": "demo"}],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins.sdk import Plugin\n"
        "class LaunchPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.register_launch_hook('prepare', self.prepare)\n"
        "        self.register_launch_hook('pre_launch', self.before)\n"
        "        self.register_launch_hook('exit', self.after)\n"
        "    def prepare(self, **context):\n"
        "        return {'jvm_args': ['-Ddemo.mode=on'], 'game_args': ['--demo'], 'env': {'ECL_DEMO': '1'}}\n"
        "    def before(self, **context):\n"
        "        return None\n"
        "    def after(self, **context):\n"
        "        return None\n",
        encoding="utf-8",
    )

    framework = PluginManager()
    framework.initialize(data_path, tmp_path / "resources")

    context = LaunchContext(
        version_id="1.21.8",
        loader="Vanilla",
        game_path=tmp_path / ".minecraft",
        game_directory=tmp_path / ".minecraft" / "versions" / "1.21.8",
        version_isolation=False,
    )
    framework.launch_hooks.prepare(context)
    assert context.jvm_args == ["-Ddemo.mode=on"]
    assert context.game_args == ["--demo"]
    assert context.env == {"ECL_DEMO": "1"}

    # 统一钩子协议：只有 prepare 阶段允许返回修改，其余阶段返回值被忽略
    framework.launch_hooks.pre_launch(context)
    framework.launch_hooks.on_exit(context)
    assert context.env == {"ECL_DEMO": "1"}

    framework.launch_hooks.unregister_owner("launch-demo")
    context.jvm_args.clear()
    context.game_args.clear()
    before = context.env.copy()
    framework.launch_hooks.prepare(context)
    assert context.jvm_args == []
    assert context.env == before


def test_third_party_hook_can_register_from_user_directory(tmp_path) -> None:
    """启用即信任（ADR-005）：用户目录插件使用统一基类同样可登记启动钩子。"""
    data_path = tmp_path / "data"
    plugin_dir = data_path / "plugins" / "sources" / "launch-user"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "launch-user",
                "entry_point": "main:LaunchPlugin",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins.sdk import Plugin\n"
        "class LaunchPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.register_launch_hook('prepare', self.prepare)\n"
        "    def prepare(self, **context):\n"
        "        return {'jvm_args': ['-Duser.hook=1']}\n",
        encoding="utf-8",
    )

    framework = PluginManager()
    framework.initialize(data_path, tmp_path / "resources")
    context = LaunchContext(
        version_id="1.21.8",
        loader="Vanilla",
        game_path=tmp_path / ".minecraft",
        game_directory=tmp_path / ".minecraft" / "versions" / "1.21.8",
        version_isolation=False,
    )
    framework.launch_hooks.prepare(context)
    assert context.jvm_args == ["-Duser.hook=1"]


def test_launch_hook_exception_is_isolated(tmp_path) -> None:
    """单个钩子抛异常不阻断其余钩子。"""
    data_path = tmp_path / "data"
    plugin_dir = tmp_path / "resources/resources/system_plugins/launch-boom"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "launch-boom",
                "entry_point": "main:BoomPlugin",
                "permissions": [{"scope": "launch", "action": "write", "resource": "*"}],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins.sdk import Plugin\n"
        "class BoomPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.register_launch_hook('prepare', self.prepare)\n"
        "        self.register_launch_hook('prepare', self.prepare_ok)\n"
        "    def prepare(self, **context):\n"
        "        raise RuntimeError('boom')\n"
        "    def prepare_ok(self, **context):\n"
        "        return {'jvm_args': ['-Dok=1']}\n",
        encoding="utf-8",
    )

    framework = PluginManager()
    framework.initialize(data_path, tmp_path / "resources")
    context = LaunchContext(
        version_id="1.21.8",
        loader="Vanilla",
        game_path=tmp_path / ".minecraft",
        game_directory=tmp_path / ".minecraft" / "versions" / "1.21.8",
        version_isolation=False,
    )
    framework.launch_hooks.prepare(context)
    assert context.jvm_args == ["-Dok=1"]
