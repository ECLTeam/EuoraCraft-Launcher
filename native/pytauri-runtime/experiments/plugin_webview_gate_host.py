from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Timer
from uuid import uuid4

from anyio.from_thread import start_blocking_portal
from pytauri import AppHandle, Commands, WebviewUrl
from pytauri.ffi.webview import WebviewWindow, WebviewWindowBuilder
from pytauri_wheel.lib import builder_factory, context_factory


def main() -> int:
    root_path = Path(sys.argv[1]).resolve()
    canary_url = sys.argv[2]
    report_path = root_path / "report.json"
    secret = uuid4().hex
    evidence: dict[str, object] = {"accepted": 0, "denied": 0, "navigation": [], "frame": {}}
    commands = Commands()

    @commands.command()
    async def gate_diag(body: bytes) -> bytes:
        (root_path / "diagnostic.json").write_bytes(body)
        return b"{}"

    @commands.command()
    async def gate_request(body: bytes, webview_window: WebviewWindow) -> bytes:
        payload = json.loads(body)
        allowed = webview_window.label() == "plugin-v2-gate" and payload.get("credential") == secret
        key = "accepted" if allowed else "denied"
        evidence[key] = int(evidence[key]) + 1
        return json.dumps({"allowed": allowed}).encode()

    @commands.command()
    async def gate_report(body: bytes, app_handle: AppHandle, webview_window: WebviewWindow) -> bytes:
        payload = json.loads(body)
        if webview_window.label() != "plugin-v2-gate" or payload.get("credential") != secret:
            return b'{"allowed":false}'
        evidence["frame"] = payload["frame"]
        evidence["navigation"] = navigation
        report_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        Timer(0.3, lambda: app_handle.exit(0)).start()
        return b'{"allowed":true}'

    frame_script = """
const result = {parent_access:false,secret_visible:false,pyinvoke:'missing',direct:'missing'};
try {result.parent_access = !!parent.document;} catch(e) {}
result.secret_visible = typeof window.__gateCredential === 'string';
async function attempt(action) {
  try {return await Promise.race([action(),new Promise(r=>setTimeout(()=>r('timeout'),500))]);}
  catch(e) {return 'rejected';}
}
if(window.__TAURI__?.pytauri) result.pyinvoke = await attempt(async()=>new TextDecoder().decode(await window.__TAURI__.pytauri.pyInvoke('gate_request',{credential:'wrong'})));
if(window.__TAURI_INTERNALS__) result.direct = await attempt(async()=>new TextDecoder().decode(await window.__TAURI_INTERNALS__.invoke('plugin:pytauri|pyfunc',new TextEncoder().encode(JSON.stringify({credential:'wrong'})),{headers:{pyfunc:'gate_request'}})));
parent.postMessage({type:'gate-frame-result',result},'*');
setTimeout(()=>{location.href=CANARY_URL;},50);
""".replace("CANARY_URL", json.dumps(canary_url))
    frame_html = (
        "<!doctype html><meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'; img-src data:; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\">"
        "<script type='module'>" + frame_script + "</script>"
    )
    frame_literal = json.dumps(frame_html).replace("</", "<\\/")
    html = """<!doctype html><title>Plugin isolation gate</title><iframe id='guest' sandbox='allow-scripts'></iframe>
<script>window.__gateErrors=[];window.addEventListener('error',e=>window.__gateErrors.push(e.message));window.addEventListener('unhandledrejection',e=>window.__gateErrors.push(String(e.reason)));</script>
<script type='module'>
const credential = window.__gateCredential;
const positive = JSON.parse(new TextDecoder().decode(await window.__TAURI__.pytauri.pyInvoke('gate_request',{credential})));
const frame = document.querySelector('#guest');
window.addEventListener('message', event=>{
  if(event.source!==frame.contentWindow || event.data?.type!=='gate-frame-result') return;
  const result={...event.data.result,positive:positive.allowed};
  setTimeout(()=>window.__TAURI__.pytauri.pyInvoke('gate_report',{credential,frame:result}),700);
});
frame.srcdoc=FRAME_DOCUMENT;
</script>""".replace("FRAME_DOCUMENT", frame_literal)
    assets_path = root_path / "assets"
    assets_path.mkdir()
    (assets_path / "index.html").write_text(html, encoding="utf-8")
    (assets_path / "blank.html").write_text("<!doctype html>", encoding="utf-8")
    capabilities_path = root_path / "capabilities"
    capabilities_path.mkdir()
    (capabilities_path / "gate.toml").write_text(
        'identifier="gate"\nwindows=["plugin-v2-gate"]\npermissions=["pytauri:allow-pyfunc"]\n',
        encoding="utf-8",
    )
    (root_path / "Tauri.toml").write_text(
        'productName="ECL plugin gate"\nidentifier="org.euoracraft.plugin-gate"\n[app]\nwithGlobalTauri=true\n',
        encoding="utf-8",
    )
    navigation: list[str] = []

    def allow_navigation(url: object) -> bool:
        location = str(url)
        navigation.append(location)
        return not location.startswith(canary_url)

    def setup(handle: AppHandle) -> None:
        window = WebviewWindowBuilder.build(
            handle,
            "plugin-v2-gate",
            WebviewUrl.App("blank.html"),
            visible=False,
            title="ECL isolation gate",
            on_navigation=allow_navigation,
            data_directory=root_path / "webview",
            initialization_script="if(window.top===window && (location.hostname==='tauri.localhost' || location.protocol==='tauri:')){window.__gateCredential="
            + json.dumps(secret)
            + ";}",
            additional_browser_args="--remote-debugging-port=" + os.environ.get("ECL_WEBVIEW_GATE_DEBUG_PORT", "0"),
        )

        def start_after_gate() -> None:
            try:
                (root_path / "started.txt").write_text("starting", encoding="utf-8")
                window.experiment_block_network()
                (root_path / "installed.txt").write_text("installed", encoding="utf-8")
                window.navigate("http://tauri.localhost/index.html")
                (root_path / "navigated.txt").write_text("navigated", encoding="utf-8")
            except Exception as exc:
                (root_path / "diagnostic.json").write_text(str(exc), encoding="utf-8")
                handle.exit(3)

        Timer(0.2, start_after_gate).start()
        Timer(
            2,
            lambda: window.eval(
                "window.__TAURI__?.pytauri?.pyInvoke('gate_diag',{url:location.href,keys:Object.keys(window.__TAURI__||{}),errors:window.__gateErrors,hasCredential:!!window.__gateCredential});"
            ),
        ).start()
        watchdog = Timer(float(os.environ.get("ECL_WEBVIEW_GATE_WATCHDOG", "12")), lambda: handle.exit(2))
        watchdog.daemon = True
        watchdog.start()

    with start_blocking_portal("asyncio") as portal:
        context = context_factory(root_path, tauri_config={"build": {"frontendDist": "assets"}, "app": {"windows": []}})
        app = builder_factory().build(context=context, invoke_handler=commands.generate_handler(portal), setup=setup)
        return app.run_return()


if __name__ == "__main__":
    raise SystemExit(main())
