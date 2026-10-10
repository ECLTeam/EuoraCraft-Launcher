from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import JsonValue

from ECL.game import InstanceMetadata
from ECL.services.game.scan import ScanCoordinator, _JavaInstallation


def _version(root: Path, name: str, **fields: JsonValue) -> Path:
    directory = root / "versions" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps({"id": name, "mainClass": "net.minecraft.client.main.Main", **fields}), encoding="utf-8"
    )
    return directory


@pytest.mark.parametrize(
    "problem", ["missing_json", "invalid_json", "missing_parent", "inheritance_cycle", "invalid_parent", "missing_jar"]
)
def test_metadata_read_is_bounded_and_keeps_files(tmp_path: Path, problem: str) -> None:
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
    elif problem == "invalid_parent":
        _version(tmp_path, "child", inheritsFrom="../outside")
    before = json_path.read_bytes() if json_path.exists() else None
    result = InstanceMetadata.read(tmp_path, "child")
    expected_count = 0 if problem in {"missing_json", "invalid_json"} else 2 if problem == "inheritance_cycle" else 1
    assert len(result.documents) == expected_count
    assert directory.is_dir()
    assert (json_path.read_bytes() if json_path.exists() else None) == before


def test_inherited_java_requirement_and_mixed_components(tmp_path: Path) -> None:
    parent = _version(tmp_path, "1.21", javaVersion={"majorVersion": 21})
    (parent / "1.21.jar").write_bytes(b"client")
    _version(
        tmp_path,
        "child",
        inheritsFrom="1.21",
        libraries=[{"name": "net.minecraftforge:forge:1.21-51.0"}, {"name": "optifine:OptiFine:1.21_HD_U"}],
    )
    result = ScanCoordinator._normalize_scanned_version(tmp_path, "child", {"LoaderType": "Forge"})
    assert "health" not in result and "isBroken" not in result
    assert result["hasForge"] and result["hasOptiFine"]
    assert result["requiredJava"] == 21
    assert result["loaderVersion"] == "1.21-51.0"
    assert InstanceMetadata.read(tmp_path, "child").mod_environment()["minecraft"] == "1.21"


def test_missing_jar_metadata_preserves_download_declaration(tmp_path: Path) -> None:
    _version(tmp_path, "1.21", downloads={"client": {"url": "https://example.invalid/client.jar"}})
    result = InstanceMetadata.read(tmp_path, "1.21")
    assert result.documents[0]["downloads"] == {"client": {"url": "https://example.invalid/client.jar"}}


def test_snapshot_without_traditional_jar_has_no_launch_precheck(tmp_path: Path) -> None:
    _version(tmp_path, "26.4-snapshot-3")
    result = ScanCoordinator._normalize_scanned_version(tmp_path, "26.4-snapshot-3", {})
    assert result["versionId"] == "26.4-snapshot-3"
    assert "health" not in result
    assert "isBroken" not in result


def _scan_service() -> ScanCoordinator:
    service = object.__new__(ScanCoordinator)
    service.logger = logging.getLogger(__name__)
    service._search_factory = lambda _path: SimpleNamespace(search_minecraft=lambda: {})
    service._version_stats = SimpleNamespace(ensure=lambda *args: None)
    service._instance_profiles = SimpleNamespace(enrich_version=lambda _root, version, **kwargs: version)
    return service


def test_scan_retains_directory_omitted_by_core(tmp_path: Path) -> None:
    _version(tmp_path, "broken")
    result = _scan_service()._scan_game_path(tmp_path)
    assert len(result) == 1
    assert result[0]["versionId"] == "broken"
    assert "isBroken" not in result[0]


def test_scan_skips_directories_without_instance_marker(tmp_path: Path) -> None:
    _version(tmp_path, "1.20.1")
    for junk in ("logs", "mods", "crash-reports"):
        (tmp_path / "versions" / junk).mkdir(parents=True, exist_ok=True)
    result = _scan_service()._scan_game_path(tmp_path)
    assert [item["versionId"] for item in result] == ["1.20.1"]


def test_scan_skips_empty_version_json(tmp_path: Path) -> None:
    _version(tmp_path, "1.20.1")
    downloads = tmp_path / "versions" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / "downloads.json").write_bytes(b"")
    result = _scan_service()._scan_game_path(tmp_path)
    assert [item["versionId"] for item in result] == ["1.20.1"]


def test_scan_retains_corrupt_version_json(tmp_path: Path) -> None:
    directory = tmp_path / "versions" / "broken"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.json").write_text("broken", encoding="utf-8")
    result = _scan_service()._scan_game_path(tmp_path)
    assert len(result) == 1
    assert result[0]["versionId"] == "broken"
    assert "health" not in result[0] and "isBroken" not in result[0]


def test_scan_retains_jar_only_directory(tmp_path: Path) -> None:
    directory = tmp_path / "versions" / "jar-only"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "jar-only.jar").write_bytes(b"client")
    result = _scan_service()._scan_game_path(tmp_path)
    assert len(result) == 1
    assert result[0]["versionId"] == "jar-only"
    assert "health" not in result[0] and "isBroken" not in result[0]


def test_java_vendor_kind_and_architecture_are_independent() -> None:
    runtime = _JavaInstallation("/jdk/bin/java", "21.0.7", 21, "Microsoft", "JDK", "arm64", ("system",))
    result = runtime.to_protocol()
    assert result["vendor"] == "Microsoft" and result["runtime_kind"] == "JDK"
    assert result["architecture"] == "arm64" and result["major_version"] == 21
    assert result["path"] == result["executable_path"]
    assert result["java_type"] == "Microsoft"


def test_metadata_read_limits_inheritance_depth(tmp_path: Path) -> None:
    for index in range(66):
        _version(tmp_path, str(index), inheritsFrom=str(index + 1))
    result = InstanceMetadata.read(tmp_path, "0")
    assert len(result.documents) == 64


@pytest.mark.parametrize("version_id", ["../outside", "..", "a/b", "a\\b", "a:b"])
def test_metadata_read_stays_within_version_names(tmp_path: Path, version_id: str) -> None:
    assert InstanceMetadata.read(tmp_path, version_id).documents == ()
