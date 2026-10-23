from __future__ import annotations

import json
from time import monotonic

import pytest
from pydantic import ValidationError

from ECL.services.game.instance_profile_models import InstanceProfilePatch
from ECL.services.game.instance_profiles import InstanceProfileStore
from ECL.services.game.version_stats import VersionStatsStore
from ECL.utils.instance_reading import InstanceReadError


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"favorite": 1},
        {"alias": " "},
        {"description": None},
        {"pinned": True},
        {"alias": "x" * 121},
        {"description": "x" * 1001},
        {"tags": ["x" * 41]},
        {"tags": ["x"] * 21},
        {"tags": [False]},
    ],
)
def test_profile_patch_is_strict_and_never_silently_truncates(values):
    with pytest.raises(ValidationError):
        InstanceProfilePatch.model_validate(values)


def test_explicit_false_empty_tags_and_casefold_deduplication():
    assert InstanceProfilePatch(favorite=False, tags=[]).model_dump(exclude_unset=True) == {
        "favorite": False,
        "tags": [],
    }
    assert InstanceProfilePatch(tags=[" One ", "one", "Two"]).tags == ["One", "Two"]


def setup(tmp_path):
    root = tmp_path / "game"
    instance = root / "versions/one"
    instance.mkdir(parents=True)
    return root, instance / ".ecl/instance.json", InstanceProfileStore(tmp_path / "data", VersionStatsStore())


def test_full_version_changes_for_nonpublic_fields_presence_and_same_bytes_recreation(tmp_path):
    root, path, store = setup(tmp_path)
    absent = store.snapshot(root, "one", monotonic() + 5).etag
    assert not path.parent.exists()
    path.parent.mkdir()
    path.write_text('{"alias":"one","pinned":false}', encoding="utf-8")
    first = store.snapshot(root, "one", monotonic() + 5).etag
    path.write_text('{"alias":"one","pinned":true}', encoding="utf-8")
    second = store.snapshot(root, "one", monotonic() + 5).etag
    content = path.read_bytes()
    path.unlink()
    path.write_bytes(content)
    third = store.snapshot(root, "one", monotonic() + 5).etag
    assert len({absent, first, second, third}) == 4


@pytest.mark.parametrize(
    "text",
    [
        '{"alias":',
        "[]",
        '{"schemaVersion":2}',
        '{"schemaVersion":true}',
        '{"alias":"a","alias":"b"}',
        '{"favorite":0}',
        '{"tags":["' + "x" * 41 + '"]}',
        '{"description":"' + "x" * 65536 + '"}',
    ],
    ids=["broken", "array", "schema", "boolean-schema", "duplicate", "boolean", "tag", "oversize"],
)
def test_corrupt_profile_is_preserved_before_desktop_patch_icon_or_reorder(tmp_path, text):
    root, path, store = setup(tmp_path)
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    icon = path.parent / "icon.png"
    icon.write_bytes(b"original icon")
    before = path.read_bytes()
    for action in (
        lambda: store.patch_profile(root, "one", {"alias": "New"}),
        lambda: store.set_icon(root, "one", "auto"),
        lambda: store.reorder_pins([(root, "one")]),
    ):
        with pytest.raises(InstanceReadError):
            action()
        assert path.read_bytes() == before
        assert icon.read_bytes() == b"original icon"


def test_desktop_patch_retains_valid_unknown_fields(tmp_path):
    root, path, store = setup(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({"alias": "Before", "private": {"retained": [1, False]}}), encoding="utf-8")
    store.patch_profile(root, "one", {"favorite": False})
    assert json.loads(path.read_bytes())["private"] == {"retained": [1, False]}


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": True},
        {"expected_etag": "bad"},
        {"recovery_ref": "bad"},
        {"action": "overwrite"},
        {"action": "save_as"},
        {"saved_suffix": None},
        {"action": "save_as", "saved_suffix": "../outside"},
        {"action": "save_as", "saved_suffix": "name.json"},
        {"action": "save_as", "saved_suffix": "x" * 65},
        {"path": "C:/private"},
    ],
)
def test_profile_recovery_rejects_ambiguous_or_path_input(changes):
    from ECL.api.plugin_models import PluginInstanceProfileRecoveryResolve

    request = {"recovery_ref": "a" * 32, "expected_revision": 1, "expected_etag": "b" * 64, "action": "restore"}
    with pytest.raises(ValidationError):
        PluginInstanceProfileRecoveryResolve.model_validate({**request, **changes})


def test_alias_limit_applies_after_trimming_without_type_conversion():
    assert InstanceProfilePatch(alias=" " * 120 + "Alias" + " " * 120).alias == "Alias"
    with pytest.raises(ValidationError):
        InstanceProfilePatch(alias=123)
