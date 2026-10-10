# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_lifecycle 模块的自动化测试。
#
# 公开接口：
#   - test_reinstall_over_enabled_plugin_syncs_code(tmp_path) -> None — 对已启用插件重复 install 应覆盖沙箱副本并保持启用，供热重载同步使用。
#   - test_reload_after_reinstall_keeps_plugin_enabled(tmp_path) -> None — 重装后的插件再 reload 应保持启用，覆盖工具箱热重载的完整调用序列。
# ============================================================

"""插件安装、重载和卸载操作的回归测试。"""

import json
from pathlib import Path

from ECL.plugins import PluginManager


def _write_plugin(plugin_dir: Path, metadata: dict, source: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (plugin_dir / "main.py").write_text(source, encoding="utf-8")


def test_reinstall_over_enabled_plugin_syncs_code(tmp_path) -> None:
    """对已启用插件重复 install 应覆盖沙箱副本并保持启用，供热重载同步使用。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    source = tmp_path / "source"
    _write_plugin(
        source,
        {"name": "demo", "version": "1.0.0", "entry_point": "main:DemoPlugin"},
        "from ECL.plugins import Plugin\nclass DemoPlugin(Plugin): pass\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    assert framework.install(str(source)).success is True
    assert framework._status.get("demo") == "enabled"

    # 修改源码后再次安装，应覆盖已加载的旧插件；先前会因状态为 enabled 而失败。
    (source / "main.py").write_text(
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    def on_load(self):\n"
        "        self.logger.info('reinstalled-marker')\n",
        encoding="utf-8",
    )
    result = framework.install(str(source))

    assert result.success is True
    assert result.status == "installed"
    assert framework._status.get("demo") == "enabled"
    assert framework.get_plugin("demo") is not None


def test_reload_after_reinstall_keeps_plugin_enabled(tmp_path) -> None:
    """重装后的插件再 reload 应保持启用，覆盖工具箱热重载的完整调用序列。"""
    data_path = tmp_path / "data"
    resource_path = tmp_path / "resources"
    source = tmp_path / "source"
    _write_plugin(
        source,
        {"name": "demo", "version": "1.0.0", "entry_point": "main:DemoPlugin"},
        "from ECL.plugins import Plugin\nclass DemoPlugin(Plugin): pass\n",
    )

    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    assert framework.install(str(source)).success is True

    (source / "main.py").write_text(
        "from ECL.plugins import Plugin\n"
        "class DemoPlugin(Plugin):\n"
        "    def on_load(self):\n"
        "        self.logger.info('reinstalled-marker')\n",
        encoding="utf-8",
    )
    assert framework.install(str(source)).success is True

    result = framework.reload("demo")

    assert result.success is True
    assert framework._status.get("demo") == "enabled"
