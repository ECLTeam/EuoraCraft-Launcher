from __future__ import annotations

import io
import json
import zipfile

import pytest

from ECL.services.game.base import GameServiceError
from ECL.services.game.mod_metadata import LocalModParser, ModDependencyDiagnostics
from ECL.services.game.mod_versions import ModVersionPredicate
from ECL.services.game.resources import ResourceCoordinator


def _jar(path, document, nested=None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps(document))
        if nested:
            archive.writestr("nested.jar", nested)
    return LocalModParser.parse(path)


def test_disabled_provides_and_nested_container_do_not_satisfy_required_dependency(tmp_path):
    child = io.BytesIO()
    with zipfile.ZipFile(child, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "inner", "version": "2.0", "provides": ["alias"]}))
    consumer = _jar(
        tmp_path / "consumer.jar",
        {"id": "consumer", "version": "1.0", "depends": {"alias": ">=2.0"}, "suggests": {"absent": "*"}},
    )
    disabled = _jar(
        tmp_path / "container.jar.disabled",
        {"id": "container", "version": "1.0", "jars": [{"file": "nested.jar"}]},
        child.getvalue(),
    )
    result = ModDependencyDiagnostics.evaluate((consumer, disabled))
    assert [issue["code"] for issue in result[consumer.filename]] == ["disabled_provider"]
    assert result[consumer.filename][0]["providers"] == [disabled.filename]
    enabled = _jar(
        tmp_path / "container.jar",
        {"id": "container", "version": "1.0", "jars": [{"file": "nested.jar"}]},
        child.getvalue(),
    )
    assert ModDependencyDiagnostics.evaluate((consumer, enabled))[consumer.filename] == []
    assert enabled.declarations[1].embedded_path == "nested.jar"


def test_versions_conflicts_alias_duplicates_and_actual_environment(tmp_path):
    consumer = _jar(
        tmp_path / "consumer.jar",
        {
            "id": "consumer",
            "version": "1",
            "depends": {"api": ">=2", "java": ">=21", "minecraft": "1.21"},
            "breaks": {"bad": "*"},
        },
    )
    provider = _jar(tmp_path / "provider.jar", {"id": "provider", "version": "1", "provides": ["api", "bad"]})
    duplicate = _jar(tmp_path / "duplicate.jar", {"id": "other", "version": "1", "provides": ["api"]})
    result = ModDependencyDiagnostics.evaluate((consumer, provider, duplicate), {"minecraft": "1.20", "java": None})
    assert {issue["code"] for issue in result[consumer.filename]} == {
        "version_mismatch",
        "conflicting_provider",
        "constraint_unknown",
    }
    assert any(issue["code"] == "duplicate_provider" and issue["modId"] == "api" for issue in result[provider.filename])


def test_neoforge_all_mod_entries_dependency_types_and_manifest_version(tmp_path):
    path = tmp_path / "multi.jar"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Implementation-Version: 2.5\n")
        archive.writestr(
            "META-INF/neoforge.mods.toml",
            """[[mods]]
modId = "first"
version = "${file.jarVersion}"
[[mods]]
modId = "second"
version = "1.0"
[[dependencies.first]]
modId = "second"
type = "optional"
versionRange = "[2.0,)"
[[dependencies.second]]
modId = "absent"
mandatory = true
versionRange = "[1.0,)"
""",
        )
    metadata = LocalModParser.parse(path)
    assert [mod.mod_id for mod in metadata.declarations] == ["first", "second"]
    assert metadata.declarations[0].version == "2.5"
    assert {issue["code"] for issue in ModDependencyDiagnostics.evaluate((metadata,))[path.name]} == {
        "version_mismatch",
        "missing_required",
    }


@pytest.mark.parametrize(
    ("version", "constraint", "syntax", "expected"),
    [
        ("1.2.3", ">=1.2 <2", "fabric", True),
        ("2.0-alpha", "^1.2", "fabric", False),
        ("1.2.4", "~1.2.3", "fabric", True),
        ("1.3-alpha", "~1.2", "fabric", False),
        ("1.2+build", "=1.2", "fabric", True),
        ("snapshot", "snapshot", "fabric", True),
        ("1.2", "1.x", "fabric", None),
        ("1.2", ">=1 || <3", "fabric", None),
        (None, "*", "fabric", None),
        ("1.2", "[1.0,2.0)", "maven", True),
        ("2.0", "[1.0,2.0)", "maven", False),
        ("3.0", "(,1.0],[3.0,)", "maven", True),
        ("1.0-rc", "[1.0,)", "maven", None),
        ("1.0", "[1.0-rc,)", "maven", None),
        ("1.0", "[1.0]", "maven", True),
        ("1.0", "1.0", "maven", None),
    ],
)
def test_loader_specific_predicates_keep_unsupported_constraints_unknown(version, constraint, syntax, expected):
    assert ModVersionPredicate.matches(version, (constraint,), syntax) is expected


def test_nested_limits_and_invalid_archive_are_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalModParser, "max_nested_bytes", 0)
    metadata = _jar(
        tmp_path / "limited.jar", {"id": "outer", "version": "1", "jars": [{"file": "nested.jar"}]}, b"not a jar"
    )
    assert metadata.issues == ("invalid_nested_archive",)
    assert len(metadata.declarations) == 1
    path = tmp_path / "broken.jar"
    path.write_bytes(b"broken")
    assert LocalModParser.parse(path).issues == ("invalid_archive",)


def test_source_manifest_tracks_content_across_disabled_suffix():
    service = object.__new__(ResourceCoordinator)
    manifest = {"resources": {"mod:test.jar": {"source": "curseforge", "projectId": "platform-id", "sha512": "same"}}}
    assert service._resource_source(manifest, "mod", "test.jar.disabled", "same")["projectId"] == "platform-id"
    assert service._resource_source(manifest, "mod", "test.jar", "changed") == {}


def test_enabling_resource_does_not_overwrite_an_existing_mod(tmp_path, monkeypatch):
    from types import SimpleNamespace

    service = object.__new__(ResourceCoordinator)
    monkeypatch.setattr(
        ResourceCoordinator, "resolve_instance", lambda *args: SimpleNamespace(game_data_path=tmp_path), raising=False
    )
    monkeypatch.setattr(ResourceCoordinator, "_resource_root", lambda *args: tmp_path)
    (tmp_path / "api.jar").write_bytes(b"enabled")
    (tmp_path / "api.jar.disabled").write_bytes(b"disabled")
    with pytest.raises(GameServiceError, match="目标模组文件已存在"):
        service.toggle_resource(tmp_path, "test", "mod", "api.jar.disabled", True)
    assert (tmp_path / "api.jar").read_bytes() == b"enabled"
    assert (tmp_path / "api.jar.disabled").read_bytes() == b"disabled"


def test_hash_lookup_cache_uses_content_and_does_not_cache_network_failure(monkeypatch):
    import httpx

    from ECL.services.game import resources

    service = object.__new__(ResourceCoordinator)
    monkeypatch.setattr(ResourceCoordinator, "identity_cache", {})
    calls = []

    def query(_url, **kwargs):
        calls.append(kwargs["json"]["hashes"][0])
        return httpx.Response(
            200,
            json={calls[-1]: {"project_id": "actual-project", "id": "actual-version"}},
            request=httpx.Request("POST", "https://api.modrinth.com"),
        )

    monkeypatch.setattr(resources, "_proxied_post", query)
    assert service.identify_resource_hash("a" * 128)["projectId"] == "actual-project"
    service.identify_resource_hash("A" * 128)
    service.identify_resource_hash("b" * 128)
    assert calls == ["a" * 128, "b" * 128]

    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(resources, "_proxied_post", fail)
    assert service.identify_resource_hash("c" * 128)["unavailable"]
    assert ("modrinth", "c" * 128) not in service.identity_cache
