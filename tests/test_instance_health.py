from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from ECL.services.game.instance_health import InstanceInspection
from ECL.services.game.scan import ScanCoordinator, _JavaInstallation


def _version(root, name, **fields):
    directory = root / "versions" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps({"id": name, "mainClass": "net.minecraft.client.main.Main", **fields}), encoding="utf-8"
    )
    return directory


@pytest.mark.parametrize(
    "problem", ["missing_json", "invalid_json", "missing_parent", "inheritance_cycle", "missing_jar"]
)
def test_health_reports_specific_blocker_and_keeps_files(tmp_path, problem):
    directory = _version(tmp_path, "child")
    json_path = directory / "child.json"
    if problem == "missing_json":
        json_path.unlink()
    elif problem == "invalid_json":
        json_path.write_text("broken", encoding="utf-8")
    elif problem == "missing_parent":
        _version(tmp_path, "child", inheritsFrom="missing")
    elif problem == "inheritance_cycle":
        _version(tmp_path, "child", inheritsFrom="parent")
        _version(tmp_path, "parent", inheritsFrom="child")
    before = json_path.read_bytes() if json_path.exists() else None
    result = InstanceInspection.inspect(tmp_path, "child")
    assert problem in {issue.code for issue in result.diagnostics}
    assert not result.to_health()["canLaunch"]
    assert directory.is_dir()
    assert (json_path.read_bytes() if json_path.exists() else None) == before


def test_valid_inherited_jar_and_mixed_components(tmp_path):
    parent = _version(tmp_path, "1.21", javaVersion={"majorVersion": 21})
    (parent / "1.21.jar").write_bytes(b"client")
    _version(
        tmp_path,
        "child",
        inheritsFrom="1.21",
        libraries=[{"name": "net.minecraftforge:forge:1.21-51.0"}, {"name": "optifine:OptiFine:1.21_HD_U"}],
    )
    result = ScanCoordinator._normalize_scanned_version(tmp_path, "child", {"LoaderType": "Forge"})
    assert not result["isBroken"]
    assert result["hasForge"] and result["hasOptiFine"]
    assert result["requiredJava"] == 21
    assert result["loaderVersion"] == "1.21-51.0"
    assert InstanceInspection.inspect(tmp_path, "child").mod_environment()["minecraft"] == "1.21"


def test_downloadable_jar_missing_is_recoverable_warning(tmp_path):
    _version(tmp_path, "1.21", downloads={"client": {"url": "https://example.invalid/client.jar"}})
    result = InstanceInspection.inspect(tmp_path, "1.21").to_health()
    assert result["canLaunch"] and result["status"] == "warning"


def test_scan_retains_directory_omitted_by_core(tmp_path):
    _version(tmp_path, "broken")
    service = object.__new__(ScanCoordinator)
    service.logger = logging.getLogger(__name__)
    service._search_factory = lambda _path: SimpleNamespace(search_minecraft=lambda: {})
    service._version_stats = SimpleNamespace(ensure=lambda *args: None)
    service._instance_profiles = SimpleNamespace(enrich_version=lambda _root, version, **kwargs: version)
    result = service._scan_game_path(tmp_path)
    assert len(result) == 1
    assert result[0]["versionId"] == "broken" and result[0]["isBroken"]


def test_java_vendor_kind_and_architecture_are_independent():
    runtime = _JavaInstallation("/jdk/bin/java", "21.0.7", 21, "Microsoft", "JDK", "arm64", ("system",))
    result = runtime.to_protocol()
    assert result["vendor"] == "Microsoft" and result["runtime_kind"] == "JDK"
    assert result["architecture"] == "arm64" and result["major_version"] == 21
    assert result["path"] == result["executable_path"]
    assert result["java_type"] == "Microsoft"
