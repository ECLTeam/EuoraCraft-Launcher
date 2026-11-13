from __future__ import annotations

import json
from pathlib import Path

import pytest

from ECL.plugins.installation_store import PluginInstallationError, PluginInstallationStore, WorkerManifest


def test_installation_survives_restart_and_reinstall_never_inherits_identity(tmp_path: Path) -> None:
    store = PluginInstallationStore(tmp_path)
    first = store.ensure("example", "directory")
    assert PluginInstallationStore(tmp_path).ensure("example", "directory") == first
    store.retire("example")
    assert store.ensure("example", "directory") != first


def test_corrupt_installation_is_preserved_and_never_replaced(tmp_path: Path) -> None:
    store = PluginInstallationStore(tmp_path)
    store.ensure("example", "directory")
    record = tmp_path / "plugins" / "installations" / "example.json"
    record.write_text('{"broken":true}', encoding="utf-8")
    with pytest.raises(PluginInstallationError):
        store.ensure("example", "directory")
    assert json.loads(record.read_text()) == {"broken": True}


@pytest.mark.parametrize("name", ["../escape", "a/b", "CON", "a.", ""])
def test_installation_names_cannot_escape_or_alias_windows_files(tmp_path: Path, name: str) -> None:
    with pytest.raises(PluginInstallationError):
        PluginInstallationStore(tmp_path).ensure(name, "directory")


def test_manifest_requires_explicit_v2_and_ignores_legacy_permissions() -> None:
    assert WorkerManifest.model_validate({"name": "example", "api_version": 2}).entry_file == "main.py"
    # 权限声明不再是清单字段；旧清单中的 permissions 被忽略而非报错。
    ignored = WorkerManifest.model_validate(
        {"name": "example", "api_version": 2, "permissions": [{"capability": "game"}]}
    )
    assert not hasattr(ignored, "permissions")
    for metadata in (
        {"name": "example"},
        {"name": "example", "api_version": True},
        {"name": "../escape", "api_version": 2},
    ):
        with pytest.raises(ValueError):
            WorkerManifest.model_validate(metadata)


def test_manifest_homepage_and_license_defaults_and_validation() -> None:
    manifest = WorkerManifest.model_validate(
        {
            "name": "example",
            "api_version": 2,
            "homepage": "https://github.com/ECLTeam/ECL-Plugin-OnlineGame",
            "license": "GPL-3.0",
        }
    )
    assert manifest.homepage == "https://github.com/ECLTeam/ECL-Plugin-OnlineGame"
    assert manifest.license == "GPL-3.0"
    # 空白被规范化；未声明时为空字符串。
    assert WorkerManifest.model_validate({"name": "example", "api_version": 2, "license": "  MIT "}).license == "MIT"
    empty = WorkerManifest.model_validate({"name": "example", "api_version": 2})
    assert empty.homepage == ""
    assert empty.license == ""
    for metadata in (
        {"name": "example", "api_version": 2, "homepage": "ftp://example.com/plugin"},
        {"name": "example", "api_version": 2, "homepage": "not-a-url"},
        {"name": "example", "api_version": 2, "homepage": "https://" + "a" * 300},
        {"name": "example", "api_version": 2, "license": "L" * 129},
    ):
        with pytest.raises(ValueError):
            WorkerManifest.model_validate(metadata)
