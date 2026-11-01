from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


@pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("ECL_WEBVIEW_GATE"), reason="需要显式启用真实 PyTauri/WebView2 gate"
)
def test_native_http_filter_blocks_frame_navigation(tmp_path: Path) -> None:
    requests: list[str] = []

    class CanaryHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"canary")

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CanaryHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    code_path = Path(__file__).with_name("plugin_webview_gate_host.py")
    environment = {**os.environ, "PYTHONUTF8": "1"}
    environment.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    canary_url = f"http://127.0.0.1:{server.server_port}/unapproved-navigation"
    try:
        process = subprocess.run(
            [sys.executable, str(code_path), str(tmp_path), canary_url],
            env=environment,
            capture_output=True,
            timeout=25,
            creationflags=0x08000000,
        )
        diagnostic = tmp_path / "diagnostic.json"
        detail = (
            diagnostic.read_text(encoding="utf-8")
            if diagnostic.exists()
            else process.stderr.decode("utf-8", errors="replace")
        )
        assert process.returncode == 0, detail
        assert (tmp_path / "report.json").exists(), detail
        result = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        evidence_path = os.environ.get("ECL_WEBVIEW_GATE_REPORT")
        if evidence_path:
            Path(evidence_path).write_text(
                json.dumps({**result, "canary_requests": requests, "network_gate_passed": not requests}, indent=2),
                encoding="utf-8",
            )
        assert result["frame"]["positive"] is True
        assert result["frame"]["parent_access"] is False
        assert result["frame"]["secret_visible"] is False
        assert result["accepted"] == 1
        # 单独验证 HTTP 导航，不将此结果外推为全部网络出口均被阻断。
        assert requests == []
        assert not any(canary_url in url for url in result["navigation"])
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
