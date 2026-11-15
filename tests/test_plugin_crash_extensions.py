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

from ECL.minecraft.services.game.crash.analyzer import CrashAnalyzer
from ECL.plugins.manager import PluginManager


def _game(tmp_path: Path) -> Path:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "Test"
    version_path.mkdir(parents=True)
    return game_path


def _register_plugin(data_path: Path) -> None:
    plugin_dir = data_path.parent / "resources/resources/system_plugins/crash-demo"
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
        "from ECL.plugins.sdk import Plugin\n"
        "class CrashPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.register_crash_analyzer(self.enrich)\n"
        "    def enrich(self, **context):\n"
        "        return {\n"
        "            'reasons': [{'title': 'demo hint', 'detail': context['version_id']}],\n"
        "            'summary': 'analyzed by demo',\n"
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

    assert result["summary"] == "analyzed by demo"
    assert any(reason.get("title") == "demo hint" for reason in result["reasons"])

    # 禁用插件后应自动移除其崩溃分析回调
    framework.crash_extensions.unregister_owner("crash-demo")
    analyzer = CrashAnalyzer(data_path, extensions=framework.crash_extensions)
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()
    assert "pluginNote" not in result


def test_crash_extension_exception_is_isolated(tmp_path) -> None:
    data_path = tmp_path / "data"
    data_path.mkdir(parents=True)
    plugin_dir = tmp_path / "resources/resources/system_plugins/crash-boom"
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
        "from ECL.plugins.sdk import Plugin\n"
        "class BoomPlugin(Plugin):\n"
        "    def on_enable(self):\n"
        "        self.register_crash_analyzer(self.enrich)\n"
        "    def enrich(self, **context):\n"
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
