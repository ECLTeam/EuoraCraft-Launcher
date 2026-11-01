from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ECL_NATIVE_UI_GATE"), reason="需要固定原生 Wheel 和真实系统 WebView"
)


async def online_control(port_file: Path) -> None:
    """
    只通过原始 CDP 取消调试协议的网络拒绝，验证独立原生代理仍阻止实际出口。
    """
    import websockets

    lines = port_file.read_text().splitlines()
    async with websockets.connect("ws://127.0.0.1:" + lines[0] + lines[1], proxy=None) as connection:
        sequence = 0

        async def request(method, params, session=None):
            nonlocal sequence
            sequence += 1
            message = {"id": sequence, "method": method, "params": params}
            if session:
                message["sessionId"] = session
            await connection.send(json.dumps(message))
            while True:
                result = json.loads(await asyncio.wait_for(connection.recv(), 2))
                if result.get("id") == sequence:
                    assert "error" not in result, result.get("error")
                    return result.get("result", {})

        target = None
        for _ in range(30):
            targets = await request("Target.getTargets", {})
            target = next(
                (item for item in targets["targetInfos"] if item["type"] == "page" and item["url"].startswith("data:")),
                None,
            )
            if target:
                break
            await asyncio.sleep(0.05)
        assert target is not None
        session = (await request("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))[
            "sessionId"
        ]
        await request("Network.enable", {}, session)
        await request("Network.setBlockedURLs", {"urls": []}, session)
        await request(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
            session,
        )


@pytest.mark.parametrize("mode", ["network", "unprotected", *(["profile_override"] if sys.platform == "win32" else [])])
def test_native_isolation_real_sources_and_fail_closed(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    received, udp_received = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"canary")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.1)
    stopped = Event()

    def receive_udp():
        while not stopped.is_set():
            try:
                udp_received.append(udp.recvfrom(2048)[0])
            except TimeoutError:
                continue

    udp_thread = Thread(target=receive_udp, daemon=True)
    udp_thread.start()
    environment = dict(os.environ, PYTHONUTF8="1", _PYTAURI_DIST="pytauri-wheel")
    environment.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    environment.pop("WEBVIEW2_USER_DATA_FOLDER", None)
    runtime = os.environ.get("ECL_NATIVE_RUNTIME_PATH")
    environment["PYTHONPATH"] = os.pathsep.join([str(root), *([runtime] if runtime else [])])
    output = tmp_path / "native"
    if mode == "profile_override":
        environment["WEBVIEW2_USER_DATA_FOLDER"] = str(tmp_path / "global-profile")
    process = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(root / "tests/plugin_native_isolation_gate_host.py"),
                str(output),
                f"http://127.0.0.1:{server.server_port}",
                str(udp.getsockname()[1]),
                mode,
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if mode == "network" and sys.platform == "win32":
            port_file = output / "webview/EBWebView/DevToolsActivePort"
            end = monotonic() + 10
            while not port_file.exists() and monotonic() < end and process.poll() is None:
                sleep(0.03)
            assert port_file.exists()
            asyncio.run(online_control(port_file))
        _, error = process.communicate(timeout=30)
        report = output / "result.json"
        assert report.exists(), error.decode("utf-8", errors="replace")[-2000:]
        data = json.loads(report.read_text(encoding="utf-8"))
        if mode != "network":
            assert data == {"denied": True}, data
            return
        assert process.returncode == 0, error.decode("utf-8", errors="replace")[-2000:]
        _assert_native_frame(data)
        assert received == [] and udp_received == [], {"http": received, "udp_packets": len(udp_received)}
        destination = os.environ.get("ECL_NATIVE_GATE_REPORT")
        if destination:
            Path(destination).write_text(
                json.dumps(
                    {
                        **data,
                        "http": received,
                        "udp_packets": len(udp_received),
                        "cdp_network_online": sys.platform == "win32",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        stopped.set()
        udp_thread.join(2)
        udp.close()
        server.shutdown()
        server.server_close()
        server_thread.join(2)


def _assert_native_frame(data):
    frame = data["frame"]
    assert frame["positive"] and frame["parentAccess"] is False, data
    if sys.platform == "win32":
        assert frame["nativeGlobals"], data
    assert data["ipc"]["positive"] >= 1 and data["ipc"]["bypass"] == 0, data
    assert frame["fetch"] == frame["websocket"] == "blocked", data
    assert frame["nested"] == "ran" and frame["worker"] in {"ran", "blocked"}, data
    assert frame["rtc"] in {"offer-created", "blocked"} and frame["hostAsset"] is False, data
