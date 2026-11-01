from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from contextlib import ExitStack
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest

from ECL.foundation import EventBus
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.connector import ConnectorService
from ECL.services.connector_nodes import ConnectorNodeConfig
from ECL.services.operations import OperationManager
from tests.test_plugin_host_files import call


@pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("ECL_CONNECTOR_NETWORK_GATE"),
    reason="显式启用真实 EasyTier 本地中继与两个 Worker 的联机验收",
)
def test_actual_worker_host_join_protocol_and_confirmed_cleanup(tmp_path):
    import easytier_pyo3

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    relay = easytier_pyo3.Node(
        {
            "listeners": [f"tcp://127.0.0.1:{port}"],
            "network_identity": {"network_name": "ecl-test-relay", "network_secret": uuid4().hex},
            "flags": {"no_tun": True, "bind_device": False, "relay_all_peer_rpc": True},
        }
    )
    settings = ConnectorNodeConfig(mode="custom", nodes=[f"tcp://127.0.0.1:{port}"])
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    executable = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if artifact
        else None
    )

    def wait(predicate):
        deadline = monotonic() + 45
        while monotonic() < deadline:
            if predicate():
                return
            sleep(0.1)
        pytest.fail("真实联机会话未在截止时间内达到预期状态")

    with ExitStack() as cleanup:
        relay.start()
        cleanup.callback(relay.stop)

        def worker(label):
            root = tmp_path / label
            source = root / "code"
            shutil.copytree(Path(__file__).resolve().parents[1] / "examples/plugins/worker_connector", source)
            metadata = json.loads((source / "plugin.json").read_bytes())
            connector = ConnectorService(player_name=label, node_settings_provider=lambda: settings)
            cleanup.callback(connector.close)
            tasks = OperationManager(root / "data", EventBus())
            cleanup.callback(tasks.close)
            service = PluginWorkerService(root / "data", executable, operations=tasks, connector=connector)
            cleanup.callback(service.close)
            service.register(metadata, source, "directory")
            view = service.permissions(metadata["name"])
            service.update_permissions(
                metadata["name"],
                view["content_digest"],
                view["revision"],
                (PermissionDecision(capability="connector.manage", decision="allow"),),
                PermissionManagementCaller.main_webview,
            )
            service.enable(metadata["name"])
            return service.broker(metadata["name"]), connector

        host, host_service = worker("host")
        guest, guest_service = worker("guest")
        host_room = json.loads(host.call_command("host", {}, 15)["result"])
        wait(lambda: host_service._mode == "host")
        host_state = json.loads(host.call_command("refresh", {}, 10)["result"])
        assert host_state["mode"] == "host" and host_state["room_code"]
        call(guest, "settings.set", {"values": {"room_code": host_state["room_code"]}, "expected_revision": 0})
        guest_room = json.loads(guest.call_command("join", {}, 15)["result"])
        wait(lambda: guest_service._mode == "guest")
        assert json.loads(guest.call_command("refresh", {}, 10)["result"])["mode"] == "guest"
        assert json.loads(guest.call_command("echo", {}, 15)["result"]) == {"value": {"message": "hello"}}
        for broker, service, room in ((guest, guest_service, guest_room), (host, host_service, host_room)):
            assert json.loads(broker.call_command("leave", {}, 10)["result"])["requested"] is True
            wait(lambda service=service: service._mode == "idle")
            wait(
                lambda broker=broker, room=room: (
                    call(broker, "operations.get", {"operation_ref": room["operation_ref"]})["state"] == "cancelled"
                )
            )
            assert service._easy_tier_node is None and service._client is None and service._room_server is None
