from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest


def make_plugin(document: list[dict]):
    source = Path(__file__).resolve().parents[1] / "external_plugins/qomicex_compatible/main.py"
    spec = importlib.util.spec_from_file_location("qomicex_test_plugin", source)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(source.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)

    class Host:
        def call(self, method, params):
            if method == "compat.read_index":
                return {
                    "revision": "a" * 64,
                    "content": base64.b64encode(json.dumps(document).encode()).decode(),
                    "next_offset": None,
                }
            return {"command_ref": "b" * 32}

    plugin = module.Plugin(Host())
    plugin.on_load()
    return plugin


def test_external_qomicex_preserves_metadata_without_online_plugin(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    plugin = make_plugin(
        [
            {
                "gameDir": str(instance),
                "modpackSummary": "说明",
                "isHidden": True,
                "isDefault": False,
                "icon": "fabric",
                "playTime": 12,
                "lastPlayed": "2026-11-07",
            }
        ]
    )
    result = plugin.read_instance(
        {"instance_path": str(instance), "version_id": "instance", "vanilla_name": "1.21", "primary_loader": "fabric"}
    )
    assert result["fields"]["description"] == "说明"
    assert result["fields"]["hidden"] is True
    assert result["stats"]["totalRunDurationSeconds"] == 720
    assert result["fields"]["icon"]["value"] == "fabric"


def test_qomicex_extension_rejects_old_run_and_foreign_player() -> None:
    plugin = make_plugin([])
    description = plugin.extension({"event": "describe"})["result"]
    payload = {
        "binding": description["binding"],
        "session": "b" * 32,
        "event": "protocol",
        "protocol": "qml:player_leave",
        "peer_machine_id": "actual",
        "body": {"machineId": "victim"},
    }
    with pytest.raises(ValueError, match="连接不符"):
        plugin.extension(payload)
    payload["body"] = {"machineId": "actual"}
    assert plugin.extension(payload)["result"]["remove_player"] == "actual"
    with pytest.raises(ValueError, match="运行身份已失效"):
        plugin.extension({**payload, "binding": "old"})
