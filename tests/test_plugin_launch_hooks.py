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

from ECL.plugins import LaunchContext, PluginManager


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
        "from ECL.plugins import Plugin\n"
        "class LaunchPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_launch_hook('demo', on_prepare=self.prepare, pre_launch=self.before, on_exit=self.after)\n"
        "    def prepare(self, ctx):\n"
        "        ctx.jvm_args.append('-Ddemo.mode=on')\n"
        "        ctx.env['ECL_DEMO'] = '1'\n"
        "        ctx.game_args.append('--demo')\n"
        "    def before(self, ctx):\n"
        "        ctx.env['ECL_PRE'] = 'yes'\n"
        "    def after(self, ctx):\n"
        "        ctx.env['ECL_EXIT'] = 'yes'\n",
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

    framework.launch_hooks.pre_launch(context)
    assert context.env["ECL_PRE"] == "yes"
    framework.launch_hooks.on_exit(context)
    assert context.env["ECL_EXIT"] == "yes"

    framework.launch_hooks.unregister_owner("launch-demo")
    before = context.env.copy()
    framework.launch_hooks.prepare(context)
    framework.launch_hooks.pre_launch(context)
    assert context.env == before

    framework.get_plugin("launch-demo").on_enable()
    context.jvm_args.clear()
    context.env.clear()
    framework.launch_hooks.prepare(context)
    assert context.jvm_args == ["-Ddemo.mode=on"]


def test_plugin_without_launch_permission_cannot_register_hook(tmp_path) -> None:
    """未声明 launch 权限的插件注册启动钩子时 enable 失败。"""
    data_path = tmp_path / "data"
    plugin_dir = data_path / "plugins" / "launch-no-perm"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "launch-no-perm",
                "entry_point": "main:LaunchPlugin",
                "permissions": [],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins import Plugin\n"
        "class LaunchPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_launch_hook('demo', on_prepare=lambda ctx: None)\n",
        encoding="utf-8",
    )

    framework = PluginManager()
    framework.initialize(data_path, tmp_path / "resources")
    assert framework._status.get("launch-no-perm") == "unsupported"


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
        "from ECL.plugins import Plugin\n"
        "class BoomPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_launch_hook('boom', on_prepare=self.prepare)\n"
        "        self.register_launch_hook('ok', on_prepare=self.prepare_ok)\n"
        "    def prepare(self, ctx):\n"
        "        raise RuntimeError('boom')\n"
        "    def prepare_ok(self, ctx):\n"
        "        ctx.jvm_args.append('-Dok=1')\n",
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
