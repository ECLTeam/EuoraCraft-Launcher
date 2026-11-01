from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from threading import Timer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from anyio.from_thread import start_blocking_portal
from pytauri import AppHandle, Commands, WebviewUrl
from pytauri.ffi.webview import WebviewWindowBuilder
from pytauri.ipc import WebviewWindow
from pytauri_wheel.lib import builder_factory, context_factory


def main() -> None:
    root, canary, udp_port = Path(sys.argv[1]).resolve(), sys.argv[2], int(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "network"
    root.mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "assets/index.html").write_text(
        "<!doctype html><title>HOST_ASSET_CANARY</title><script>window.__TAURI_INTERNALS__.invoke('plugin:pytauri|pyfunc',new Uint8Array(),{headers:{pyfunc:'gate_request'}});</script>",
        encoding="utf-8",
    )
    capabilities = root / "capabilities"
    capabilities.mkdir()
    (capabilities / "gate.toml").write_text(
        'identifier="gate"\nwindows=["main","plugin-v2-*"]\npermissions=["pytauri:allow-pyfunc"]\n', encoding="utf-8"
    )
    (root / "Tauri.toml").write_text(
        'productName="ECL native gate"\nidentifier="cn.euora.nativegate"\n[app]\nwithGlobalTauri=true\n',
        encoding="utf-8",
    )
    secret = uuid4().hex
    counters = {"positive": 0, "bypass": 0}
    commands = Commands()

    @commands.command()
    async def gate_request(body: bytes, webview_window: WebviewWindow) -> bytes:
        counters["positive" if webview_window.label() == "main" else "bypass"] += 1
        return b"{}"

    script = """
const result={positive:true,nativeGlobals:!!window.__TAURI_INTERNALS__,parentAccess:false,fetch:'pending',websocket:'pending',worker:'pending',nested:'pending',rtc:'pending',hostAsset:false};
try{result.parentAccess=!!parent.document;}catch(e){}
try{window.__TAURI_INTERNALS__?.invoke('plugin:pytauri|pyfunc',new Uint8Array(),{headers:{pyfunc:'gate_request'}}).catch(()=>{});}catch(e){}
fetch(CANARY+'/fetch').then(()=>result.fetch='succeeded').catch(()=>result.fetch='blocked');
fetch('http://tauri.localhost/index.html').then(r=>r.text()).then(t=>result.hostAsset=t.includes('HOST_ASSET_CANARY')).catch(()=>{});
try{const ws=new WebSocket(CANARY.replace('http:','ws:')+'/ws');ws.onopen=()=>result.websocket='opened';ws.onerror=()=>result.websocket='blocked';}catch(e){result.websocket='blocked';}
try{const worker=new Worker(URL.createObjectURL(new Blob(['postMessage("ran");fetch('+JSON.stringify(CANARY+'/worker')+').catch(()=>{})'],{type:'text/javascript'})));worker.onmessage=()=>result.worker='ran';worker.onerror=()=>result.worker='blocked';}catch(e){result.worker='blocked';}
try{new SharedWorker(URL.createObjectURL(new Blob(['fetch('+JSON.stringify(CANARY+'/shared-worker')+').catch(()=>{})'],{type:'text/javascript'})));}catch(e){}
try{navigator.serviceWorker.register(CANARY+'/sw.js').catch(()=>{});}catch(e){}
const nested=document.createElement('iframe');nested.srcdoc='<script>parent.postMessage("nested-ran","*");fetch('+JSON.stringify(CANARY+'/nested-fetch')+').catch(()=>{});setTimeout(()=>location.href='+JSON.stringify(CANARY+'/nested-nav')+',50)<'+ '/script>';document.body.append(nested);
addEventListener('message',e=>{if(e.source===nested.contentWindow&&e.data==='nested-ran')result.nested='ran';});
const image=new Image();image.src=CANARY+'/image';document.body.append(image);
window.open(CANARY+'/popup','_blank');
try{const rtc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:UDP_PORT'}]});rtc.createDataChannel('test');rtc.createOffer().then(offer=>rtc.setLocalDescription(offer)).then(()=>result.rtc='offer-created').catch(()=>result.rtc='blocked');setTimeout(()=>rtc.close(),1500);}catch(e){result.rtc='blocked';}
const link=document.createElement('a');link.download='unexpected.txt';link.href=URL.createObjectURL(new Blob(['unexpected']));link.click();
setTimeout(()=>parent.postMessage({kind:'result',result},'*'),1800);
""".replace("CANARY", json.dumps(canary)).replace("UDP_PORT", str(udp_port))
    frame = "<!doctype html><body><script>" + script + "</script>"
    shell = """<!doctype html><title>Native gate</title><iframe id="guest" sandbox="allow-scripts"></iframe><script type="module">
const secret=SECRET,guest=document.getElementById('guest');let pending=null;
Object.defineProperty(globalThis,'__eclTakeRequest',{value:()=>{const value=pending;pending=null;return value;}});
Object.defineProperty(globalThis,'__eclStart',{value:()=>{guest.srcdoc=FRAME;}});
addEventListener('message',event=>{if(event.source!==guest.contentWindow||event.data?.kind!=='result')return;pending='ecl-plugin-message://request/'+secret+'?payload='+encodeURIComponent(JSON.stringify(event.data.result));document.title='ecl-plugin-message://request/'+secret+'?id=1';});
</script>""".replace("SECRET", json.dumps(secret)).replace("FRAME", json.dumps(frame).replace("<", "\\u003c"))
    url = "data:text/html;base64," + base64.b64encode(shell.encode()).decode()

    def setup(handle: AppHandle) -> None:
        WebviewWindowBuilder.build(
            handle, "main", WebviewUrl.App("index.html"), visible=False, data_directory=root / "trusted-profile"
        )
        try:
            window = WebviewWindowBuilder.build(
                handle,
                "plugin-v2-" + uuid4().hex,
                WebviewUrl.External("about:blank"),
                plugin_isolation=mode != "unprotected",
                visible=False,
                data_directory=root / "webview",
                additional_browser_args="--remote-debugging-port=0" if sys.platform == "win32" else "",
            )
            if mode == "unprotected":
                window.install_plugin_isolation(url, lambda _: None, lambda _: None)
        except (ValueError, RuntimeError) as exc:
            (root / "result.json").write_text(
                json.dumps({"denied": "PLUGIN_UI_ISOLATION_UNAVAILABLE" in str(exc)}), encoding="utf-8"
            )
            Timer(0.2, lambda: handle.exit(0)).start()
            return

        def message(uri: str) -> None:
            if uri == "ecl-plugin-ready://loaded":
                Timer(3, lambda: window.eval("globalThis.__eclStart()")).start()
                return
            parts = urlsplit(uri)
            if parts.path != "/" + secret:
                return
            result = json.loads(parse_qs(parts.query)["payload"][0])
            (root / "result.json").write_text(
                json.dumps({"frame": result, "ipc": counters}, indent=2), encoding="utf-8"
            )
            Timer(0.3, lambda: handle.exit(0)).start()

        def ready(success: bool) -> None:
            if success:
                window.navigate(url)
            else:
                handle.exit(3)

        window.install_plugin_isolation(url, message, ready)
        timer = Timer(20, lambda: handle.exit(2))
        timer.daemon = True
        timer.start()

    with start_blocking_portal("asyncio") as portal:
        app = builder_factory().build(
            context=context_factory(root, tauri_config={"build": {"frontendDist": "assets"}, "app": {"windows": []}}),
            invoke_handler=commands.generate_handler(portal),
            setup=setup,
        )
        app.run_return()


if __name__ == "__main__":
    main()
