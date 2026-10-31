from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.utils.config import default_config
from tests.test_plugin_game_launch import game_launch  # noqa: F401 - pytest fixture
from tests.test_plugin_game_resources import assets  # noqa: F401 - pytest fixture
from tests.test_plugin_host_files import call
from tests.test_plugin_modpacks import packs, upload  # noqa: F401 - pytest fixture


@pytest.mark.skipif(sys.platform != "win32", reason="真实 Worker 当前要求 Windows x64")
def test_actual_worker_resources_pack_import_export_public_query_and_ownership(request):
    state, source_broker, source = request.getfixturevalue("packs")
    example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_game_tools"
    metadata = json.loads((example / "plugin.json").read_bytes())
    worker = state.worker
    worker._public_config = lambda: default_config
    worker.register(metadata, example, "directory")
    view = worker.permissions(metadata["name"])
    page = worker.permission_resources(metadata["name"], view["content_digest"], None, "", None, 50, "instances.manage")
    worker.update_permissions(
        metadata["name"],
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="instances.manage", decision="allow", resource_refs=(state.root_id,)),),
        PermissionManagementCaller.main_webview,
        page["catalog_revision"],
    )
    worker.enable(metadata["name"])
    broker = worker.broker(metadata["name"])

    def command(method, params=None, key=None):
        return json.loads(
            broker.call_command(
                "request",
                {"method": method, "params_json": json.dumps(params or {}), "mutation_key": key or uuid4().hex},
                timeout=30,
            )["json"]
        )

    def completed(receipt):
        deadline = monotonic() + 15
        while True:
            value = command("operations.get", {"operation_ref": receipt["operation_ref"]})
            if value["state"] in {"completed", "cancelled", "failed"} and not value["cleanup_pending"]:
                assert value["state"] == "completed", value
                return value["result"]
            assert monotonic() < deadline
            sleep(0.02)

    try:
        root = command("resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
        instance = next(
            item["instance_ref"]
            for item in command("instances.scan", {"root_ref": root})["items"]
            if item["version_id"] == "one"
        )
        private = upload(broker, state.manager, "fixture.jar", b"fixture")
        receipt = command(
            "mods.install", {"instance_ref": instance, "source": private, "filename": "fixture.jar"}, "resource-once"
        )
        completed(receipt)
        assert (
            command(
                "mods.install",
                {"instance_ref": instance, "source": private, "filename": "fixture.jar"},
                "resource-once",
            )
            == receipt
        )
        item = command("mods.list", {"instance_ref": instance})["items"][0]
        completed(command("mods.set_enabled", {"resource_ref": item["resource_ref"], "enabled": False}))
        disabled = command("mods.list", {"instance_ref": instance})["items"][0]
        assert disabled["enabled"] is False
        completed(command("mods.delete", {"resource_ref": disabled["resource_ref"]}))
        assert not command("mods.list", {"instance_ref": instance})["items"]
        archive = call(source_broker, "files.read", {"target": source, "offset": 0, "count": 32768})
        pack = upload(broker, state.manager, "fixture.mrpack", base64.b64decode(archive["data_b64"]))
        preparation = completed(command("modpacks.prepare", {"source": pack}))["preparation_ref"]
        preview = command("modpacks.get", {"preparation_ref": preparation})
        assert preview["name"] == "Fixture pack" and preview["minecraft_version"] == "1.21.1"
        completed(
            command(
                "modpacks.install", {"root_ref": root, "preparation_ref": preparation, "version_name": "worker-pack"}
            )
        )
        imported = next(
            item["instance_ref"]
            for item in command("instances.scan", {"root_ref": root, "force": True})["items"]
            if item["version_id"] == "worker-pack"
        )
        exported = completed(command("modpacks.export_preview", {"instance_ref": imported}))
        files = command("modpacks.export_files", {"preview_ref": exported["preview_ref"]})["items"]
        destination = {"root_ref": pack["root_ref"], "relative_path": "export.mrpack"}
        completed(
            command(
                "modpacks.export",
                {
                    "preview_ref": exported["preview_ref"],
                    "selected_file_ids": [item["fileId"] for item in files if item["path"] == "config/example.json"],
                    "destination": destination,
                    "name": "Worker export",
                    "version": "1",
                },
            )
        )
        assert command("files.stat", {"target": destination})["size"] > 0
        assert command("config.public")["default_memory_mb"] == 4096
        with pytest.raises(ProtocolError) as rejected:
            broker.call_command(
                "request", {"method": "accounts.list", "params_json": "{}", "mutation_key": uuid4().hex}
            )
        assert rejected.value.error_code == "PLUGIN_COMMAND_FAILED"
    finally:
        worker.stop(metadata["name"])
