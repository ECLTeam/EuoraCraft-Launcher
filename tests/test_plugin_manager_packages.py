from __future__ import annotations

import json
import os
import platform
import sys

import pytest
from plugin_wheel_helpers import make_package, make_wheel

from ECL.plugins.api._errors import PluginRuntimeError
from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.framework import PluginManager
from ECL.plugins.runtime.supervisor import WorkerSupervisor


def install_and_restart(tmp_path, archive):
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        assert manager.install(str(archive), confirm_unverified_source=True).success
        assert manager.get_plugin("demo") is None
        assert manager.list_plugins()[0]["pending_restart"]
        assert not manager.enable("demo").success
    finally:
        manager.close()
    restarted = PluginManager()
    restarted.initialize(tmp_path / "data", tmp_path / "resources")
    return restarted


@pytest.mark.parametrize(
    "system,machine", [("win32", "ARM64"), ("linux", "riscv64"), ("darwin", "ppc"), ("freebsd", "x86_64")]
)
def test_unsupported_worker_platform_is_rejected_before_start(monkeypatch, system, machine):
    supervisor = WorkerSupervisor()
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    try:
        with pytest.raises(PluginRuntimeError) as error:
            supervisor.start()
        assert error.value.error_code == "PLUGIN_RUNTIME_UNSUPPORTED"
        assert supervisor.pid is None
        assert supervisor.state == "stopped"
    finally:
        supervisor.close()


def test_package_executes_in_worker_and_can_be_disabled_reloaded_uninstalled(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path))
    try:
        assert manager.enable("demo").success
        assert manager.call_command("demo:pid")["pid"] != os.getpid()
        assert manager.get_plugin("demo") is None
        assert manager.disable("demo").success
        assert manager.enable("demo").success
        assert manager.reload("demo").success
        assert manager.uninstall("demo").success
        assert manager.list_plugins() == []
    finally:
        manager.close()


def test_package_update_invalidates_review_and_waits_for_restart(tmp_path):
    first = make_package(tmp_path)
    second = make_package(tmp_path, version="2.0.0")
    manager = install_and_restart(tmp_path, first)
    try:
        identity = manager.list_plugins()[0]["installation_id"]
        assert manager.install(str(second), confirm_unverified_source=True).success
        entry = manager.list_plugins()[0]
        assert entry["installation_id"] == identity
        assert entry["installed_version"] == "2.0.0"
        assert entry["pending_restart"]
        assert not manager.enable("demo").success
        assert not manager.reload("demo").success
    finally:
        manager.close()


def test_install_does_not_execute_failing_entry(tmp_path):
    marker = tmp_path / "executed"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nraise RuntimeError('not now')\n"
    manager = install_and_restart(tmp_path, make_package(tmp_path, code=code))
    try:
        assert not marker.exists()
    finally:
        manager.close()


def test_safe_mode_discovers_package_without_starting_worker(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path))
    manager.close()
    safe = PluginManager()
    safe.initialize(tmp_path / "data", tmp_path / "resources", auto_enable=False)
    try:
        assert safe.get_plugin("demo") is None
        assert safe.worker_service.runtime_info("demo")["pid"] is None
    finally:
        safe.close()


def test_failure_in_on_load_does_not_report_enabled(tmp_path):
    code = "from ECL.plugins.api import Plugin as Base\nclass Plugin(Base):\n    def on_load(self):\n        raise RuntimeError('private detail')\n"
    manager = install_and_restart(tmp_path, make_package(tmp_path, code=code))
    try:
        result = manager.enable("demo")
        assert not result.success
        assert "private detail" not in result.message
        assert manager.worker_service.runtime_info("demo")["cleanup_state"] == "completed"
    finally:
        manager.close()


def test_package_dependency_missing_prevents_enable(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path, plugin_dependencies={"missing": ">=1"}))
    try:
        assert not manager.enable("demo").success
        assert manager.get_plugin("demo") is None
    finally:
        manager.close()


def test_two_package_workers_use_distinct_versions_without_host_dependency_conflict(tmp_path):
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    code = "from ECL.plugins.api import Plugin as Base\nimport samplelib\nclass Plugin(Base):\n    def on_load(self):\n        self.define_command('version', self.version, output_fields=({'name':'value','type':'string'},))\n    def version(self):\n        return {'value':samplelib.VALUE}\n"
    for name, version in (("one", "1.0"), ("two", "2.0")):
        folder = tmp_path / name
        folder.mkdir()
        wheel = make_wheel(
            folder, "samplelib", version, members={"samplelib/__init__.py": f"VALUE = {version!r}\n".encode()}
        )
        archive = make_package(
            folder,
            name=name,
            code=code,
            wheel_paths=(wheel,),
            dependencies=(f"samplelib=={version}",),
        )
        assert manager.install(str(archive), confirm_unverified_source=True).success
    manager.close()
    restarted = PluginManager()
    restarted.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        for name, version in (("one", "1.0"), ("two", "2.0")):
            assert restarted.enable(name).success
            assert restarted.call_command(f"{name}:version") == {"value": version}
        assert "samplelib" not in sys.modules
    finally:
        restarted.close()


def _write_directory_manifest(source, metadata):
    source.mkdir(parents=True, exist_ok=True)
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")


def test_inspect_directory_reports_manifest_and_dependency_state(tmp_path):
    source = tmp_path / "demo"
    _write_directory_manifest(
        source, {"name": "demo", "api_version": 1, "version": "1.2.3", "entry_point": "main:Plugin"}
    )
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        preflight = manager.inspect_directory(str(source))
        assert preflight.name == "demo"
        assert preflight.version == "1.2.3"
        assert preflight.source_path == str(source)
        assert preflight.python_dependencies == ()
        assert preflight.has_target_lock is False
        assert preflight.dependencies_ready is True
    finally:
        manager.close()


def test_inspect_directory_rejects_folder_without_manifest(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        with pytest.raises(PluginDependencyError) as error:
            manager.inspect_directory(str(empty))
        assert "plugin.json" in str(error.value)
    finally:
        manager.close()


def test_inspect_directory_requires_lock_for_python_dependencies(tmp_path):
    source = tmp_path / "demo"
    _write_directory_manifest(
        source,
        {
            "name": "demo",
            "api_version": 1,
            "version": "1.0.0",
            "entry_point": "main:Plugin",
            "pythonDependencies": ["example==1.0.0"],
        },
    )
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        with pytest.raises(PluginDependencyError) as error:
            manager.inspect_directory(str(source))
        assert "依赖锁" in str(error.value)
    finally:
        manager.close()
