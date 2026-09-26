from __future__ import annotations

import json
from pathlib import Path

import pytest

from ECL.plugins import PluginManager
from ECL.plugins.manager.install_transaction import PluginInstallTransaction


def _plugin(plugin_path: Path, version: str, *, fail_enable: bool = False) -> None:
    plugin_path.mkdir(parents=True, exist_ok=True)
    (plugin_path / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": version, "entry_point": "main:DemoPlugin"}), encoding="utf-8"
    )
    source = "from ECL.plugins import Plugin\nclass DemoPlugin(Plugin):\n"
    source += (
        "    def on_enable(self):\n        raise RuntimeError('new enable failed')\n" if fail_enable else "    pass\n"
    )
    (plugin_path / "main.py").write_text(source, encoding="utf-8")


def _framework(data_path: Path, resource_path: Path) -> PluginManager:
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    return framework


def test_failed_update_restores_old_code_and_enabled_plugin(tmp_path: Path) -> None:
    """
    新版 on_enable 失败时，旧版代码、实例和启用状态都恢复。
    """
    data_path = tmp_path / "data"
    old_path = data_path / "plugins" / "demo"
    _plugin(old_path, "1.0.0")
    framework = _framework(data_path, tmp_path / "resources")
    source_path = tmp_path / "new"
    _plugin(source_path, "2.0.0", fail_enable=True)

    result = framework.install(str(source_path))

    assert not result.success
    assert "new enable failed" in result.message
    assert framework.get_plugin("demo").version == "1.0.0"
    assert framework._status["demo"] == "enabled"
    assert "new enable failed" not in (old_path / "main.py").read_text(encoding="utf-8")
    assert not list((data_path / "plugin_install_transactions").glob("*.json"))


def test_failed_first_install_leaves_no_visible_plugin(tmp_path: Path) -> None:
    """
    首次安装 on_enable 失败时，不留下可发现目录或候选项。
    """
    data_path = tmp_path / "data"
    framework = _framework(data_path, tmp_path / "resources")
    source_path = tmp_path / "new"
    _plugin(source_path, "1.0.0", fail_enable=True)

    assert not framework.install(str(source_path)).success
    assert framework.get_plugin("demo") is None
    assert "demo" not in framework._candidate_map
    assert not (data_path / "plugins" / "demo").exists()


def test_failed_update_preserves_disabled_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    禁用插件的新版本目录切换失败时，旧版本和禁用记录都保留。
    """
    data_path = tmp_path / "data"
    old_path = data_path / "plugins" / "demo"
    _plugin(old_path, "1.0.0")
    framework = _framework(data_path, tmp_path / "resources")
    assert framework.disable("demo").success
    source_path = tmp_path / "new"
    _plugin(source_path, "2.0.0")
    transaction_class = PluginInstallTransaction
    original_begin = transaction_class.begin

    def fail_after_switch(transaction):
        original_begin(transaction)
        raise OSError("模拟切换后失败")

    monkeypatch.setattr(transaction_class, "begin", fail_after_switch)
    assert not framework.install(str(source_path)).success

    assert framework.get_plugin("demo") is None
    assert framework._status["demo"] == "disabled"
    assert framework._candidate_map["demo"]["metadata"]["version"] == "1.0.0"
    assert json.loads((old_path / "plugin.json").read_text(encoding="utf-8"))["version"] == "1.0.0"
    assert "demo" in json.loads((data_path / "plugin_state.json").read_text(encoding="utf-8"))["disabled"]


def test_install_preserves_disabled_state_and_updates_candidate(tmp_path: Path) -> None:
    """
    更新禁用插件不会擅自启用；候选信息同步到新版本供界面与后续启用使用。
    """
    data_path = tmp_path / "data"
    _plugin(data_path / "plugins" / "demo", "1.0.0")
    framework = _framework(data_path, tmp_path / "resources")
    assert framework.disable("demo").success
    source_path = tmp_path / "new"
    _plugin(source_path, "2.0.0")

    assert framework.install(str(source_path)).success
    assert framework.get_plugin("demo") is None
    assert framework._status["demo"] == "disabled"
    assert framework._candidate_map["demo"]["metadata"]["version"] == "2.0.0"
    assert {item["name"]: item for item in framework.list_plugins()}["demo"]["version"] == "2.0.0"
    assert framework.enable("demo").success
    assert framework.get_plugin("demo").version == "2.0.0"


def test_new_install_can_be_uninstalled_without_restart(tmp_path: Path) -> None:
    """
    新安装立即进入候选映射，卸载不依赖重启后重新扫描。
    """
    data_path = tmp_path / "data"
    framework = _framework(data_path, tmp_path / "resources")
    source_path = tmp_path / "source"
    _plugin(source_path, "1.0.0")

    assert framework.install(str(source_path)).success
    assert framework.uninstall("demo").success
    assert not (data_path / "plugins" / "demo").exists()


def test_pending_transaction_restores_old_directory_before_discovery(tmp_path: Path) -> None:
    """
    崩溃留下的 pending 日志必须在插件发现前把旧版切回。
    """
    data_path = tmp_path / "data"
    plugin_dir = data_path / "plugins"
    old_path = plugin_dir / "demo"
    _plugin(old_path, "1.0.0")
    new_path = tmp_path / "new"
    _plugin(new_path, "2.0.0")
    transaction = PluginInstallTransaction(data_path, plugin_dir, "demo")
    transaction.stage(new_path)
    transaction.begin()
    assert json.loads((old_path / "plugin.json").read_text(encoding="utf-8"))["version"] == "2.0.0"

    framework = _framework(data_path, tmp_path / "resources")

    assert framework.get_plugin("demo").version == "1.0.0"
    assert not transaction.journal_path.exists()
    assert not transaction.backup_path.exists()
    assert not transaction.stage_path.exists()


def test_pending_first_install_is_removed_before_discovery(tmp_path: Path) -> None:
    """
    首次安装在目录切换后崩溃时，启动器不加载未经验证的新代码。
    """
    data_path = tmp_path / "data"
    plugin_dir = data_path / "plugins"
    plugin_dir.mkdir(parents=True)
    new_path = tmp_path / "new"
    _plugin(new_path, "1.0.0")
    transaction = PluginInstallTransaction(data_path, plugin_dir, "demo")
    transaction.stage(new_path)
    transaction.begin()
    assert (plugin_dir / "demo").is_dir()

    framework = _framework(data_path, tmp_path / "resources")

    assert framework.get_plugin("demo") is None
    assert not (plugin_dir / "demo").exists()
    assert not transaction.journal_path.exists()


def test_committed_transaction_keeps_new_directory_after_restart(tmp_path: Path, monkeypatch) -> None:
    """
    提交标记已落地但备份清理失败时，下次启动继续使用新版并清理备份。
    """
    data_path = tmp_path / "data"
    plugin_dir = data_path / "plugins"
    _plugin(plugin_dir / "demo", "1.0.0")
    new_path = tmp_path / "new"
    _plugin(new_path, "2.0.0")
    transaction = PluginInstallTransaction(data_path, plugin_dir, "demo")
    transaction.stage(new_path)
    transaction.begin()

    def fail_cleanup() -> None:
        raise OSError("busy")

    monkeypatch.setattr(transaction, "cleanup_committed", fail_cleanup)
    assert transaction.commit() is False
    assert transaction.journal_path.exists()
    assert transaction.backup_path.exists()

    framework = _framework(data_path, tmp_path / "resources")

    assert framework.get_plugin("demo").version == "2.0.0"
    assert not transaction.journal_path.exists()
    assert not transaction.backup_path.exists()


def test_uninstall_after_committed_cleanup_failure_does_not_resurrect_old_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    更新成功但备份待清理时立即卸载，重启不得从残留日志恢复旧插件。
    """
    data_path = tmp_path / "data"
    _plugin(data_path / "plugins" / "demo", "1.0.0")
    framework = _framework(data_path, tmp_path / "resources")
    source_path = tmp_path / "new"
    _plugin(source_path, "2.0.0")
    original_cleanup = PluginInstallTransaction.cleanup_committed

    def fail_cleanup(_transaction: PluginInstallTransaction) -> None:
        raise OSError("busy")

    with monkeypatch.context() as patcher:
        patcher.setattr(PluginInstallTransaction, "cleanup_committed", fail_cleanup)
        assert framework.install(str(source_path)).success
    assert original_cleanup is PluginInstallTransaction.cleanup_committed
    journal_path = data_path / "plugin_install_transactions" / "demo.json"
    assert journal_path.exists()

    assert framework.uninstall("demo").success
    assert not journal_path.exists()
    assert _framework(data_path, tmp_path / "resources").get_plugin("demo") is None


def test_unrecoverable_journal_skips_user_plugin_without_erasing_disabled_state(tmp_path: Path) -> None:
    """
    非法恢复日志不能让可疑插件目录被加载，也不能清掉原禁用记录。
    """
    data_path = tmp_path / "data"
    _plugin(data_path / "plugins" / "demo", "1.0.0")
    journal_path = data_path / "plugin_install_transactions" / "demo.json"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text(
        '{"name":"demo","transaction_id":"../bad","had_old":true,"phase":"pending"}', encoding="utf-8"
    )
    state_path = data_path / "plugin_state.json"
    state_path.write_text('{"disabled":["demo"]}', encoding="utf-8")

    framework = _framework(data_path, tmp_path / "resources")

    assert framework.get_plugin("demo") is None
    assert "demo" in framework._disabled_plugins
    assert journal_path.exists()


def test_invalid_source_manifest_does_not_change_existing_plugin(tmp_path: Path) -> None:
    """
    非对象或损坏的 JSON 在任何卸载及目录移动前被拒绝。
    """
    data_path = tmp_path / "data"
    _plugin(data_path / "plugins" / "demo", "1.0.0")
    framework = _framework(data_path, tmp_path / "resources")
    source_path = tmp_path / "invalid"
    source_path.mkdir()
    (source_path / "plugin.json").write_text("[]", encoding="utf-8")

    assert framework.install(str(source_path)).status == "invalid"
    assert framework.get_plugin("demo").version == "1.0.0"
    assert framework.install(None).status == "invalid"


def test_uninstall_rejects_plugin_name_outside_user_plugin_directory(tmp_path: Path) -> None:
    """
    卸载输入在检查恢复日志路径之前拒绝路径穿越。
    """
    framework = _framework(tmp_path / "data", tmp_path / "resources")
    assert framework.uninstall("../demo").status == "invalid"
