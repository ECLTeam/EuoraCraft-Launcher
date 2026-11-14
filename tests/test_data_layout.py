# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：回归数据目录布局，断言各模块落点固定且不再产生锁文件。
# ============================================================

from __future__ import annotations

from pathlib import Path

from ECL.desktop.single_instance import SingleInstanceService
from ECL.java.models import JavaPolicy
from ECL.plugins.environment_pool import PluginEnvironmentPool
from ECL.plugins.installation_store import PluginInstallationStore
from ECL.services.account.wardrobe import WardrobeStore
from ECL.utils import EventBus
from ECL.utils.config import ConfigStore

declared_top_level = frozenset({"cache", "config", "logs", "plugins", "runtime", "temp", "userdata"})


def test_module_paths_land_in_declared_roots(tmp_path: Path) -> None:
    assert ConfigStore(tmp_path).config_path == tmp_path / "config" / "setting.json"
    assert WardrobeStore(tmp_path).root == tmp_path / "userdata" / "wardrobe"
    pool = PluginEnvironmentPool(tmp_path)
    # Windows 上依赖池会加长路径前缀，按路径尾段比对。
    assert pool.environments_path.parts[-2:] == ("plugins", "deps")
    assert pool.cache_path.parts[-3:] == ("plugins", "cache", "wheels")
    assert JavaPolicy.storage_root(tmp_path) == (tmp_path / "runtime" / "java").resolve()

    service = SingleInstanceService(events=EventBus(), data_path=tmp_path, launcher_version="test")
    assert service.discovery_path == tmp_path / "runtime" / "single_instance.json"


def test_plugin_installation_writes_no_lock_files(tmp_path: Path) -> None:
    store = PluginInstallationStore(tmp_path)
    store.ensure("example", "directory")

    assert (tmp_path / "plugins" / "installations" / "example.json").is_file()
    assert list(tmp_path.rglob("*.lock")) == []


def test_only_declared_top_level_entries_appear(tmp_path: Path) -> None:
    ConfigStore(tmp_path).get_config()
    WardrobeStore(tmp_path)
    PluginInstallationStore(tmp_path).ensure("example", "directory")
    PluginEnvironmentPool(tmp_path)

    created = {item.name for item in tmp_path.iterdir()}
    assert created <= declared_top_level
    assert not any(name.startswith("plugin_") for name in created)
