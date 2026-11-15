# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对插件实例兼容监听路径在宿主关闭时序下的自动化测试。
#
# 公开接口：
#   - test_worker_selected_files_returns_empty_when_host_closed() -> None
#   - test_worker_selected_files_reraises_other_protocol_errors() -> None
#   - test_worker_selected_files_without_worker_service() -> None
# ============================================================

from pathlib import Path
from types import SimpleNamespace

import pytest

from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.manager import PluginManager


def _manager_with_worker_service(settings_view) -> PluginManager:
    manager = object.__new__(PluginManager)
    manager.worker_service = SimpleNamespace(settings_view=settings_view)
    return manager


def test_worker_selected_files_returns_empty_when_host_closed() -> None:
    def settings_view(_name: str) -> dict:
        raise ProtocolError("插件宿主已经关闭", "PLUGIN_HOST_CLOSED")

    manager = _manager_with_worker_service(settings_view)

    assert manager._worker_selected_files("qomicex_compatible") == []


def test_worker_selected_files_reraises_other_protocol_errors() -> None:
    def settings_view(_name: str) -> dict:
        raise ProtocolError("插件安装目录不可用", "PLUGIN_RESOURCE_NOT_FOUND")

    manager = _manager_with_worker_service(settings_view)

    with pytest.raises(ProtocolError):
        manager._worker_selected_files("qomicex_compatible")


def test_worker_selected_files_without_worker_service() -> None:
    manager = object.__new__(PluginManager)
    manager.worker_service = None

    assert manager._worker_selected_files("qomicex_compatible") == []


def test_worker_selected_files_returns_selected_paths(tmp_path: Path) -> None:
    index_file = tmp_path / "selected.json"
    index_file.write_text("{}", encoding="utf-8")
    settings = {
        "readonly_files": {"external_index": str(index_file)},
        "schema": [{"key": "external_index", "type": "readonly_file"}],
    }
    manager = _manager_with_worker_service(lambda _name: settings)

    assert manager._worker_selected_files("qomicex_compatible") == [index_file]
