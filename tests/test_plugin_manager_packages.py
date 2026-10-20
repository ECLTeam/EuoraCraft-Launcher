from __future__ import annotations

import os
import sys

import pytest
from plugin_wheel_helpers import make_package, make_wheel

from ECL.plugins import PluginManager
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller


def review(manager, name="demo", *, allow=True):
    service = manager.worker_service
    view = service.permissions(name)
    return service.update_permissions(
        name,
        view["content_digest"],
        view["revision"],
        tuple(
            PermissionDecision(capability=item["capability"], decision="allow" if allow else "deny")
            for item in view["items"]
        ),
        PermissionManagementCaller.main_webview,
    )


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


def test_package_requires_review_after_install_and_restart(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path))
    try:
        assert manager.list_plugins()[0]["review_required"]
        assert not manager.enable("demo").success
        assert manager.get_plugin("demo") is None
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="真实 Worker 仅在 Windows 验收")
def test_package_executes_in_worker_and_can_be_disabled_reloaded_uninstalled(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path))
    try:
        review(manager)
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
        review(manager)
        identity = manager.list_plugins()[0]["installation_id"]
        assert manager.install(str(second), confirm_unverified_source=True).success
        entry = manager.list_plugins()[0]
        assert entry["installation_id"] == identity
        assert entry["installed_version"] == "2.0.0"
        assert entry["review_required"]
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
        assert manager.list_plugins()[0]["review_required"]
    finally:
        manager.close()


def test_safe_mode_discovers_package_without_starting_worker(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path))
    review(manager)
    manager.close()
    safe = PluginManager()
    safe.initialize(tmp_path / "data", tmp_path / "resources", auto_enable=False)
    try:
        assert safe.get_plugin("demo") is None
        assert safe.worker_service.runtime_info("demo")["pid"] is None
    finally:
        safe.close()


@pytest.mark.skipif(sys.platform != "win32", reason="真实 Worker 仅在 Windows 验收")
def test_failure_in_on_load_does_not_report_enabled(tmp_path):
    code = "from ECL.plugin_api import Plugin as Base\nclass Plugin(Base):\n    def on_load(self):\n        raise RuntimeError('private detail')\n"
    manager = install_and_restart(tmp_path, make_package(tmp_path, code=code))
    try:
        review(manager)
        result = manager.enable("demo")
        assert not result.success
        assert "private detail" not in result.message
        assert manager.worker_service.runtime_info("demo")["cleanup_state"] == "completed"
    finally:
        manager.close()


def test_package_dependency_missing_prevents_enable(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path, plugin_dependencies={"missing": ">=1"}))
    try:
        review(manager)
        assert not manager.enable("demo").success
        assert manager.get_plugin("demo") is None
    finally:
        manager.close()


def test_explicit_empty_permissions_are_not_inferred_from_code(tmp_path):
    manager = install_and_restart(tmp_path, make_package(tmp_path, permissions=()))
    try:
        view = manager.worker_service.permissions("demo")
        assert view["items"] == []
        assert view["revision"] == 0
        assert not manager.enable("demo").success
    finally:
        manager.close()


@pytest.mark.skipif(sys.platform != "win32", reason="真实 Worker 仅在 Windows 验收")
def test_two_package_workers_use_distinct_versions_without_host_dependency_conflict(tmp_path):
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    code = "from ECL.plugin_api import Plugin as Base\nimport samplelib\nclass Plugin(Base):\n    def on_load(self):\n        self.define_command('version', self.version, output_fields=({'name':'value','type':'string'},))\n    def version(self):\n        return {'value':samplelib.VALUE}\n"
    permissions = (
        {"capability": "commands.register", "required": True},
        {"capability": "commands.execute", "required": True},
    )
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
            permissions=permissions,
            wheel_paths=(wheel,),
            dependencies=(f"samplelib=={version}",),
        )
        assert manager.install(str(archive), confirm_unverified_source=True).success
    manager.close()
    restarted = PluginManager()
    restarted.initialize(tmp_path / "data", tmp_path / "resources")
    try:
        for name, version in (("one", "1.0"), ("two", "2.0")):
            review(restarted, name)
            assert restarted.enable(name).success
            assert restarted.call_command(f"{name}:version") == {"value": version}
        assert "samplelib" not in sys.modules
    finally:
        restarted.close()
