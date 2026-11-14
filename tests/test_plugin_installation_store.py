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
    assert WorkerManifest.model_validate({"name": "example", "api_version": 1}).entry_file == "main.py"
    # 权限声明不再是清单字段；旧清单中的 permissions 被忽略而非报错。
    ignored = WorkerManifest.model_validate(
        {"name": "example", "api_version": 1, "permissions": [{"capability": "game"}]}
    )
    assert not hasattr(ignored, "permissions")
    for metadata in (
        {"name": "example"},
        {"name": "example", "api_version": True},
        {"name": "../escape", "api_version": 1},
    ):
        with pytest.raises(ValueError):
            WorkerManifest.model_validate(metadata)


def test_manifest_homepage_and_license_defaults_and_validation() -> None:
    manifest = WorkerManifest.model_validate(
        {
            "name": "example",
            "api_version": 1,
            "homepage": "https://github.com/ECLTeam/ECL-Plugin-OnlineGame",
            "license": "GPL-3.0",
        }
    )
    assert manifest.homepage == "https://github.com/ECLTeam/ECL-Plugin-OnlineGame"
    assert manifest.license == "GPL-3.0"
    # 空白被规范化；未声明时为空字符串。
    assert WorkerManifest.model_validate({"name": "example", "api_version": 1, "license": "  MIT "}).license == "MIT"
    empty = WorkerManifest.model_validate({"name": "example", "api_version": 1})
    assert empty.homepage == ""
    assert empty.license == ""
    for metadata in (
        {"name": "example", "api_version": 1, "homepage": "ftp://example.com/plugin"},
        {"name": "example", "api_version": 1, "homepage": "not-a-url"},
        {"name": "example", "api_version": 1, "homepage": "https://" + "a" * 300},
        {"name": "example", "api_version": 1, "license": "L" * 129},
    ):
        with pytest.raises(ValueError):
            WorkerManifest.model_validate(metadata)


def test_manifest_api_version_is_checked_against_supported_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    api_version 改为 ADR-005 支持集合校验：集合内放行，集合外拒绝且错误码专指版本。
    """
    from pydantic import ValidationError

    from ECL.plugins.api.contracts import ProtocolPolicy
    from ECL.plugins.installation_store import manifest_validation_error_code

    assert WorkerManifest.model_validate({"name": "example", "api_version": 1}).api_version == 1
    with pytest.raises(ValidationError) as unsupported:
        WorkerManifest.model_validate({"name": "example", "api_version": 2})
    assert manifest_validation_error_code(unsupported.value) == "PLUGIN_API_VERSION_UNSUPPORTED"

    # 支持集合扩展为 {1, 2} 后，v1 与 v2 清单同时可用（多版本并存基线）。
    monkeypatch.setattr(ProtocolPolicy, "supported_api_versions", frozenset({1, 2}))
    assert WorkerManifest.model_validate({"name": "example", "api_version": 2}).api_version == 2


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
    assert (
        WorkerManifest.model_validate({"name": "example", "api_version": 1, "minLauncher": "0.3.1"}).min_launcher
        == "0.3.1"
    )
    assert (
        WorkerManifest.model_validate({"name": "example", "api_version": 1, "minLauncher": "0.2.0"}).min_launcher
        == "0.2.0"
    )
    with pytest.raises(ValidationError) as too_new:
        WorkerManifest.model_validate({"name": "example", "api_version": 1, "minLauncher": "0.4.0"})
    assert manifest_validation_error_code(too_new.value) == "PLUGIN_LAUNCHER_VERSION_UNSUPPORTED"

    with pytest.raises(ValidationError) as malformed:
        WorkerManifest.model_validate({"name": "example", "api_version": 1, "minLauncher": "not-a-version"})
    assert manifest_validation_error_code(malformed.value) == "PLUGIN_MANIFEST_INVALID"


def test_manifest_features_are_whitelisted_but_need_not_be_enabled() -> None:
    """
    features 只做已知能力白名单校验；未知能力拒绝且错误码专指能力，已知能力无论是否启用都放行。
    """
    from pydantic import ValidationError

    from ECL.plugins.installation_store import manifest_validation_error_code

    manifest = WorkerManifest.model_validate(
        {"name": "example", "api_version": 1, "features": ["panels", "hot_reload"]}
    )
    assert manifest.features == ["panels", "hot_reload"]
    with pytest.raises(ValidationError) as unknown:
        WorkerManifest.model_validate({"name": "example", "api_version": 1, "features": ["teleport"]})
    assert manifest_validation_error_code(unknown.value) == "PLUGIN_FEATURE_UNSUPPORTED"


def test_manifest_error_codes_distinguish_fields_not_swallowed_by_generic_code() -> None:
    """
    多字段同时失败时归入清单无效，单一协商字段失败时各自返回专码。
    """
    from pydantic import ValidationError

    from ECL.plugins.installation_store import manifest_validation_error_code

    with pytest.raises(ValidationError) as combined:
        WorkerManifest.model_validate({"name": "example", "api_version": 99, "features": ["nope"]})
    assert manifest_validation_error_code(combined.value) == "PLUGIN_MANIFEST_INVALID"
