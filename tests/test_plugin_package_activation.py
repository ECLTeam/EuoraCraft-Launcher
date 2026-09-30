from __future__ import annotations

import json
from pathlib import Path

import pytest
from plugin_wheel_helpers import make_package, make_wheel

from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.package_activation import PluginPackageActivationStore
from ECL.plugins.package_preparation import PluginPackagePreparer, current_plugin_target


def test_pointer_commits_without_executing_code_and_updates_atomically(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    preparer = PluginPackagePreparer(data_path)
    store = PluginPackageActivationStore(data_path)
    first = preparer.prepare(
        make_package(tmp_path, code="raise Exception('not executed')\n"), confirm_unverified_source=True
    )
    active = store.activate(first)
    assert store.restore("demo") == active
    second = preparer.prepare(make_package(tmp_path, version="2.0.0"), confirm_unverified_source=True)
    store.activate(second)
    assert store.restore("demo").version == "2.0.0"
    assert json.loads((first.code_path.parent / "previous.json").read_text(encoding="utf-8"))["version"] == "1.0.0"
    assert store.set_enabled("demo", False).enabled is False
    store.uninstall("demo")
    assert store.restore("demo") is None
    assert first.code_path.exists() and second.code_path.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_version", 99),
        ("install_mode", "worker"),
        ("manifest_sha256", "bad"),
        ("environment_key", "bad"),
        ("name", "other"),
        ("enabled", 1),
        ("target_tag", "other"),
    ],
)
def test_invalid_records_fail_closed(tmp_path: Path, field: str, value: object) -> None:
    data_path = tmp_path / "data"
    prepared = PluginPackagePreparer(data_path).prepare(make_package(tmp_path), confirm_unverified_source=True)
    store = PluginPackageActivationStore(data_path)
    store.activate(prepared)
    pointer_path = prepared.code_path.parent / "active.json"
    payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    payload[field] = value
    pointer_path.write_text(json.dumps(payload), encoding="utf-8")
    before = pointer_path.read_bytes()
    with pytest.raises(PluginDependencyError):
        store.restore("demo")
    assert pointer_path.read_bytes() == before


def test_legacy_record_migrates_offline_and_preserves_old_data(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    wheel = make_wheel(tmp_path / "wheels")
    prepared = PluginPackagePreparer(data_path).prepare(
        make_package(tmp_path, wheel_paths=(wheel,), dependencies=("ecl_test_dependency",)),
        confirm_unverified_source=True,
    )
    pointer_path = prepared.code_path.parent / "active.json"
    payload = {
        "name": "demo",
        "version": "1.0.0",
        "manifest_sha256": prepared.preflight.package.manifest_sha256,
        "environment_key": "a" * 64,
        "runtime_id": "old-runtime",
        "target_tag": current_plugin_target(),
        "enabled": True,
    }
    pointer_path.write_text(json.dumps(payload), encoding="utf-8")
    old_data_path = data_path / "plugin_envs" / "old"
    old_data_path.mkdir(parents=True)
    active = PluginPackageActivationStore(data_path).restore("demo")
    assert active.dependency_directory
    assert active.environment_key != payload["environment_key"]
    assert json.loads(pointer_path.read_text(encoding="utf-8"))["record_version"] == 2
    assert (pointer_path.parent / "active.worker-v1.json").exists()
    assert old_data_path.exists()


def test_failed_migration_keeps_legacy_pointer(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    wheel = make_wheel(tmp_path / "wheels")
    archive_path = make_package(tmp_path, wheel_paths=(wheel,), dependencies=("ecl_test_dependency",))
    # 只解包旧版本，不预先把 wheel 放进新缓存。
    from ECL.plugins.package_archive import extract_plugin_package, inspect_plugin_package

    info = inspect_plugin_package(archive_path)
    code_path = data_path / "plugin_packages" / "demo" / f"pkg-{info.manifest_sha256[:20]}"
    extract_plugin_package(archive_path, code_path)
    (code_path / "wheels" / current_plugin_target() / wheel.name).unlink()
    payload = {
        "name": "demo",
        "version": "1.0.0",
        "manifest_sha256": info.manifest_sha256,
        "environment_key": "a" * 64,
        "runtime_id": "old-runtime",
        "target_tag": current_plugin_target(),
        "enabled": True,
    }
    pointer_path = code_path.parent / "active.json"
    pointer_path.write_text(json.dumps(payload), encoding="utf-8")
    before = pointer_path.read_bytes()
    with pytest.raises(PluginDependencyError):
        PluginPackageActivationStore(data_path).restore("demo")
    assert pointer_path.read_bytes() == before
