from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from plugin_network_helpers import MappedDialer, server, trusted_ssl_context

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.network_api import PluginNetworkService
from ECL.plugins.worker_service import PluginWorkerService


@pytest.mark.parametrize("is_frozen", [False, True], ids=["source", "frozen"])
def test_real_worker_network_tls_get_head_limits_revoke_and_restart(tmp_path, is_frozen):
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

    def respond(target, headers):
        if target.startswith("/max"):
            etag = b'"' * (4096 - len("etag") - len("content-length") - 5)
            yield b"HTTP/1.1 200 OK\r\nContent-Length: 32768\r\nETag: " + etag + b"\r\n\r\n" + b"x" * 32768
        elif target == "/large":
            yield b"HTTP/1.1 200 OK\r\nContent-Length: 32769\r\n\r\n" + b"x" * 32769
        elif target == "/slow":
            yield b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nfirst"
            entered.set()
            release.wait(4)
        else:
            yield b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"

    with server(respond, is_tls=True) as (port, requests, sni):
        example_path = Path(__file__).resolve().parents[1] / "examples/plugins/worker_network_read"
        code_path = tmp_path / "code"
        code_path.mkdir()
        metadata = json.loads((example_path / "plugin.json").read_bytes())
        references = [f"https:p10.example.test:{port}"]
        metadata["permissions"][-1]["resource_refs"] = references
        code = (
            (example_path / "main.py").read_text(encoding="utf-8")
            + """

class Plugin(BasePlugin):
    def on_load(self):
        self.define_command('read', self.read, input_fields=(
            {'name':'url','type':'string'}, {'name':'method','type':'string'},
        ), output_fields=(
            {'name':'status_code','type':'integer'}, {'name':'body_bytes','type':'integer'},
            {'name':'error_code','type':'string'},
        ))

    def read(self, url, method):
        try:
            return dict(read_metadata(self.host, url, method), error_code='')
        except HostApiError as exc:
            return {'status_code':0,'body_bytes':0,'error_code':exc.error_code}
"""
        )
        (code_path / "main.py").write_text(code, encoding="utf-8")
        (code_path / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
        service = PluginWorkerService(tmp_path / "data", executable)
        # 仅受信测试宿主替换它拥有的服务；插件、RPC、环境均不能注入该夹具。
        assert service._network_service.close()
        service._network_service = PluginNetworkService(
            resolver=lambda *_: ("1.1.1.1",), dialer=MappedDialer(), ssl_context=trusted_ssl_context()
        )

        def review(is_allowed):
            view = service.permissions(metadata["name"])
            return service.update_permissions(
                metadata["name"],
                view["content_digest"],
                view["revision"],
                tuple(
                    PermissionDecision(
                        capability=item["capability"],
                        decision="deny" if item["capability"] == "network.read" and not is_allowed else "allow",
                        resource_refs=tuple(item["requested_resource_refs"])
                        if item["capability"] == "network.read" and is_allowed
                        else (),
                    )
                    for item in view["items"]
                ),
                PermissionManagementCaller.main_webview,
            )

        try:
            service.register(metadata, code_path, "directory")
            denied_view = service.permissions(metadata["name"])
            network = next(item for item in denied_view["items"] if item["capability"] == "network.read")
            assert network["requested_resource_refs"] == references and network["resource_refs"] == []
            review(False)
            service.enable(metadata["name"])
            broker = service.broker(metadata["name"])

            def command(target="/", method="GET"):
                return broker.call_command(
                    "read", {"url": f"https://p10.example.test:{port}{target}", "method": method}, timeout=10
                )

            assert command()["error_code"] == "PLUGIN_PERMISSION_DENIED"
            assert not requests
            review(True)
            assert command() == {"status_code": 200, "body_bytes": 5, "error_code": ""}
            assert command(method="HEAD") == {"status_code": 200, "body_bytes": 0, "error_code": ""}
            prefix = f"https://p10.example.test:{port}/max?pad="
            assert command("/max?pad=" + "x" * (4096 - len(prefix)))["body_bytes"] == 32768
            assert command("/large")["error_code"] == "PLUGIN_RESPONSE_TOO_LARGE"
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(command, "/slow")
                assert entered.wait(2)
                review(False)
                with pytest.raises((ProtocolError, PluginGrantError)):
                    future.result(3)
            assert command()["error_code"] == "PLUGIN_PERMISSION_DENIED"
            assert not broker.is_closed
            release.set()
            service.stop(metadata["name"])
            assert broker.wait_idle(1)
            service.enable(metadata["name"])
            broker = service.broker(metadata["name"])
            review(True)
            assert command()["body_bytes"] == 5
            assert sni and set(sni) == {"p10.example.test"}
        finally:
            release.set()
            service.close()
