from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from threading import get_ident

import pytest

from ECL.api.plugins import PluginHandlers
from ECL.plugins import PluginManager
from ECL.plugins.manager.contracts import PluginAction, PluginActionResult
from ECL.plugins.package_activation import PluginPackageActivationStore
from ECL.plugins.package_archive import build_plugin_package, extract_plugin_package
from ECL.plugins.package_preparation import PluginPackagePreflight, PluginPreparedPackage, current_plugin_target
from ECL.plugins.runtime_assets import PluginRuntimeAsset


def _prepared_package(tmp_path: Path, data_path: Path) -> PluginPreparedPackage:
    source_path = tmp_path / "source"
    source_path.mkdir()
    (source_path / "plugin.json").write_text(
        json.dumps({"name": "demo", "title": "归档示例", "version": "1.0.0", "entry_point": "main:Plugin"}),
        encoding="utf-8",
    )
    (source_path / "main.py").write_text(
        "import os\n"
        "class Plugin:\n"
        "    def on_enable(self):\n"
        "        self.enabled = True\n"
        "    def pid(self):\n"
        "        return os.getpid()\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "demo.eclplugin"
    package = build_plugin_package(source_path, archive_path)
    code_path = data_path / "plugin_packages" / "demo" / f"pkg-{package.manifest_sha256[:20]}"
    assert extract_plugin_package(archive_path, code_path) == package
    preflight = PluginPackagePreflight(package, current_plugin_target(), (), 0, False)
    return PluginPreparedPackage(preflight, code_path, Path(sys.executable), "a" * 64)


def _resource_path(tmp_path: Path, asset: PluginRuntimeAsset) -> Path:
    resource_path = tmp_path / "resources-root"
    manifest_path = resource_path / "resources" / "plugin_runtime_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(asdict(asset)), encoding="utf-8")
    return resource_path


def _ready_worker_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_path = Path(__file__).resolve().parent.parent / "packaging" / "plugin_worker.py"
    monkeypatch.setattr(
        PluginPackageActivationStore,
        "_ready_worker_paths",
        lambda _store, _environment_key: (Path(sys.executable), worker_path),
    )


def test_manager_restores_and_operates_packaged_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    管理器恢复归档插件后，列表和基础生命周期都只操作 Worker。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    prepared = _prepared_package(tmp_path, data_path)
    _ready_worker_paths(monkeypatch)
    preparatory_store = PluginPackageActivationStore(data_path, asset)
    preparatory_store.activate(prepared)
    preparatory_store.close()

    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        listed = framework.list_plugins()
        assert len(listed) == 1
        assert listed[0]["name"] == "demo" and listed[0]["status"] == "enabled"
        assert framework.get_plugin("demo") is None
        assert framework.get_plugin_metadata("demo")["title"] == "归档示例"
        assert "ecl_worker_plugin_main" not in sys.modules

        store = framework._package_store
        assert store is not None
        active = store.restore("demo")
        assert active is not None and active.worker is not None
        first_pid = active.worker.call("pid")
        assert first_pid != os.getpid()

        assert framework.disable("demo").success
        assert framework.list_plugins()[0]["status"] == "disabled"
        assert framework.enable("demo").success
        assert framework.list_plugins()[0]["status"] == "enabled"
        assert framework.reload("demo").success
        assert framework.uninstall("demo").success
        assert framework.list_plugins() == []
        assert not (data_path / "plugin_packages" / "demo" / "active.json").exists()
        assert prepared.code_path.is_dir()
    finally:
        framework.close()


def test_manager_never_falls_back_to_same_name_host_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    归档与目录重名时都不启动，尤其不能在宿主执行目录插件。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    prepared = _prepared_package(tmp_path, data_path)
    _ready_worker_paths(monkeypatch)
    store = PluginPackageActivationStore(data_path, asset)
    store.activate(prepared)
    store.close()

    legacy_path = data_path / "plugins" / "demo"
    legacy_path.mkdir(parents=True)
    (legacy_path / "plugin.json").write_text('{"name":"demo"}', encoding="utf-8")
    marker_path = tmp_path / "host-imported.txt"
    (legacy_path / "main.py").write_text(
        f"from pathlib import Path\nPath({str(marker_path)!r}).write_text('imported')\n"
        "from ECL.plugins import Plugin as Base\nclass Plugin(Base):\n    pass\n",
        encoding="utf-8",
    )
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        entries = framework.list_plugins()
        assert len(entries) == 1
        assert entries[0]["status"] == "error" and "同名目录插件" in entries[0]["error"]
        assert framework.get_plugin("demo") is None
        assert not marker_path.exists()
        assert not framework.uninstall("demo").success
    finally:
        framework.close()


def test_bad_package_pointer_is_visible(tmp_path: Path) -> None:
    """
    活动指针损坏时显示恢复错误，不将其当作可用插件。
    """
    data_path = tmp_path / "data"
    pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text("{broken", encoding="utf-8")
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        entries = framework.list_plugins()
        assert len(entries) == 1 and entries[0]["status"] == "error"
        assert "活动指针" in entries[0]["error"]
    finally:
        framework.close()


def test_package_ipc_runs_worker_action_outside_event_loop() -> None:
    """
    归档生命周期操作在线程中执行，不阻塞 IPC 事件循环。
    """
    called_threads: list[int] = []

    class PackageActions:
        def is_package_plugin(self, name: str) -> bool:
            return name == "demo"

        def enable(self, name: str) -> PluginActionResult:
            called_threads.append(get_ident())
            return PluginActionResult(name, PluginAction.ENABLE, "enabled")

    handler = PluginHandlers.__new__(PluginHandlers)
    handler.plugins = PackageActions()
    caller_thread = get_ident()
    assert asyncio.run(handler.plugin_enable({"plugin_name": "demo"})) == {"success": True}
    assert len(called_threads) == 1 and called_threads[0] != caller_thread
