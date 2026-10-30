from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from plugin_network_helpers import MappedDialer, server, trusted_ssl_context

from ECL.foundation import EventBus
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.network_api import PluginNetworkService
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.operations import OperationManager


@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_real_worker_download_publish_cancel_revoke_and_stop(tmp_path, is_frozen):
    if sys.platform != "win32":
        pytest.skip("第三方 Worker 仅支持 Windows x64")
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if is_frozen and not artifact:
        pytest.skip("需要本轮真实冻结发行物")
    executable = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if is_frozen
        else None
    )
    entered, release = Event(), Event()
    body = b"download payload" * 5000

    def respond(target, headers):
        if target == "/slow":
            yield b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nfirst"
            entered.set()
            release.wait(5)
        else:
            yield f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body

    with server(respond, is_tls=True) as (port, requests, sni):
        example = Path(__file__).resolve().parents[1] / "examples/plugins/worker_download"
        code = tmp_path / "code"
        code.mkdir()
        metadata = json.loads((example / "plugin.json").read_bytes())
        metadata["permissions"][-1]["resource_refs"] = [f"https:p10.example.test:{port}"]
        (code / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
        (code / "main.py").write_text((example / "main.py").read_text(encoding="utf-8"), encoding="utf-8")
        manager = OperationManager(tmp_path / "data", EventBus())
        service = PluginWorkerService(tmp_path / "data", executable, operations=manager)
        assert service._network_service.close()
        service._network_service = PluginNetworkService(
            resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer(), ssl_context=trusted_ssl_context()
        )

        def review(deny=None):
            view = service.permissions(metadata["name"])
            service.update_permissions(
                metadata["name"],
                view["content_digest"],
                view["revision"],
                tuple(
                    PermissionDecision(
                        capability=i["capability"],
                        decision="deny" if i["capability"] == deny else "allow",
                        resource_refs=tuple(i["requested_resource_refs"])
                        if i["capability"] == "network.read" and deny != "network.read"
                        else (),
                    )
                    for i in view["items"]
                ),
                PermissionManagementCaller.main_webview,
            )

        def start(name, route="/"):
            return broker.call_command(
                "submit", {"url": f"https://p10.example.test:{port}{route}", "name": name, "mutation_key": uuid4().hex}
            )

        def wait(receipt):
            deadline = monotonic() + 15
            while True:
                view = broker.call_command("status", {"operation_ref": receipt["operation_ref"]})
                if view["state"] in {"completed", "failed", "cancelled"}:
                    return view
                assert monotonic() < deadline
                sleep(0.02)

        try:
            service.register(metadata, code, "directory")
            review()
            service.enable(metadata["name"])
            broker = service.broker(metadata["name"])
            assert not requests
            receipt = broker.call_command(
                "submit",
                {
                    "url": f"https://p10.example.test:{port}/",
                    "name": "first.bin",
                    "mutation_key": "first-download",
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
            )
            view = wait(receipt)
            assert view["state"] == "completed" and view["effect"] == "committed" and view["size"] == len(body)
            installation = broker.session.view().installation_id
            files = tmp_path / "data/plugin_data" / installation / "files"
            assert (files / "first.bin").read_bytes() == body
            slow = start("cancel.bin", "/slow")
            assert entered.wait(2)
            assert broker.call_command("cancel", {"operation_ref": slow["operation_ref"]})["requested"]
            # 不手工调用 network.refresh：由生产监管循环唤醒真实阻塞读取。
            assert wait(slow)["state"] == "cancelled"
            assert not (files / "cancel.bin").exists()
            release.set()
            entered.clear()
            release.clear()
            slow = start("revoked.bin", "/slow")
            assert entered.wait(2)
            review("plugin.base")
            assert wait(slow)["state"] in {"failed", "cancelled"}
            assert not (files / "revoked.bin").exists()
            release.set()
            review()
            entered.clear()
            release.clear()
            slow = start("stop.bin", "/slow")
            assert entered.wait(2)
            service.stop(metadata["name"])
            assert broker.wait_idle(2)
            assert not (files / "stop.bin").exists()
            service.enable(metadata["name"])
            broker = service.broker(metadata["name"])
            assert wait(start("after-restart.bin"))["state"] == "completed"
            assert (files / "first.bin").read_bytes() == body
            assert sni and set(sni) == {"p10.example.test"}
            # 新 run 不能控制旧任务引用。
            with pytest.raises(ProtocolError) as exc:
                broker.handle(
                    RpcRequest("operations.get", {"operation_ref": receipt["operation_ref"]}, monotonic() + 5)
                )
            assert exc.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"
        finally:
            release.set()
            service.close()
            manager.close()
