# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 plugin_crash_extensions 模块的自动化测试。
#
# 公开接口：
#   - test_crash_extension_enriches_analysis_result(tmp_path) -> None
#   - test_crash_extension_exception_is_isolated(tmp_path) -> None
# ============================================================

import json
from pathlib import Path

from ECL.plugins import PluginManager
from ECL.services.game.crash.analyzer import CrashAnalyzer


def _game(tmp_path: Path) -> Path:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "Test"
    version_path.mkdir(parents=True)
    return game_path


def _register_plugin(data_path: Path) -> None:
    plugin_dir = data_path / "plugins" / "crash-demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "crash-demo",
                "entry_point": "main:CrashPlugin",
                "permissions": [{"scope": "crash", "action": "write", "resource": "demo"}],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins import Plugin\n"
        "class CrashPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_crash_analyzer('demo', self.enrich)\n"
        "    def enrich(self, context, result):\n"
        "        return {\n"
        "            'reasons': [{'code': 'plugin.demo_hint', 'confidence': 'possible', 'evidence': [context.version_id], 'parameters': {}}],\n"
        "            'pluginNote': 'analyzed by demo',\n"
        "        }\n",
        encoding="utf-8",
    )


def test_crash_extension_enriches_analysis_result(tmp_path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir(parents=True)
    _register_plugin(data_path)
    game_path = _game(tmp_path)
    source = tmp_path / "latest.log"
    source.write_text("some random non-crash output", encoding="utf-8")

    framework = PluginManager()
    framework.initialize(data_path, tmp_path / "resources")
    analyzer = CrashAnalyzer(data_path, extensions=framework.crash_extensions)
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()

    assert result["pluginNote"] == "analyzed by demo"
    assert any(reason["code"] == "plugin.demo_hint" for reason in result["reasons"])

    # 禁用插件后宿主自动撤销富化回调
    assert framework.disable("crash-demo").success is True
    analyzer = CrashAnalyzer(data_path, extensions=framework.crash_extensions)
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()
    assert "pluginNote" not in result


def test_crash_extension_exception_is_isolated(tmp_path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir(parents=True)
    plugin_dir = data_path / "plugins" / "crash-boom"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "crash-boom",
                "entry_point": "main:BoomPlugin",
                "permissions": [{"scope": "crash", "action": "write", "resource": "boom"}],
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(
        "from ECL.plugins import Plugin\n"
        "class BoomPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        super().on_enable()\n"
        "        self.register_crash_analyzer('boom', self.enrich)\n"
        "    def enrich(self, context, result):\n"
        "        raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    framework = PluginManager()
    framework.initialize(data_path, tmp_path / "resources")
    game_path = _game(tmp_path)
    source = tmp_path / "latest.log"
    source.write_text("some random output", encoding="utf-8")

    analyzer = CrashAnalyzer(data_path, extensions=framework.crash_extensions)
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()
    assert result["reportId"]
