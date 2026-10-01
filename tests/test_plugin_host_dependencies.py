from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ECL.plugins.dependency_lock import PluginDependencyError
from ECL.plugins.environment_pool import InstalledPackage, PluginDependencyDirectory
from ECL.plugins.host_dependencies import HostDependencyPolicy, build_host_dependency_manifest


@pytest.fixture(autouse=True)
def isolated_process_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HostDependencyPolicy, "_process_packages", {})
    monkeypatch.setattr(HostDependencyPolicy, "_process_imports", {})
    monkeypatch.setattr(HostDependencyPolicy, "_process_paths", set())
    monkeypatch.setattr(sys, "path", sys.path.copy())


def policy(tmp_path: Path, host: tuple[tuple[str, str, str], ...] = ()) -> HostDependencyPolicy:
    manifest_path = tmp_path / "resources" / "plugin_host_dependencies.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "packages": [{"name": name, "version": version, "imports": [root]} for name, version, root in host],
            }
        ),
        encoding="utf-8",
    )
    return HostDependencyPolicy(tmp_path)


def directory(tmp_path: Path, *items: tuple[str, str, str]) -> PluginDependencyDirectory:
    return PluginDependencyDirectory(
        "a" * 64,
        tmp_path / "site-packages",
        tuple(InstalledPackage(name, version, "b" * 64, frozenset({root})) for name, version, root in items),
    )


def test_host_versions_use_pep440_equivalence_and_cannot_be_overwritten(tmp_path: Path) -> None:
    instance = policy(tmp_path, (("example", "1", "example"),))
    assert instance.conflict(directory(tmp_path, ("example", "1.0", "example"))) is None
    assert "宿主" in instance.conflict(directory(tmp_path, ("example", "2", "example")))
    assert "导入冲突" in instance.conflict(directory(tmp_path, ("other", "1", "example")))


@pytest.mark.parametrize("root", ["json", "ECL", "pytauri"])
def test_protected_imports_are_rejected(tmp_path: Path, root: str) -> None:
    assert "受保护" in policy(tmp_path).conflict(directory(tmp_path, ("third-party", "1", root)))


def test_import_conflicts_inside_same_lock_are_rejected(tmp_path: Path) -> None:
    assert "导入冲突" in policy(tmp_path).conflict(directory(tmp_path, ("one", "1", "shared"), ("two", "1", "shared")))


def test_already_imported_unknown_modules_are_not_assumed_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "ecl_unknown_test", SimpleNamespace())
    assert "无法确认" in policy(tmp_path).conflict(directory(tmp_path, ("test", "1", "ecl_unknown_test")))


def test_versions_are_pinned_and_path_registered_once(tmp_path: Path) -> None:
    instance = policy(tmp_path)
    original = sys.path.copy()
    first = directory(tmp_path, ("example", "1", "ecl_example_test"))
    instance.register(first)
    instance.register(first)
    assert sys.path == [*original, str(first.site_path)]
    assert HostDependencyPolicy(tmp_path).conflict(directory(tmp_path, ("example", "2", "ecl_example_test")))
    with pytest.raises(PluginDependencyError, match="重启"):
        instance.register(directory(tmp_path, ("example", "2", "ecl_example_test")))
    assert sys.path == [*original, str(first.site_path)]


def test_startup_conflicts_disable_both_independent_of_order(tmp_path: Path) -> None:
    instance = policy(tmp_path)
    directories = {
        "one": directory(tmp_path, ("example", "1", "ecl_example_test")),
        "two": directory(tmp_path, ("example", "2", "ecl_example_test")),
        "empty": None,
    }
    assert instance.combination_conflicts(directories) == instance.combination_conflicts(
        dict(reversed(list(directories.items())))
    )
    assert set(instance.combination_conflicts(directories)) == {"one", "two"}


def test_build_manifest_includes_production_closure_not_dev_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import ECL.plugins.host_dependencies as module

    monkeypatch.setattr(module.tomllib, "loads", lambda _: {"project": {"dependencies": ["host[extra]>=1"]}})
    metadata = {
        "host": SimpleNamespace(
            version="1", requires=["base>=2", 'optional>=1; extra == "extra"', 'absent; python_version < "3"']
        ),
        "base": SimpleNamespace(version="2", requires=[]),
        "optional": SimpleNamespace(version="1", requires=[]),
    }
    monkeypatch.setattr(module.importlib.metadata, "distribution", metadata.__getitem__)
    monkeypatch.setattr(
        module.importlib.metadata,
        "packages_distributions",
        lambda: {"host": ["host"], "base": ["base"], "pytest": ["pytest"]},
    )
    payload = build_host_dependency_manifest()
    assert {entry["name"] for entry in payload["packages"]} == {"host", "base", "optional"}
    assert payload["packages"][0]["imports"] == ["base"]


def test_frozen_build_requires_embedded_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(PluginDependencyError, match="缺少"):
        HostDependencyPolicy(tmp_path)
