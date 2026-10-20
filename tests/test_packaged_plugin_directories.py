from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import subprocess
from pathlib import Path

import httpx
import psutil
import pytest
from plugin_wheel_helpers import make_package, make_wheel
from websockets.asyncio.client import connect


async def _request(websocket, request_id: int, method: str, params: dict | None = None):
    await websocket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    async with asyncio.timeout(90):
        while True:
            reply = json.loads(await websocket.recv())
            if reply.get("id") == request_id:
                assert reply.get("ok"), reply
                return reply["data"]


async def _connect(data_path: Path, process: subprocess.Popen, log_path: Path):
    async with asyncio.timeout(90):
        while True:
            if process.poll() is not None:
                pytest.fail(log_path.read_text(encoding="utf-8", errors="replace"))
            discovery_path = data_path / "dev_channel.json"
            if discovery_path.is_file():
                payload = json.loads(discovery_path.read_text(encoding="utf-8"))
                try:
                    websocket = await connect(f"ws://127.0.0.1:{payload['port']}")
                except OSError:
                    await asyncio.sleep(0.1)
                    continue
                await websocket.send(json.dumps({"op": "auth", "token": payload["token"]}))
                assert json.loads(await websocket.recv())["op"] == "auth_ok"
                return websocket
            await asyncio.sleep(0.1)


async def _native_channel(port: int):
    async with asyncio.timeout(90), httpx.AsyncClient(trust_env=False) as client:
        while True:
            try:
                pages = (await client.get(f"http://127.0.0.1:{port}/json/list")).json()
                page = next(
                    (item for item in pages if item.get("type") == "page" and "tauri.localhost" in item.get("url", "")),
                    None,
                )
                if page:
                    return await connect(page["webSocketDebuggerUrl"])
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(0.1)


async def _native_invoke(channel, sequence: int, command: str, params: dict):
    expression = (
        "(async()=>{const end=Date.now()+30000;while(typeof window.__TAURI__?.pytauri?.pyInvoke!=='function'){if(Date.now()>end)throw new Error('native bridge not ready');await new Promise(r=>setTimeout(r,25));}return window.__TAURI__.pytauri.pyInvoke("
        + json.dumps(command)
        + ","
        + json.dumps(params)
        + ");})()"
    )
    await channel.send(
        json.dumps(
            {
                "id": sequence,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
            }
        )
    )
    async with asyncio.timeout(60):
        while True:
            reply = json.loads(await channel.recv())
            if reply.get("id") == sequence:
                assert "exceptionDetails" not in reply.get("result", {}), reply
                return reply["result"]["result"]["value"]


@pytest.mark.skipif(
    not os.environ.get("ECL_PLUGIN_SMOKE_EXECUTABLE")
    or platform.system() != "Windows"
    or platform.machine().casefold() not in {"amd64", "x86_64"},
    reason="需要真实 Windows x64 发行物；其他平台第三方运行明确不支持",
)
async def test_packaged_launcher_installs_offline_wheels_and_loads_native_extension(tmp_path: Path) -> None:
    executable_path = Path(os.environ["ECL_PLUGIN_SMOKE_EXECUTABLE"]).resolve()
    wheelhouse_path = Path(os.environ["ECL_PLUGIN_SMOKE_WHEELS"]).resolve()
    native_wheels = tuple(wheelhouse_path.glob("xxhash-3.6.0-*.whl"))
    assert len(native_wheels) == 1 and executable_path.is_file()
    pure_wheel = make_wheel(
        tmp_path,
        name="ecl_smoke_resource",
        members={
            "ecl_smoke_resource/__init__.py": b"",
            "ecl_smoke_resource/value.txt": b"offline-resource",
        },
    )
    code = """from ECL.plugin_api import Plugin as BasePlugin
import os
import importlib.resources
import xxhash

class Plugin(BasePlugin):
    def on_load(self):
        self.define_command('probe', self.probe, output_fields=(
            {'name':'pid','type':'integer'}, {'name':'resource','type':'string'},
            {'name':'native','type':'string'}, {'name':'native_path','type':'string'},
        ))

    def probe(self):
        return {'pid':os.getpid(),
            'resource':importlib.resources.files('ecl_smoke_resource').joinpath('value.txt').read_text(),
            'native':xxhash.xxh64(b'ecl').hexdigest(), 'native_path':xxhash.__file__}
"""
    package_path = make_package(
        tmp_path,
        code=code,
        wheel_paths=(pure_wheel, *native_wheels),
        dependencies=("ecl-smoke-resource==1.0.0", "xxhash==3.6.0"),
        permissions=(
            {"capability": "commands.register", "required": True},
            {"capability": "commands.execute", "required": True},
        ),
    )
    data_path = tmp_path / "data"
    for phase in ("install", "restart"):
        log_path = tmp_path / f"{phase}.log"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            debug_port = listener.getsockname()[1]
        environment = dict(
            os.environ,
            WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug_port}",
            WEBVIEW2_USER_DATA_FOLDER=str(data_path / "webview-profile"),
        )
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                [str(executable_path), "--data-dir", str(data_path), "--dev-channel"],
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            websocket = None
            native = None
            try:
                websocket = await _connect(data_path, process, log_path)
                info = await _request(websocket, 1, "launcher.info")
                if phase == "install":
                    result = await _request(
                        websocket,
                        2,
                        "plugin.install",
                        {"path": str(package_path), "confirm_unverified_source": True, "allow_network": False},
                    )
                    assert result["status"] == "installed", result
                    entries = await _request(websocket, 3, "plugin.list")
                    assert entries[0]["pendingRestart"] and entries[0]["status"] == "pending_restart", entries
                else:
                    native = await _native_channel(debug_port)
                    entries = await _request(websocket, 20, "plugin.list")
                    identity = entries[0]["installationId"]
                    assert entries[0]["reviewRequired"]
                    denied = await _request(
                        websocket,
                        21,
                        "frontend.invoke",
                        {"command": "plugin_permissions_get", "payload": {"installation_id": identity}},
                    )
                    assert denied["result"]["errorCode"] == "PLUGIN_MANAGEMENT_CALLER_DENIED"
                    await _native_invoke(native, 100, "frontend_ready", {})
                    view = await _native_invoke(native, 1, "plugin_permissions_get", {"installation_id": identity})
                    assert view["success"], view
                    view = view["data"]
                    result = await _native_invoke(
                        native,
                        2,
                        "plugin_permissions_update",
                        {
                            "installation_id": identity,
                            "content_digest": view["content_digest"],
                            "expected_revision": view["revision"],
                            "decisions": [
                                {"capability": item["capability"], "decision": "allow"} for item in view["items"]
                            ],
                        },
                    )
                    assert result["success"] and result["data"]["persisted"], result
                    result = await _native_invoke(native, 3, "plugin_enable", {"plugin_name": "demo"})
                    assert result["success"], result
                    result = await _native_invoke(native, 4, "plugin_call_command", {"command": "demo:probe"})
                    assert result["success"], result
                    probe = result["data"]
                    assert probe["pid"] != info["pid"]
                    assert probe["resource"] == "offline-resource" and len(probe["native"]) == 16
                    assert Path(probe["native_path"]).is_relative_to(data_path / "plugin_deps")
                    result = await _native_invoke(native, 5, "plugin_disable", {"plugin_name": "demo"})
                    assert result["success"], result
                    assert not psutil.pid_exists(probe["pid"])
                    assert not (data_path / "plugin_envs").exists()
                    assert not (data_path / "plugin_runtimes").exists()
            finally:
                if native is not None:
                    await native.close()
                if websocket is not None:
                    await websocket.close()
                # 仅停止本用例创建的启动器；不借退出 API 扩展生产命令表。
                descendants = psutil.Process(process.pid).children(recursive=True) if process.poll() is None else []
                for child in reversed(descendants):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        continue
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=20)
                await asyncio.to_thread(psutil.wait_procs, descendants, timeout=10)
                for child in descendants:
                    if child.is_running():
                        child.kill()
        (data_path / "dev_channel.json").unlink(missing_ok=True)
