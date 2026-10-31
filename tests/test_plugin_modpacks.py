from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from time import monotonic

import pytest

from ECL.plugin_api.contracts import ProtocolError
from tests.test_plugin_game_launch import game_launch  # noqa: F401 - pytest fixture
from tests.test_plugin_game_resources import assets  # noqa: F401 - pytest fixture
from tests.test_plugin_host_files import begin, call, chunk, commit, finish

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="整合包插件文件边界当前要求 Windows NTFS")


def upload(broker, manager, name, content):
    target, reference = begin(broker, name, content)
    chunk(broker, reference, content)
    result = finish(broker, manager, commit(broker, reference, content))
    assert result["state"] == "completed", result
    target["root_ref"] = call(broker, "resources.list", {"capability": "plugin.base"})["items"][0]["root_ref"]
    return target


@pytest.fixture
def packs(request):
    state, broker, source = request.getfixturevalue("assets")
    root = state.root / "versions" / "1.21.1"
    root.mkdir()
    (root / "1.21.1.json").write_text(
        json.dumps({"id": "1.21.1", "libraries": [], "mainClass": "net.minecraft.client.main.Main"}), encoding="utf-8"
    )
    (state.root / "versions" / "one" / "one.json").write_text(
        json.dumps({"id": "one", "inheritsFrom": "1.21.1"}), encoding="utf-8"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "modrinth.index.json",
            json.dumps(
                {
                    "formatVersion": 1,
                    "game": "minecraft",
                    "versionId": "1",
                    "name": "Fixture pack",
                    "dependencies": {"minecraft": "1.21.1"},
                    "files": [],
                }
            ),
        )
        archive.writestr("overrides/config/example.json", '{"fixture":true}')
    source = upload(broker, state.manager, "pack.mrpack", buffer.getvalue())
    yield state, broker, source


def prepare(state, broker, source):
    result = finish(broker, state.manager, call(broker, "modpacks.prepare", {"source": source}))
    assert result["state"] == "completed", result
    return result["result"]["preparation_ref"]


def test_real_pack_preparation_and_import_use_owned_tasks_and_hide_paths(packs):
    state, broker, source = packs
    reference = prepare(state, broker, source)
    view = call(broker, "modpacks.get", {"preparation_ref": reference})
    assert view["minecraft_version"] == "1.21.1", view
    assert str(state.root) not in json.dumps(view)
    root = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
    result = finish(
        broker,
        state.manager,
        call(
            broker, "modpacks.install", {"root_ref": root, "preparation_ref": reference, "version_name": "plugin-pack"}
        ),
    )
    assert result["state"] == "completed", result
    installed = state.root / "versions" / "plugin-pack"
    assert (installed / "plugin-pack.json").is_file()
    assert (installed / "config" / "example.json").read_text() == '{"fixture":true}'
    assert "path" not in result["result"]


def test_imported_custom_instance_preserves_minecraft_version_in_exported_pack(packs):
    state, broker, source = packs
    reference = prepare(state, broker, source)
    root = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
    installed = finish(
        broker,
        state.manager,
        call(
            broker, "modpacks.install", {"root_ref": root, "preparation_ref": reference, "version_name": "Custom name"}
        ),
    )
    assert installed["state"] == "completed", installed
    manifest = json.loads((state.root / "versions/Custom name/Custom name.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "Custom name" and manifest["inheritsFrom"] == "1.21.1"
    instance = next(
        item["instance_ref"]
        for item in call(broker, "instances.scan", {"root_ref": root, "force": True})["items"]
        if item["version_id"] == "Custom name"
    )
    preview = finish(broker, state.manager, call(broker, "modpacks.export_preview", {"instance_ref": instance}))[
        "result"
    ]
    files = call(broker, "modpacks.export_files", {"preview_ref": preview["preview_ref"]})["items"]
    selected = [item["fileId"] for item in files if item["path"] == "config/example.json"]
    destination = {"root_ref": source["root_ref"], "relative_path": "custom-export.mrpack"}
    result = finish(
        broker,
        state.manager,
        call(
            broker,
            "modpacks.export",
            {
                "preview_ref": preview["preview_ref"],
                "selected_file_ids": selected,
                "destination": destination,
                "name": "Custom export",
                "version": "1",
            },
        ),
    )
    assert result["state"] == "completed", result
    exported = call(broker, "files.read", {"target": destination, "offset": 0, "count": 32768})
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["data_b64"]))) as archive:
        assert json.loads(archive.read("modrinth.index.json"))["dependencies"]["minecraft"] == "1.21.1"


def test_export_preview_is_stable_when_polled_and_exports_only_to_private_file(packs):
    state, broker, source = packs
    target, _ = broker._resources.game_target(state.ref, "instances.read", monotonic() + 5)
    data = state.game.plugin_resource_path(target, "mods").parent
    (data / "config").mkdir(exist_ok=True)
    (data / "config" / "example.json").write_text("{}", encoding="utf-8")
    receipt = call(broker, "modpacks.export_preview", {"instance_ref": state.ref})
    result = finish(broker, state.manager, receipt)
    assert result["state"] == "completed", result
    preview = result["result"]
    again = call(broker, "operations.get", {"operation_ref": receipt["operation_ref"]})["result"]
    assert again["preview_ref"] == preview["preview_ref"]
    files = call(broker, "modpacks.export_files", {"preview_ref": preview["preview_ref"]})["items"]
    selected = [item["fileId"] for item in files if item["path"] == "config/example.json"]
    assert selected
    destination = {"root_ref": source["root_ref"], "relative_path": "export.mrpack"}
    result = finish(
        broker,
        state.manager,
        call(
            broker,
            "modpacks.export",
            {
                "preview_ref": preview["preview_ref"],
                "selected_file_ids": selected,
                "destination": destination,
                "name": "Export fixture",
                "version": "1",
            },
        ),
    )
    assert result["state"] == "completed", result
    file = call(broker, "files.read", {"target": destination, "offset": 0, "count": 32768})

    with zipfile.ZipFile(io.BytesIO(base64.b64decode(file["data_b64"]))) as archive:
        assert archive.read("overrides/config/example.json") == b"{}"
        assert json.loads(archive.read("modrinth.index.json"))["dependencies"]["minecraft"] == "1.21.1"


def test_foreign_preparation_and_host_path_inputs_are_rejected(packs):
    state, broker, source = packs
    with pytest.raises(ProtocolError):
        call(broker, "modpacks.get", {"preparation_ref": "f" * 32})
    with pytest.raises(ProtocolError):
        call(broker, "modpacks.prepare", {"source": source, "source_path": str(state.root)})


def test_ecl_pack_import_preserves_unowned_preexisting_staging(packs):
    state, broker, _ = packs
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ecl-pack.json", "{}")
        archive.writestr("original.json", json.dumps({"id": "original", "mainClass": "fixture.Main"}))
        archive.writestr("config/fixture.txt", "pack")
    source = upload(broker, state.manager, "legacy.zip", buffer.getvalue())
    old_staging = state.root / "versions/.legacy-instance.ecl-import"
    old_staging.mkdir()
    (old_staging / "user.txt").write_text("preserve", encoding="utf-8")
    preparation = prepare(state, broker, source)
    root = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
    result = finish(
        broker,
        state.manager,
        call(
            broker,
            "modpacks.install",
            {"root_ref": root, "preparation_ref": preparation, "version_name": "legacy-instance"},
        ),
    )
    assert result["state"] == "completed", result
    assert (old_staging / "user.txt").read_text(encoding="utf-8") == "preserve"
    assert (state.root / "versions/legacy-instance/config/fixture.txt").read_text() == "pack"


def test_pack_manifest_download_cannot_bypass_plugin_network_authorization(packs, monkeypatch):
    state, broker, _source = packs
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "modrinth.index.json",
            json.dumps(
                {
                    "formatVersion": 1,
                    "game": "minecraft",
                    "versionId": "1",
                    "name": "Unapproved network",
                    "dependencies": {"minecraft": "1.21.1"},
                    "files": [
                        {
                            "path": "mods/remote.jar",
                            "hashes": {"sha512": "a" * 128},
                            "downloads": ["https://unapproved.invalid/remote.jar"],
                            "fileSize": 1,
                        }
                    ],
                }
            ),
        )
    source = upload(broker, state.manager, "network.mrpack", buffer.getvalue())
    reference = prepare(state, broker, source)
    root = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]

    def shared_downloader(*_args, **_kwargs):
        pytest.fail("Plugin manifest URLs must not use the shared downloader")

    monkeypatch.setattr(state.game, "_downloader_factory", shared_downloader)
    result = finish(
        broker,
        state.manager,
        call(
            broker,
            "modpacks.install",
            {"root_ref": root, "preparation_ref": reference, "version_name": "unapproved-pack"},
        ),
    )
    assert result["state"] == "failed", result
    assert result["error_code"] == "PLUGIN_PERMISSION_DENIED", result
    assert not (state.root / "versions" / "unapproved-pack").exists()


def test_pack_archive_traversal_fails_before_creating_an_instance(packs):
    state, broker, _source = packs
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escaped.txt", "must not be published")
    source = upload(broker, state.manager, "unsafe.zip", buffer.getvalue())
    result = finish(broker, state.manager, call(broker, "modpacks.prepare", {"source": source}))
    assert result["state"] == "failed", result
    assert result["result"] is None
    assert not (state.worker.data_path / "escaped.txt").exists()
