from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from importlib.metadata import version
from pathlib import Path
from threading import Thread, Timer
from time import monotonic, sleep

from anyio.from_thread import start_blocking_portal
from pytauri import AppHandle, Commands, WebviewUrl
from pytauri.ffi import webview_version
from pytauri.ffi.webview import WebviewWindowBuilder
from pytauri_wheel.lib import builder_factory, context_factory

from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "assets/index.html").write_text("<!doctype html><title>HOST_ASSET_CANARY</title>", encoding="utf-8")
    config = {
        "productName": "Custom UI gate",
        "version": "0.1.0",
        "identifier": "cn.euora.customuigate",
        "build": {"frontendDist": "assets"},
        "app": {"windows": []},
    }
    (root / "tauri.conf.json").write_text(json.dumps(config), encoding="utf-8")
    source = root / "code"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples/plugins/worker_custom_ui", source)
    with (source / "index.html").open("a", encoding="utf-8") as stream:
        stream.write("""<script>(async()=>{try{
          let parentDenied=false;try{void parent.document.body;}catch(e){parentDenied=true;}
          let refused=false;try{await ECL.call('accounts.list',{});}catch(e){refused=e.message==='PLUGIN_API_METHOD_NOT_FOUND';}
          const result=await ECL.call('commands.call',{command_ref:'greet',args:{text:'actual native UI'}});
          const current=await ECL.call('settings.get',{keys:['message']});
          await ECL.call('settings.set',{values:{message:JSON.stringify({text:result.value.text,parentDenied,refused})},expected_revision:current.revision});
        }catch(e){document.querySelector('output').textContent=e.message;}})();</script>""")
    metadata = json.loads((source / "plugin.json").read_bytes())
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    executable = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if artifact
        else None
    )
    service = PluginWorkerService(root / "data", executable)
    name = metadata["name"]
    service.register(metadata, source, "directory")

    def decide(decision: str) -> None:
        view = service.permissions(name)
        service.update_permissions(
            name,
            view["content_digest"],
            view["revision"],
            (PermissionDecision(capability="ui.custom.execute", decision=decision),),
            PermissionManagementCaller.main_webview,
        )

    decide("allow")
    service.enable(name)
    broker = service.broker(name)

    def wait(predicate) -> None:
        end = monotonic() + 12
        while monotonic() < end:
            if predicate():
                return
            sleep(0.05)
        raise AssertionError("native custom UI did not reach expected state")

    def setup(handle: AppHandle) -> None:
        main_window = WebviewWindowBuilder.build(
            handle, "main", WebviewUrl.External("about:blank"), visible=False, data_directory=root / "main-profile"
        )
        service.bind_window_app(handle)

        def run() -> None:
            try:
                opened = broker.call_command("open", {}, 20)
                assert opened["ready"] is True
                wait(lambda: broker.settings_view()["values"]["message"].startswith("{"))
                result = json.loads(broker.settings_view()["values"]["message"])
                assert result == {"text": "Hello, actual native UI", "parentDenied": True, "refused": True}, result
                decide("deny")
                wait(broker.custom_ui.is_idle)
                assert service._entry(name).supervisor.is_alive() and main_window.label() == "main"
                decide("allow")
                second = broker.call_command("open", {}, 20)
                assert second["window_ref"] != opened["window_ref"]
                broker.custom_ui.handle("ui.windows.close", {"window_ref": second["window_ref"]})
                wait(broker.custom_ui.is_idle)
                (root / "result.json").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "round_trip": result,
                            "revocation_closed": True,
                            "reopened_and_closed": True,
                            "wheel": version("pytauri-wheel"),
                            "webview2": webview_version(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception as exc:
                traceback.print_exc()
                (root / "result.json").write_text(
                    json.dumps({"ok": False, "error": type(exc).__name__}), encoding="utf-8"
                )
            finally:
                service.close()
                handle.exit(0)

        Thread(target=run, daemon=True).start()
        timer = Timer(45, lambda: handle.exit(2))
        timer.daemon = True
        timer.start()

    with start_blocking_portal("asyncio") as portal:
        app = builder_factory().build(
            context=context_factory(root, tauri_config=config),
            invoke_handler=Commands().generate_handler(portal),
            setup=setup,
        )
        app.run_return()
    service.close()


if __name__ == "__main__":
    main()
