from __future__ import annotations

import subprocess
import sys
from time import monotonic

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.runtime.broker import PluginHostBroker
from tests.test_plugin_game_launch import game_launch  # noqa: F401 - pytest fixture
from tests.test_plugin_host_files import begin, call, chunk, commit, finish

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="当前受控资源文件修改要求 Windows")


@pytest.fixture
def assets(request):
    state = request.getfixturevalue("game_launch")
    broker = PluginHostBroker(
        state.entry.session,
        state.worker.data_path,
        lambda *_: {},
        run_state=lambda: "enabled",
        operations=state.manager,
        resources=state.broker._resources,
        game_operations=state.game,
        file_storage=state.worker._file_storage,
        network_service=state.worker._network_service,
    )
    _target, upload = begin(broker, "source.jar", b"resource fixture")
    chunk(broker, upload, b"resource fixture")
    assert finish(broker, state.manager, commit(broker, upload, b"resource fixture"))["state"] == "completed"
    private = call(broker, "resources.list", {"capability": "plugin.base"})["items"][0]["root_ref"]
    try:
        yield state, broker, {"root_ref": private, "relative_path": "source.jar"}
    finally:
        broker.close()
        assert broker.wait_idle(5)


@pytest.mark.parametrize("kind,suffix", [("mods", ".jar"), ("resourcepacks", ".zip"), ("shaders", ".zip")])
@pytest.mark.parametrize("isolated", [True, False])
def test_private_source_installs_in_actual_instance_resource_location_and_can_be_deleted(
    assets, monkeypatch, kind, suffix, isolated
):
    state, broker, source = assets
    monkeypatch.setattr(state.game, "resolve_version_isolation", lambda *_: isolated)
    method = kind + ".list"
    assert call(broker, method, {"instance_ref": state.ref})["items"] == []
    filename = "example" + suffix
    receipt = call(broker, kind + ".install", {"instance_ref": state.ref, "source": source, "filename": filename})
    result = finish(broker, state.manager, receipt)
    assert result["state"] == "completed", result
    base = state.root / "versions" / ("one" if isolated else "")
    destination = base / ({"mods": "mods", "resourcepacks": "resourcepacks", "shaders": "shaderpacks"}[kind]) / filename
    assert destination.read_bytes() == b"resource fixture"
    listed = call(broker, method, {"instance_ref": state.ref})["items"]
    assert len(listed) == 1 and listed[0]["filename"] == filename
    deleted = finish(broker, state.manager, call(broker, kind + ".delete", {"resource_ref": listed[0]["resource_ref"]}))
    assert deleted["state"] == "completed", deleted
    assert not destination.exists()


def test_mod_enable_is_explicit_and_repeating_disable_does_not_enable_it(assets):
    state, broker, source = assets
    receipt = call(broker, "mods.install", {"instance_ref": state.ref, "source": source, "filename": "example.jar"})
    assert finish(broker, state.manager, receipt)["state"] == "completed"
    item = call(broker, "mods.list", {"instance_ref": state.ref})["items"][0]
    receipt = call(broker, "mods.set_enabled", {"resource_ref": item["resource_ref"], "enabled": False})
    assert finish(broker, state.manager, receipt)["result"]["enabled"] is False
    disabled = call(broker, "mods.list", {"instance_ref": state.ref})["items"][0]
    receipt = call(broker, "mods.set_enabled", {"resource_ref": disabled["resource_ref"], "enabled": False})
    assert finish(broker, state.manager, receipt)["result"]["enabled"] is False
    assert call(broker, "mods.list", {"instance_ref": state.ref})["items"][0]["enabled"] is False


def test_existing_target_is_preserved_and_foreign_reference_is_rejected(assets):
    state, broker, source = assets
    request = {"instance_ref": state.ref, "source": source, "filename": "example.jar"}
    assert finish(broker, state.manager, call(broker, "mods.install", request))["state"] == "completed"
    result = finish(broker, state.manager, call(broker, "mods.install", request))
    assert result["error_code"] == "PLUGIN_FILE_ALREADY_EXISTS"
    with pytest.raises(ProtocolError):
        call(broker, "mods.delete", {"resource_ref": "f" * 32})
    with pytest.raises(ProtocolError):
        call(broker, "mods.install", {**request, "filename": "../escape.jar"})


def test_repeated_install_mutation_returns_same_receipt_without_second_write(assets):
    state, broker, source = assets
    params = {"instance_ref": state.ref, "source": source, "filename": "once.jar"}
    receipt = call(broker, "mods.install", params, key="same-install")
    assert finish(broker, state.manager, receipt)["state"] == "completed"
    assert call(broker, "mods.install", params, key="same-install") == receipt
    assert len(call(broker, "mods.list", {"instance_ref": state.ref})["items"]) == 1


def test_stale_resource_reference_cannot_delete_replacement(assets):
    state, broker, source = assets
    receipt = call(broker, "mods.install", {"instance_ref": state.ref, "source": source, "filename": "changed.jar"})
    assert finish(broker, state.manager, receipt)["state"] == "completed"
    item = call(broker, "mods.list", {"instance_ref": state.ref})["items"][0]
    target, _ = state.broker._resources.game_target(state.ref, "instances.manage", monotonic() + 5)
    destination = state.game.plugin_resource_path(target, "mods") / "changed.jar"
    destination.write_bytes(b"new user contents")
    result = finish(broker, state.manager, call(broker, "mods.delete", {"resource_ref": item["resource_ref"]}))
    assert result["state"] == "failed"
    assert destination.read_bytes() == b"new user contents"


def test_resource_directory_junction_cannot_read_or_publish_outside_instance(assets):
    state, broker, source = assets
    target, _ = state.broker._resources.game_target(state.ref, "instances.manage", monotonic() + 5)
    directory = state.game.plugin_resource_path(target, "mods")
    outside = state.root.parent / "outside-resources"
    outside.mkdir()
    (outside / "protected.jar").write_bytes(b"preserve")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(directory), str(outside)], check=True, capture_output=True)
    with pytest.raises(ProtocolError):
        call(broker, "mods.list", {"instance_ref": state.ref})
    result = finish(
        broker,
        state.manager,
        call(broker, "mods.install", {"instance_ref": state.ref, "source": source, "filename": "escape.jar"}),
    )
    assert result["state"] == "failed", result
    assert sorted(item.name for item in outside.iterdir()) == ["protected.jar"]
    assert (outside / "protected.jar").read_bytes() == b"preserve"
