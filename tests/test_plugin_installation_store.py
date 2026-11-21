from __future__ import annotations

import json
from pathlib import Path

import pytest

from ECL.plugins.installation_store import PluginInstallationError, PluginInstallationStore, PluginManifest


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


def test_manifest_requires_explicit_name_and_ignores_legacy_permissions() -> None:
    assert PluginManifest.model_validate({"name": "example"}).entry_file == "main.py"
    # 权限声明不再是清单字段；旧清单中的 permissions 被忽略而非报错。
    # api_version 等历史版本化字段同样被忽略，不再参与校验。
    ignored = PluginManifest.model_validate({"name": "example", "permissions": [{"capability": "game"}]})
    assert not hasattr(ignored, "permissions")
    for metadata in (
        {"name": "example", "entry_point": "../escape:Plugin"},
        {"name": "../escape"},
    ):
        with pytest.raises(ValueError):
            PluginManifest.model_validate(metadata)


def test_manifest_homepage_and_license_defaults_and_validation() -> None:
    manifest = PluginManifest.model_validate(
        {
            "name": "example",
            "homepage": "https://github.com/ECLTeam/ECL-Plugin-Example",
            "license": "GPL-3.0",
        }
    )
    assert manifest.homepage == "https://github.com/ECLTeam/ECL-Plugin-Example"
    assert manifest.license == "GPL-3.0"
    # 空白被规范化；未声明时为空字符串。
    assert PluginManifest.model_validate({"name": "example", "license": "  MIT "}).license == "MIT"
    empty = PluginManifest.model_validate({"name": "example"})
    assert empty.homepage == ""
    assert empty.license == ""
    for metadata in (
        {"name": "example", "homepage": "ftp://example.com/plugin"},
        {"name": "example", "homepage": "not-a-url"},
        {"name": "example", "homepage": "https://" + "a" * 300},
        {"name": "example", "license": "L" * 129},
    ):
        with pytest.raises(ValueError):
            PluginManifest.model_validate(metadata)


def test_manifest_min_launcher_is_compared_with_host_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    min_launcher 高于宿主版本拒绝且错误码专指版本；等于或低于放行；格式非法归清单无效。
    """
    from pydantic import ValidationError

    import ECL.plugins.installation_store as store
    from ECL.plugins.installation_store import manifest_validation_error_code

    def _with_host_version(version: str) -> None:
        monkeypatch.setattr(store, "__version__", version)

    _with_host_version("0.3.1")
    assert PluginManifest.model_validate({"name": "example", "minLauncher": "0.3.1"}).min_launcher == "0.3.1"
    assert PluginManifest.model_validate({"name": "example", "minLauncher": "0.2.0"}).min_launcher == "0.2.0"
    with pytest.raises(ValidationError) as too_new:
        PluginManifest.model_validate({"name": "example", "minLauncher": "0.4.0"})
    assert manifest_validation_error_code(too_new.value) == "PLUGIN_LAUNCHER_VERSION_UNSUPPORTED"

    with pytest.raises(ValidationError) as malformed:
        PluginManifest.model_validate({"name": "example", "minLauncher": "not-a-version"})
    assert manifest_validation_error_code(malformed.value) == "PLUGIN_MANIFEST_INVALID"


def test_manifest_features_are_whitelisted_but_need_not_be_enabled() -> None:
    """
    features 只做已知能力白名单校验；未知能力拒绝且错误码专指能力，已知能力无论是否启用都放行。
    """
    from pydantic import ValidationError

    from ECL.plugins.installation_store import manifest_validation_error_code

    manifest = PluginManifest.model_validate({"name": "example", "features": ["panels", "hot_reload"]})
    assert manifest.features == ["panels", "hot_reload"]
    with pytest.raises(ValidationError) as unknown:
        PluginManifest.model_validate({"name": "example", "features": ["teleport"]})
    assert manifest_validation_error_code(unknown.value) == "PLUGIN_FEATURE_UNSUPPORTED"


def test_manifest_error_codes_distinguish_fields_not_swallowed_by_generic_code() -> None:
    """
    多字段同时失败时归入清单无效，单一协商字段失败时各自返回专码。
    """
    from pydantic import ValidationError

    from ECL.plugins.installation_store import manifest_validation_error_code

    with pytest.raises(ValidationError) as combined:
        PluginManifest.model_validate({"name": "../escape", "features": ["nope"]})
    assert manifest_validation_error_code(combined.value) == "PLUGIN_MANIFEST_INVALID"
