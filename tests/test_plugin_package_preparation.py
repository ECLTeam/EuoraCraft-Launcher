from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from plugin_wheel_helpers import make_package, make_wheel

from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.package_preparation import PluginPackagePreparer, verify_prepared_code


def test_no_dependency_plugin_needs_no_runtime_and_does_not_execute(tmp_path: Path) -> None:
    archive_path = make_package(tmp_path, code="raise RuntimeError('must not execute')\n")
    preparer = PluginPackagePreparer(tmp_path / "data")
    assert preparer.inspect(archive_path).dependencies_ready
    prepared = preparer.prepare(archive_path, confirm_unverified_source=True)
    assert prepared.dependency_directory is None and prepared.environment_key == ""
    assert not (tmp_path / "data" / "plugin_deps").exists()
    assert not (tmp_path / "data" / "plugin_runtimes").exists()


def test_dependencies_prepare_offline_in_data_directory(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "wheels")
    archive_path = make_package(tmp_path, wheel_paths=(wheel,), dependencies=("ecl_test_dependency>=1",))
    preparer = PluginPackagePreparer(tmp_path / "data")
    assert not preparer.inspect(archive_path).dependencies_ready
    prepared = preparer.prepare(archive_path, confirm_unverified_source=True)
    assert prepared.dependency_directory
    assert (prepared.dependency_directory.site_path / "ecl_test_dependency.py").is_file()
    assert preparer.inspect(archive_path).dependencies_ready


def test_requires_confirmation_before_creating_data_directory(tmp_path: Path) -> None:
    archive_path = make_package(tmp_path)
    with pytest.raises(PluginDependencyError, match="必须确认"):
        PluginPackagePreparer(tmp_path / "data").prepare(archive_path, confirm_unverified_source=False)
    assert not (tmp_path / "data").exists()


def test_changed_archive_is_rejected_before_extract(tmp_path: Path) -> None:
    archive_path = make_package(tmp_path)
    preparer = PluginPackagePreparer(tmp_path / "data")
    package = preparer.inspect(archive_path).package
    with pytest.raises(PluginDependencyError, match="预检后发生变化"):
        preparer.prepare(archive_path, confirm_unverified_source=True, expected_package=replace(package, version="99"))
    assert not (tmp_path / "data" / "plugin_packages").exists()


def test_missing_target_lock_is_rejected(tmp_path: Path) -> None:
    archive_path = make_package(tmp_path, dependencies=("missing>=1",))
    with pytest.raises(PluginDependencyError, match="缺少当前目标依赖锁"):
        PluginPackagePreparer(tmp_path / "data").inspect(archive_path)


@pytest.mark.parametrize("mutation", ["code", "extra", "manifest"])
def test_reused_code_is_checked_again(tmp_path: Path, mutation: str) -> None:
    archive_path = make_package(tmp_path)
    preparer = PluginPackagePreparer(tmp_path / "data")
    prepared = preparer.prepare(archive_path, confirm_unverified_source=True)
    if mutation == "code":
        (prepared.code_path / "main.py").write_bytes(b"changed")
    elif mutation == "extra":
        (prepared.code_path / "extra.py").write_bytes(b"extra")
    else:
        (prepared.code_path / "package-manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    with pytest.raises(PluginDependencyError):
        preparer.prepare(archive_path, confirm_unverified_source=True)


def test_verified_code_rejects_symlink(tmp_path: Path) -> None:
    archive_path = make_package(tmp_path)
    prepared = PluginPackagePreparer(tmp_path / "data").prepare(archive_path, confirm_unverified_source=True)
    try:
        (prepared.code_path / "extra").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(PluginDependencyError, match="符号链接"):
        verify_prepared_code(prepared.code_path, prepared.preflight.package.manifest_sha256)
