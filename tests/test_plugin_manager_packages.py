from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from threading import get_ident

import pytest

from ECL.api.plugins import PluginHandlers
from ECL.plugins import PluginManager
from ECL.plugins.manager.contracts import PluginAction, PluginActionResult, PluginCommandError
from ECL.plugins.package_activation import PluginPackageActivationStore
from ECL.plugins.package_archive import build_plugin_package, extract_plugin_package
from ECL.plugins.package_preparation import (
    PluginPackagePreflight,
    PluginPackagePreparer,
    PluginPreparedPackage,
    current_plugin_target,
)
from ECL.plugins.runtime_assets import PluginRuntimeAsset


def _prepared_package(
    tmp_path: Path,
    data_path: Path,
    version: str = "1.0.0",
    *,
    fails_enable: bool = False,
    with_commands: bool = False,
) -> PluginPreparedPackage:
    source_path = tmp_path / f"source-{version}"
    source_path.mkdir()
    (source_path / "plugin.json").write_text(
        json.dumps({"name": "demo", "title": "归档示例", "version": version, "entry_point": "main:Plugin"}),
        encoding="utf-8",
    )
    plugin_source = (
        "import os\n"
        + ("from ecl_plugin_sdk import Plugin as BasePlugin\n" if with_commands else "")
        + ("class Plugin(BasePlugin):\n" if with_commands else "class Plugin:\n")
        + "    def on_enable(self):\n"
        + ("        raise RuntimeError('enable failed')\n" if fails_enable else "        self.enabled = True\n")
        + "    def pid(self):\n"
        + "        return os.getpid()\n"
        + (
            "    @BasePlugin.on_command('echo')\n"
            "    def echo(self, value):\n"
            "        return {'value': value, 'pid': os.getpid()}\n"
            "    @BasePlugin.on_command('crash')\n"
            "    def crash(self):\n"
            "        os._exit(11)\n"
            if with_commands
            else ""
        )
    )
    (source_path / "main.py").write_text(plugin_source, encoding="utf-8")
    archive_path = tmp_path / f"demo-{version}.eclplugin"
    package = build_plugin_package(source_path, archive_path)
    code_path = data_path / "plugin_packages" / "demo" / f"pkg-{package.manifest_sha256[:20]}"
    assert extract_plugin_package(archive_path, code_path) == package
    preflight = PluginPackagePreflight(package, current_plugin_target(), (), 0, False)
    return PluginPreparedPackage(preflight, code_path, Path(sys.executable), "a" * 64)


def test_package_install_commits_worker_after_explicit_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    正式安装入口只在明确确认后提交归档活动指针，插件留在 Worker。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    prepared = _prepared_package(tmp_path, data_path)
    archive_path = tmp_path / "demo-1.0.0.eclplugin"
    _ready_worker_paths(monkeypatch)
    monkeypatch.setattr(PluginPackagePreparer, "prepare", lambda *_args, **_kwargs: prepared)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        assert not framework.install(str(archive_path)).success
        assert not (prepared.code_path.parent / "active.json").exists()
        preflight = framework.inspect_package(str(archive_path))
        assert preflight.unverified_source and preflight.package.name == "demo" and not preflight.runtime_ready
        result = framework.install(str(archive_path), confirm_unverified_source=True)
        assert result.success
        assert framework.list_plugins()[0]["status"] == "enabled"
        active = framework._package_store.restore("demo")
        assert active is not None and active.worker is not None
        assert active.worker.call("pid") != os.getpid()
        assert "ecl_worker_plugin_main" not in sys.modules
    finally:
        framework.close()


def test_package_sdk_commands_follow_active_worker_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    归档命令只路由到已启用 Worker，禁用、重载和卸载后没有宿主回退。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    prepared = _prepared_package(tmp_path, data_path, with_commands=True)
    _ready_worker_paths(monkeypatch)
    monkeypatch.setattr(PluginPackagePreparer, "prepare", lambda *_args, **_kwargs: prepared)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        assert framework.install(str(tmp_path / "demo-1.0.0.eclplugin"), confirm_unverified_source=True).success
        assert framework.list_plugins()[0]["services"] == ["crash", "echo"]
        handler = PluginHandlers.__new__(PluginHandlers)
        handler.plugins = framework
        response = asyncio.run(handler.plugin_call_command({"command": "demo:echo", "params": {"value": "hello"}}))
        assert response["success"] and response["data"]["value"] == "hello"
        assert response["data"]["pid"] != os.getpid()
        with pytest.raises(PluginCommandError, match="未注册"):
            framework.call_command("demo:pid")
        assert framework.disable("demo").success
        with pytest.raises(PluginCommandError, match="未启用"):
            framework.call_command("demo:echo", {"value": "disabled"})
        assert framework.enable("demo").success
        assert framework.reload("demo").success
        assert framework.call_command("demo:echo", {"value": "again"})["value"] == "again"
        with pytest.raises(PluginCommandError, match="通信失败"):
            framework.call_command("demo:crash")
        failed_entry = framework.list_plugins()[0]
        assert failed_entry["status"] == "error" and failed_entry["title"] == "归档示例"
        assert failed_entry["services"] == []
        assert framework.enable("demo").success
        assert framework.call_command("demo:echo", {"value": "recovered"})["value"] == "recovered"
        assert framework.uninstall("demo").success
        with pytest.raises(PluginCommandError, match="未启用"):
            framework.call_command("demo:echo")
        assert "ecl_worker_plugin_main" not in sys.modules
    finally:
        framework.close()


def test_failed_package_update_keeps_committed_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    新版本的启用钩子失败后，旧版活动指针和 Worker 仍可调用。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    first = _prepared_package(tmp_path, data_path)
    second = _prepared_package(tmp_path, data_path, "2.0.0", fails_enable=True)
    _ready_worker_paths(monkeypatch)
    monkeypatch.setattr(
        PluginPackagePreparer, "prepare", lambda _self, path, **_kwargs: first if "1.0.0" in str(path) else second
    )
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        assert framework.install(str(tmp_path / "demo-1.0.0.eclplugin"), confirm_unverified_source=True).success
        store = framework._package_store
        assert store is not None
        active = store.restore("demo")
        assert active is not None and active.worker is not None
        pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
        previous_pointer = pointer_path.read_bytes()

        result = framework.install(str(tmp_path / "demo-2.0.0.eclplugin"), confirm_unverified_source=True)
        assert not result.success and "enable failed" in result.message
        assert pointer_path.read_bytes() == previous_pointer
        assert active.worker.call("pid") != os.getpid()
        assert framework.list_plugins()[0]["version"] == "1.0.0"
    finally:
        framework.close()


def test_failed_first_install_removes_uncommitted_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    首次安装的 Worker 启用失败后，不留下活动指针或未提交代码。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    prepared = _prepared_package(tmp_path, data_path, fails_enable=True)
    archive_path = tmp_path / "demo-1.0.0.eclplugin"
    shutil.rmtree(prepared.code_path)
    _ready_worker_paths(monkeypatch)

    def prepare(_self: PluginPackagePreparer, _path: Path, **_kwargs: object) -> PluginPreparedPackage:
        extract_plugin_package(archive_path, prepared.code_path)
        return prepared

    monkeypatch.setattr(PluginPackagePreparer, "prepare", prepare)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        result = framework.install(str(archive_path), confirm_unverified_source=True)
        assert not result.success and "enable failed" in result.message
        assert not prepared.code_path.exists()
        assert not (prepared.code_path.parent / "active.json").exists()
        assert framework.list_plugins() == []
    finally:
        framework.close()


def test_package_update_preserves_disabled_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    更新已禁用归档时只验证新版加载，不意外启用插件。
    """
    data_path = tmp_path / "data"
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    resource_path = _resource_path(tmp_path, asset)
    first = _prepared_package(tmp_path, data_path)
    second = _prepared_package(tmp_path, data_path, "2.0.0")
    _ready_worker_paths(monkeypatch)
    monkeypatch.setattr(
        PluginPackagePreparer,
        "prepare",
        lambda _self, path, **_kwargs: first if "1.0.0" in str(path) else second,
    )
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        assert framework.install(str(tmp_path / "demo-1.0.0.eclplugin"), confirm_unverified_source=True).success
        assert framework.disable("demo").success
        assert framework.install(str(tmp_path / "demo-2.0.0.eclplugin"), confirm_unverified_source=True).success
        assert framework.list_plugins()[0]["status"] == "disabled"
        active = framework._package_store.restore("demo")
        assert active is not None and active.version == "2.0.0" and active.worker is None
    finally:
        framework.close()


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


def test_package_inspect_and_install_ipc_run_outside_event_loop(tmp_path: Path) -> None:
    """
    归档预检和安装的文件、网络及进程调用均不阻塞正式 IPC 事件循环。
    """
    preflight = _prepared_package(tmp_path, tmp_path / "data").preflight
    called_threads: list[int] = []

    class PackageActions:
        def inspect_package(self, source_path: str) -> PluginPackagePreflight:
            called_threads.append(get_ident())
            assert source_path.endswith(".eclplugin")
            return preflight

        def install(self, source_path: str, **options: object) -> PluginActionResult:
            called_threads.append(get_ident())
            assert source_path.endswith(".eclplugin")
            assert options == {
                "confirm_unverified_source": True,
                "allow_network": True,
                "offline_runtime_pack": None,
            }
            return PluginActionResult("demo", PluginAction.INSTALL, "installed")

    handler = PluginHandlers.__new__(PluginHandlers)
    handler.plugins = PackageActions()
    caller_thread = get_ident()
    archive_path = str(tmp_path / "demo-1.0.0.eclplugin")
    inspected = asyncio.run(handler.plugin_package_inspect({"plugin_path": archive_path}))
    assert inspected["success"] and inspected["data"]["unverified_source"] is True
    installed = asyncio.run(
        handler.plugin_install({"plugin_path": archive_path, "confirm_unverified_source": True, "allow_network": True})
    )
    assert installed == {"success": True}
    assert len(called_threads) == 2 and all(thread != caller_thread for thread in called_threads)


def test_plugin_command_ipc_runs_outside_event_loop() -> None:
    """
    Worker 命令的进程等待不占用正式 IPC 事件循环。
    """

    class CommandActions:
        def call_command(self, command: str, params: dict[str, object]) -> dict[str, object]:
            return {"command": command, "params": params, "thread": get_ident()}

    handler = PluginHandlers.__new__(PluginHandlers)
    handler.plugins = CommandActions()
    caller_thread = get_ident()
    result = asyncio.run(handler.plugin_call_command({"command": "demo:echo", "params": {"value": "ok"}}))
    assert result["success"] and result["data"] == {
        "command": "demo:echo",
        "params": {"value": "ok"},
        "thread": result["data"]["thread"],
    }
    assert result["data"]["thread"] != caller_thread
