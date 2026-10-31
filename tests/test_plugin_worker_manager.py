from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest

from ECL.plugins import PluginManager
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller


def make_plugin(root: Path, marker: Path, *, api_version=2):
    root.mkdir(parents=True, exist_ok=True)
    metadata = {
        "name": "example",
        "version": "1.0.0",
        "api_version": api_version,
        "permissions": [{"capability": c, "required": True} for c in ()],
    }
    (root / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "main.py").write_text(
        "from ECL.plugin_api import Plugin as BasePlugin\nfrom pathlib import Path\nimport os\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "class Plugin(BasePlugin):\n"
        "    def on_load(self):\n"
        "        self.host.call('settings.define', {'definitions':[{'key':'greeting','type':'string','default':'hello'}]})\n"
        "        self.define_command('echo', self.echo, input_fields=({'name':'value','type':'string'},),"
        " output_fields=({'name':'value','type':'string'},))\n"
        "    def echo(self, value):\n"
        "        return {'value':value}\n",
        encoding="utf-8",
    )


def review(manager, decisions=None):
    service = manager.worker_service
    view = service.permissions("example")
    if decisions is None:
        decisions = tuple(PermissionDecision(capability=item["capability"], decision="allow") for item in view["items"])
    return service.update_permissions(
        "example", view["content_digest"], view["revision"], decisions, PermissionManagementCaller.main_webview
    )


def test_discovery_and_directory_install_do_not_execute_before_review(tmp_path: Path):
    marker = tmp_path / "executed"
    source = tmp_path / "source"
    make_plugin(source, marker)
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert manager.install(str(source)).success
        assert not marker.exists()
        assert manager.get_plugin("example") is None
        assert manager.list_plugins()[0]["review_required"] is True
        assert not manager.enable("example").success
        assert not marker.exists()
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="真实 Worker 运行仅在 Windows 验收")
def test_manager_real_worker_review_command_settings_revoke_and_restart(tmp_path: Path):
    marker = tmp_path / "executed"
    make_plugin(tmp_path / "data/plugins/example", marker)
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert not marker.exists()
        review(manager)
        assert manager.enable("example").success, manager.list_plugins()
        assert int(marker.read_text()) != os.getpid()
        assert manager.get_plugin("example") is None
        assert manager.call_command("example:echo", {"value": "hello"}) == {"value": "hello"}
        assert manager.get_settings("example")["values"] == {"greeting": "hello"}
        original_run = manager.worker_service.runtime_info("example")["run_id"]
        review(manager, ())
        assert manager.worker_service.runtime_info("example")["run_state"] == "enabled"
        assert manager.disable("example").success
        review(manager)
        assert manager.list_plugins()[0]["error"] is None
        assert manager.enable("example").success
        assert manager.worker_service.runtime_info("example")["run_id"] != original_run
        assert manager.disable("example").success
        assert manager.worker_service.runtime_info("example")["cleanup_state"] == "completed"
    finally:
        manager.close()


def test_legacy_third_party_never_executes_in_host(tmp_path: Path):
    marker = tmp_path / "executed"
    make_plugin(tmp_path / "data/plugins/example", marker, api_version=1)
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert not marker.exists()
        assert manager.list_plugins()[0]["status"] == "unsupported"
        assert not manager.enable("example").success
    finally:
        manager.close()


def test_non_windows_worker_rejection_never_executes_entry(tmp_path: Path, monkeypatch):
    marker = tmp_path / "executed"
    make_plugin(tmp_path / "data/plugins/example", marker)
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        review(manager)
        monkeypatch.setattr("ECL.plugins.worker_service.platform.system", lambda: "Linux")
        assert not manager.enable("example").success
        assert not marker.exists()
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Worker")
def test_unexpected_worker_exit_cleans_resources_without_page_polling(tmp_path: Path):
    root = tmp_path / "data/plugins/example"
    make_plugin(root, tmp_path / "marker")
    source = (root / "main.py").read_text(encoding="utf-8")
    source += "\n    def on_enable(self):\n        import threading\n        threading.Timer(0.3, lambda: os._exit(7)).start()\n"
    (root / "main.py").write_text(source, encoding="utf-8")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        review(manager)
        assert manager.enable("example").success
        deadline = monotonic() + 5
        while monotonic() < deadline:
            runtime = manager.worker_service.runtime_info("example")
            if runtime["run_state"] == "failed":
                break
            sleep(0.05)
        assert runtime["run_state"] == "failed"
        assert runtime["cleanup_state"] == "completed"
        assert manager.list_plugins()[0]["error"] == "PLUGIN_WORKER_EXITED"
        assert manager.worker_service._entries["example"].supervisor.active_count() == 0
    finally:
        manager.close()


@pytest.mark.parametrize("metadata", [[], {"name": ["bad"]}, {"name": "example", "api_version": 2, "dependencies": []}])
def test_malformed_manifest_cannot_break_discovery(tmp_path: Path, metadata):
    root = tmp_path / "data/plugins/example"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "main.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert manager.get_plugin("example") is None
        assert all(item["status"] != "enabled" for item in manager.list_plugins())
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Worker")
def test_directory_with_complete_offline_lock_loads_its_own_dependency(tmp_path: Path):
    from plugin_wheel_helpers import make_wheel, wheel_lock

    from ECL.plugins.package_preparation import current_plugin_target

    source = tmp_path / "source"
    make_plugin(source, tmp_path / "marker")
    metadata_path = source / "plugin.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["pythonDependencies"] = ["samplelib==1.0"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    wheel = make_wheel(
        source / "wheels" / current_plugin_target(),
        "samplelib",
        "1.0",
        members={"samplelib/__init__.py": b"VALUE = 'offline-directory'\n"},
    )
    locks = source / "locks"
    locks.mkdir()
    (locks / f"{current_plugin_target()}.txt").write_bytes(wheel_lock(wheel))
    code = (source / "main.py").read_text(encoding="utf-8")
    code = "import samplelib\n" + code.replace("return {'value':value}", "return {'value':samplelib.VALUE}")
    (source / "main.py").write_text(code, encoding="utf-8")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert manager.install(str(source)).success
        review(manager)
        assert manager.list_plugins()[0]["status"] == "disabled"
        assert manager.enable("example").success
        assert manager.call_command("example:echo", {"value": "ignored"}) == {"value": "offline-directory"}
    finally:
        manager.close()


def test_unsupported_plugin_can_be_disabled_and_uninstalled(tmp_path: Path):
    make_plugin(tmp_path / "data/plugins/example", tmp_path / "marker", api_version=1)
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert manager.disable("example").success
        assert manager.uninstall("example").success
        assert manager.list_plugins() == []
    finally:
        manager.close()


def test_changed_directory_contents_require_new_review(tmp_path: Path):
    root = tmp_path / "data/plugins/example"
    make_plugin(root, tmp_path / "marker")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        review(manager)
        before = manager.worker_service.permissions("example")
        with (root / "main.py").open("a", encoding="utf-8") as stream:
            stream.write("\n# reviewed contents changed\n")
        after = manager.worker_service.permissions("example")
        assert after["installation_id"] == before["installation_id"]
        assert after["content_digest"] != before["content_digest"]
        assert after["revision"] == 0
        assert not manager.enable("example").success
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Worker")
def test_callback_timeout_denies_new_calls_and_monitor_confirms_stop(tmp_path: Path):
    from ECL.plugins.framework import PluginCommandError

    root = tmp_path / "data/plugins/example"
    make_plugin(root, tmp_path / "marker")
    source = (root / "main.py").read_text(encoding="utf-8")
    source = source.replace("return {'value':value}", "__import__('time').sleep(5)\n        return {'value':value}")
    (root / "main.py").write_text(source, encoding="utf-8")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        review(manager)
        assert manager.enable("example").success
        with pytest.raises(PluginCommandError):
            manager.call_command("example:echo", {"value": "slow"}, timeout=0.1)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            runtime = manager.worker_service.runtime_info("example")
            if runtime["run_state"] == "failed":
                break
            sleep(0.05)
        assert runtime["run_state"] == "failed"
        assert runtime["cleanup_state"] == "completed"
        assert runtime["last_error"]["code"] == "PLUGIN_CALLBACK_TIMEOUT"
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Worker")
def test_install_waits_for_concurrent_enable_then_stops_old_worker(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import psutil

    marker = tmp_path / "marker"
    make_plugin(tmp_path / "data/plugins/example", marker)
    new_source = tmp_path / "source"
    make_plugin(new_source, marker)
    code = (new_source / "main.py").read_text(encoding="utf-8")
    (new_source / "main.py").write_text(code + "\n# new installed content\n", encoding="utf-8")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    enable_started, release_enable, install_started = Event(), Event(), Event()
    original_enable = manager.worker_service.enable
    original_install = manager._install_directory

    def slow_enable(*args, **kwargs):
        enable_started.set()
        assert release_enable.wait(5)
        return original_enable(*args, **kwargs)

    def observed_install(*args, **kwargs):
        install_started.set()
        return original_install(*args, **kwargs)

    monkeypatch.setattr(manager.worker_service, "enable", slow_enable)
    monkeypatch.setattr(manager, "_install_directory", observed_install)
    try:
        review(manager)
        with ThreadPoolExecutor(max_workers=2) as executor:
            enabling = executor.submit(manager.enable, "example")
            assert enable_started.wait(2)
            installing = executor.submit(manager.install, str(new_source))
            assert install_started.wait(2)
            assert not installing.done()
            release_enable.set()
            assert enabling.result(15).success
            assert installing.result(15).success
        assert manager.list_plugins()[0]["review_required"]
        assert manager.worker_service.runtime_info("example")["pid"] is None
        assert not psutil.pid_exists(int(marker.read_text()))
    finally:
        release_enable.set()
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows Worker")
def test_disable_keeps_pending_until_accepted_basic_write_has_finished(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import ECL.plugins.runtime.broker as broker_module
    from ECL.plugin_api._rpc import RpcRequest

    make_plugin(tmp_path / "data/plugins/example", tmp_path / "marker")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    write_started, release_write = Event(), Event()
    original_write = broker_module.atomic_write_text

    def delayed_write(*args):
        write_started.set()
        assert release_write.wait(10)
        return original_write(*args)

    try:
        review(manager)
        assert manager.enable("example").success
        broker = manager.worker_service.broker("example")
        monkeypatch.setattr(broker_module, "atomic_write_text", delayed_write)
        with ThreadPoolExecutor(max_workers=1) as executor:
            writing = executor.submit(
                broker.handle,
                RpcRequest(
                    "settings.set",
                    {"values": {"greeting": "committed"}, "expected_revision": 0},
                    monotonic() + 10,
                    "accepted-write",
                ),
            )
            assert write_started.wait(2)
            from ECL.plugin_api._windows import WindowsRuntimeError

            with pytest.raises(WindowsRuntimeError) as error:
                manager.worker_service.stop("example")
            assert error.value.error_code == "PLUGIN_HOST_CLEANUP_PENDING"
            assert manager.worker_service.runtime_info("example")["cleanup_state"] == "pending"
            assert not writing.done()
            release_write.set()
            assert writing.result(5) == {"revision": 1}
        deadline = monotonic() + 5
        while monotonic() < deadline:
            runtime = manager.worker_service.runtime_info("example")
            if runtime["cleanup_state"] == "completed":
                break
            sleep(0.05)
        assert runtime["cleanup_state"] == "completed"
        settings = next((tmp_path / "data/plugin_settings_v2").glob("*.json"))
        assert json.loads(settings.read_bytes())["values"] == {"greeting": "committed"}
    finally:
        release_write.set()
        manager.close()
