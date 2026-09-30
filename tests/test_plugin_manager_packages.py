from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from plugin_wheel_helpers import make_package, make_wheel

from ECL.plugins.framework import PluginCommandError, PluginManager
from ECL.plugins.host_dependencies import HostDependencyPolicy


@pytest.fixture(autouse=True)
def isolated_process_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(HostDependencyPolicy, "_process_packages", {})
    monkeypatch.setattr(HostDependencyPolicy, "_process_imports", {})
    monkeypatch.setattr(HostDependencyPolicy, "_process_paths", set())
    monkeypatch.setattr(sys, "path", sys.path.copy())


def resources(tmp_path: Path) -> Path:
    root_path = tmp_path / "resource-root"
    manifest_path = root_path / "resources" / "plugin_host_dependencies.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"format_version": 1, "packages": []}), encoding="utf-8")
    return root_path


def test_install_waits_for_restart_then_runs_in_main_process(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    archive_path = make_package(tmp_path)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        assert not framework.install(str(archive_path)).success
        result = framework.install(str(archive_path), confirm_unverified_source=True)
        assert result.success and "重启" in result.message
        assert framework.get_plugin("demo") is None
        assert framework.list_plugins()[0]["pending_restart"]
        assert not framework.enable("demo").success
    finally:
        framework.close()
    restarted = PluginManager()
    restarted.initialize(data_path, resource_path)
    try:
        assert restarted.get_plugin("demo") is not None
        assert restarted.list_plugins()[0]["status"] == "enabled"
        assert restarted.call_command("demo:pid") == os.getpid()
        with pytest.raises(PluginCommandError):
            restarted.call_command("demo:not_registered")
        assert restarted.disable("demo").success
        with pytest.raises(PluginCommandError):
            restarted.call_command("demo:pid")
        assert restarted.enable("demo").success
        assert restarted.reload("demo").success
        assert restarted.uninstall("demo").success
        assert restarted.list_plugins() == []
    finally:
        restarted.close()


def test_update_keeps_loaded_version_until_restart(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    first = make_package(tmp_path)
    second = make_package(tmp_path, version="2.0.0")
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    framework.install(str(first), confirm_unverified_source=True)
    framework.close()
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    try:
        assert framework.install(str(second), confirm_unverified_source=True).success
        entry = framework.list_plugins()[0]
        assert entry["version"] == "1.0.0" and entry["installed_version"] == "2.0.0" and entry["pending_restart"]
        assert framework.call_command("demo:pid") == os.getpid()
        assert not framework.reload("demo").success
        assert framework.disable("demo").success
        assert not framework.enable("demo").success
    finally:
        framework.close()
    restarted = PluginManager()
    restarted.initialize(data_path, resource_path)
    try:
        assert restarted.list_plugins()[0]["version"] == "2.0.0"
        assert restarted.list_plugins()[0]["status"] == "disabled"
    finally:
        restarted.close()


def test_install_does_not_execute_failing_entry(tmp_path: Path) -> None:
    framework = PluginManager()
    framework.initialize(tmp_path / "data", resources(tmp_path))
    archive_path = make_package(tmp_path, code="raise RuntimeError('entry failed')\n")
    try:
        assert framework.install(str(archive_path), confirm_unverified_source=True).success
    finally:
        framework.close()


def test_safe_mode_does_not_execute_archives(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    framework.install(str(make_package(tmp_path)), confirm_unverified_source=True)
    framework.close()
    safe = PluginManager()
    safe.initialize(data_path, resource_path, auto_enable=False)
    try:
        assert safe.get_plugin("demo") is None
        assert safe.list_plugins()[0]["status"] == "unloaded"
        assert safe.enable("demo").success
        assert safe.call_command("demo:pid") == os.getpid()
    finally:
        safe.close()


def test_startup_conflicting_archives_do_not_execute_either(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    framework = PluginManager()
    framework.initialize(data_path, resource_path)
    for name, version in (("first", "1.0.0"), ("second", "2.0.0")):
        wheel = make_wheel(tmp_path / name, version=version)
        archive = make_package(
            tmp_path,
            name=name,
            wheel_paths=(wheel,),
            dependencies=("ecl-test-dependency>=1",),
            code="raise RuntimeError('must not execute')\n",
        )
        assert framework.install(str(archive), confirm_unverified_source=True).success
    framework.close()
    restarted = PluginManager()
    restarted.initialize(data_path, resource_path)
    try:
        entries = restarted.list_plugins()
        assert {entry["status"] for entry in entries} == {"disabled"}
        assert all("插件依赖冲突" in entry["error"] for entry in entries)
        assert restarted.get_plugin("first") is None and restarted.get_plugin("second") is None
    finally:
        restarted.close()


def test_on_load_failure_is_not_enabled(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    code = "from ecl_plugin_sdk import Plugin as BasePlugin\nclass Plugin(BasePlugin):\n    def on_load(self):\n        raise RuntimeError('load failed')\n    def on_enable(self):\n        raise AssertionError('must not enable')\n"
    installer = PluginManager()
    installer.initialize(data_path, resource_path)
    assert installer.install(str(make_package(tmp_path, code=code)), confirm_unverified_source=True).success
    installer.close()
    restarted = PluginManager()
    restarted.initialize(data_path, resource_path)
    try:
        assert restarted.list_plugins()[0]["status"] == "error"
        assert "load failed" in restarted.list_plugins()[0]["error"]
        assert not restarted.enable("demo").success
    finally:
        restarted.close()


def test_archive_host_sdk_and_plugin_dependency_order(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    code = "from ECL.plugins.plugin import Plugin as BasePlugin\nclass Plugin(BasePlugin):\n    @BasePlugin.on_command('value')\n    def value(self):\n        return 'host'\n"
    installer = PluginManager()
    installer.initialize(data_path, resource_path)
    for archive in (
        make_package(
            tmp_path, name="base", code=code, permissions=({"scope": "commands", "action": "execute", "resource": "*"},)
        ),
        make_package(tmp_path, name="dependent", plugin_dependencies={"base": ">=1"}),
    ):
        assert installer.install(str(archive), confirm_unverified_source=True).success
    installer.close()
    restarted = PluginManager()
    restarted.initialize(data_path, resource_path)
    try:
        assert restarted._dependency_resolution.load_order.index(
            "base"
        ) < restarted._dependency_resolution.load_order.index("dependent")
        assert restarted.call_command("base:value") == "host"
        assert restarted.disable("base").success
        assert not restarted.enable("dependent").success
    finally:
        restarted.close()


def test_unloaded_archive_reports_missing_plugin_dependency(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    resource_path = resources(tmp_path)
    installer = PluginManager()
    installer.initialize(data_path, resource_path)
    archive = make_package(tmp_path, plugin_dependencies={"missing": ">=1"})
    assert installer.install(str(archive), confirm_unverified_source=True).success
    installer.close()
    restarted = PluginManager()
    restarted.initialize(data_path, resource_path)
    try:
        assert "missing" in restarted.list_plugins()[0]["error"]
        assert restarted.get_plugin("demo") is None
    finally:
        restarted.close()
