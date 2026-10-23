from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import psutil
import pytest
from plugin_network_ui_helpers import check_network_permission_layout
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


async def _native_channel(port: int, process: subprocess.Popen, log_path: Path):
    try:
        async with asyncio.timeout(90), httpx.AsyncClient(trust_env=False) as client:
            while True:
                if process.poll() is not None:
                    pytest.fail(log_path.read_text(encoding="utf-8", errors="replace"))
                try:
                    pages = (await client.get(f"http://127.0.0.1:{port}/json/list")).json()
                    page = next(
                        (
                            item
                            for item in pages
                            if item.get("type") == "page" and "tauri.localhost" in item.get("url", "")
                        ),
                        None,
                    )
                    if page:
                        return await connect(page["webSocketDebuggerUrl"])
                except (httpx.HTTPError, OSError):
                    pass
                await asyncio.sleep(0.1)
    except TimeoutError:
        pytest.fail("等待原生页面调试通道超时：\n" + log_path.read_text(encoding="utf-8", errors="replace"))


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


async def _wait_cleanup(native, identity: str) -> None:
    async with asyncio.timeout(15):
        while True:
            runtime = await _native_invoke(native, 107, "plugin_runtime_info", {"installation_id": identity})
            assert runtime["success"], runtime
            if runtime["data"]["cleanup_state"] == "completed":
                return
            assert runtime["data"]["cleanup_state"] == "pending", runtime
            await asyncio.sleep(0.1)


async def _check_user_task_cancel(native, data_path: Path) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    connections: set[asyncio.Task] = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            started.set()
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    target_path = data_path / "cancelled-download.bin"
    try:
        response = await _native_invoke(
            native,
            110,
            "custom_download_start",
            {
                "url": f"http://127.0.0.1:{port}/fixture.bin",
                "target_path": str(target_path),
            },
        )
        assert response["success"], response
        operation_id = response["data"]["operationId"]
        async with asyncio.timeout(15):
            await started.wait()
        cancelled = await _native_invoke(native, 111, "game_operation_cancel", {"operation_id": operation_id})
        assert cancelled["success"], cancelled
        async with asyncio.timeout(15):
            while True:
                state = await _native_invoke(native, 112, "game_operation_get", {"operation_id": operation_id})
                assert state["success"], state
                if state["data"]["status"] in {"cancelled", "failed", "completed"}:
                    break
                await asyncio.sleep(0.05)
        assert state["data"]["status"] == "cancelled", state
        assert not target_path.exists()
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        if connections:
            await asyncio.gather(*tuple(connections))


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
import time
import xxhash

class Plugin(BasePlugin):
    def on_load(self):
        self.host.call('settings.define', {'definitions':[{'key':'label','type':'string','default':'before'}]})
        self.define_command('probe', self.probe, output_fields=(
            {'name':'pid','type':'integer'}, {'name':'resource','type':'string'},
            {'name':'native','type':'string'}, {'name':'native_path','type':'string'},
            {'name':'count','type':'integer'}, {'name':'first','type':'string'},
            {'name':'tasks','type':'integer'},
            {'name':'exported','type':'bool'},
            {'name':'network_checked','type':'bool'},
            {'name':'download_checked','type':'bool'},
        ))

    def probe(self):
        network_checked = False
        if live_network_checks:
            target = 'https://connectivitycheck.gstatic.com/generate_204'
            for method in ('GET','HEAD'):
                result = self.host.call('network.request', {'method':method,'url':target}, timeout=15)
                assert result['status_code'] == 204 and result['body_b64'] == '', result
            text = self.host.call('network.request', {
                'method':'GET','url':'http://www.msftconnecttest.com/connecttest.txt'}, timeout=15)
            assert text['status_code'] == 200 and base64.b64decode(text['body_b64']) == b'Microsoft Connect Test'
            try:
                self.host.call('network.request', {'method':'GET','url':'https://unapproved.example/'})
            except HostApiError as exc:
                assert exc.error_code == 'PLUGIN_RESOURCE_DENIED', exc.error_code
            else:
                raise AssertionError('unapproved site accepted')
            network_checked = True
        resources = self.host.call('resources.list', {'capability':'instances.read'})['items']
        value = self.host.call('instances.get', {'instance_ref':resources[0]['instance_ref']})
        profile_receipt = self.host.call('instances.profile_patch', {
            'instance_ref':value['instance_ref'], 'expected_profile_etag':value['profile_etag'],
            'patch':{'alias':'Worker profile','favorite':False,'tags':[]}}, mutation_key='profile-update')
        deadline = time.monotonic() + 10
        while True:
            profile_task = self.host.call('operations.get', {'operation_ref':profile_receipt['operation_ref']})
            if profile_task['state'] in ('completed','failed','cancelled') and not profile_task['cleanup_pending']:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert profile_task['state'] == 'completed' and profile_task['effect'] == 'committed', profile_task
        current = self.host.call('instances.get', {'instance_ref':value['instance_ref']})
        assert current['alias'] == 'Worker profile'
        assert current['profile_etag'] == profile_task['result']['profile_etag'] != value['profile_etag']
        exported = export_snapshot(self.host)
        deadline = time.monotonic() + 10
        while True:
            task = self.host.call('operations.get', {'operation_ref':exported['operation_ref']})
            if task['state'] in ('completed','failed','cancelled'):
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert task['state'] == 'completed' and task['effect'] == 'committed', task
        content = self.host.call('files.read', {'target':{'root_ref':exported['root_ref'],
            'relative_path':exported['name']}, 'offset':0, 'count':32768})
        assert json.loads(base64.b64decode(content['data_b64']))['api_version'] == 2
        selected = export_snapshot(self.host)
        deadline = time.monotonic() + 10
        while True:
            ready = self.host.call('operations.get', {'operation_ref':selected['operation_ref']})
            if ready['state'] in ('completed','failed','cancelled'):
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert ready['state'] == 'completed', ready
        target = {'root_ref':selected['root_ref'], 'relative_path':selected['name']}
        version = self.host.call('files.stat', {'target':target})['etag']
        deletion = self.host.call('files.delete', {'target':target, 'expected_etag':version}, mutation_key='selected-delete')
        deadline = time.monotonic() + 10
        while True:
            removed = self.host.call('operations.get', {'operation_ref':deletion['operation_ref']})
            if removed['state'] in ('completed','failed','cancelled') and not removed['cleanup_pending']:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert removed['state'] == 'completed' and removed['effect'] == 'committed', removed
        assert removed['result'] == {'deleted':True}, removed
        target = {'root_ref':exported['root_ref'], 'relative_path':exported['name']}
        version = self.host.call('files.stat', {'target':target})['etag']
        payload = json.dumps({'api_version':2,'replaced':True}).encode()
        upload = self.host.call('files.replace_begin', {
            'target':target,'expected_etag':version,'total_bytes':len(payload)}, mutation_key='replace-begin')['write_ref']
        self.host.call('files.write_chunk', {
            'write_ref':upload,'offset':0,'data_b64':base64.b64encode(payload).decode()}, mutation_key='replace-chunk')
        replacement = self.host.call('files.write_commit', {
            'write_ref':upload,'sha256':hashlib.sha256(payload).hexdigest()}, mutation_key='replace-commit')
        deadline = time.monotonic() + 10
        while True:
            replaced = self.host.call('operations.get', {'operation_ref':replacement['operation_ref']})
            if replaced['state'] in ('completed','failed','cancelled') and not replaced['cleanup_pending']:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert replaced['state'] == 'completed' and replaced['effect'] == 'committed', replaced
        assert replaced['result']['etag'] != version
        download_checked = False
        if live_download_checks:
            params = {'url':'http://www.msftconnecttest.com/connecttest.txt',
                'target':{'root_ref':exported['root_ref'],'relative_path':'download.bin'},
                'overwrite':False, 'expected_sha256':hashlib.sha256(b'Microsoft Connect Test').hexdigest()}
            receipt = self.host.call('downloads.submit', params, mutation_key='native-download')
            deadline = time.monotonic() + 15
            while True:
                downloaded = self.host.call('operations.get', {'operation_ref':receipt['operation_ref']})
                if downloaded['state'] in ('completed','failed','cancelled'):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert downloaded['state'] == 'completed' and downloaded['effect'] == 'committed', downloaded
            assert self.host.call('downloads.submit', params, mutation_key='native-download') == receipt
            content = self.host.call('files.read', {'target':params['target'], 'offset':0,'count':32768})
            assert base64.b64decode(content['data_b64']) == b'Microsoft Connect Test'
            params['target'] = {'root_ref':exported['root_ref'],'relative_path':'cancel-download.bin'}
            cancelled = self.host.call('downloads.submit', params)
            self.host.call('operations.cancel', {'operation_ref':cancelled['operation_ref']})
            deadline = time.monotonic() + 10
            while True:
                stopped = self.host.call('operations.get', {'operation_ref':cancelled['operation_ref']})
                if stopped['state'] in ('completed','failed','cancelled'):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert stopped['state'] == 'cancelled' and stopped['effect'] == 'none', stopped
            download_checked = True
        return {'pid':os.getpid(),
            'exported':True,
            'network_checked':network_checked,
            'download_checked':download_checked,
            'tasks':len(self.host.call('operations.list')['items']),
            'count':len(resources), 'first':value['version_id'],
            'resource':importlib.resources.files('ecl_smoke_resource').joinpath('value.txt').read_text(),
            'native':xxhash.xxh64(b'ecl').hexdigest(), 'native_path':xxhash.__file__}
"""
    code = (
        (Path(__file__).resolve().parents[1] / "examples/plugins/worker_snapshot/main.py").read_text(encoding="utf-8")
        + f"\nlive_network_checks = {bool(os.environ.get('ECL_PLUGIN_SMOKE_LIVE_NETWORK'))!r}\n"
        + f"live_download_checks = {bool(os.environ.get('ECL_PLUGIN_SMOKE_LIVE_DOWNLOAD'))!r}\n"
        + code
    )
    package_path = make_package(
        tmp_path,
        code=code,
        wheel_paths=(pure_wheel, *native_wheels),
        dependencies=("ecl-smoke-resource==1.0.0", "xxhash==3.6.0"),
        permissions=(
            {"capability": "commands.register", "required": True},
            {"capability": "commands.execute", "required": True},
            {"capability": "instances.read", "required": True},
            {"capability": "instances.write", "required": True},
            {"capability": "settings.write", "required": True},
            {"capability": "settings.read", "required": True},
            {"capability": "filesystem.read", "required": True},
            {"capability": "filesystem.write", "required": True},
            {"capability": "filesystem.delete", "required": True},
            {"capability": "filesystem.replace", "required": True},
            {
                "capability": "network.read",
                "required": True,
                "resource_refs": [
                    "https:connectivitycheck.gstatic.com:443",
                    "http:www.msftconnecttest.com:80",
                    *(f"https:api{index}.{'a' * 50}.example.test:443" for index in range(14)),
                ],
            },
        ),
    )
    data_path = tmp_path / "data"
    game_path = tmp_path / "game"
    for name in ("permitted", "private"):
        directory = game_path / "versions" / name
        directory.mkdir(parents=True)
        (directory / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
        (directory / ".ecl").mkdir()
        (directory / ".ecl/instance.json").write_text(
            json.dumps({"schemaVersion": 1, "pinned": True, "custom": {"preserved": name}}), encoding="utf-8"
        )
    for phase in ("install", "restart"):
        log_path = tmp_path / f"{phase}.log"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            debug_port = listener.getsockname()[1]
        environment = dict(
            os.environ,
            WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug_port}",
            # 每次启动使用独立浏览器进程，避免复用旧进程时仍绑定上一阶段的调试端口。
            WEBVIEW2_USER_DATA_FOLDER=str(data_path / f"webview-profile-{phase}"),
        )
        with log_path.open("wb") as log_file:
            launcher = (
                [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py")]
                if os.environ.get("ECL_PLUGIN_SMOKE_SOURCE")
                else [str(executable_path)]
            )
            process = subprocess.Popen(
                [*launcher, "--data-dir", str(data_path), "--dev-channel"],
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
                # 开发者通道先于窗口启动；等待浏览器创建完成后才能安装并结束首阶段，
                # 否则终止进程时 WebView2 仍可能创建未纳入清理快照的子进程。
                native = await _native_channel(debug_port, process, log_path)
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
                    await check_network_permission_layout(native, process)
                    view = view["data"]
                    game = await _native_invoke(native, 101, "settings_get", {"section": "game"})
                    game_config = {**game["data"], "minecraft_paths": [{"name": "Test", "path": str(game_path)}]}
                    configured = await _native_invoke(
                        native, 102, "settings_set", {"section": "game", "data": game_config}
                    )
                    assert configured["success"], configured
                    candidate_query = {
                        "installation_id": identity,
                        "content_digest": view["content_digest"],
                        "capability": "instances.read",
                    }
                    roots = await _native_invoke(native, 103, "plugin_permission_resources", candidate_query)
                    assert roots["success"], roots
                    children = await _native_invoke(
                        native,
                        104,
                        "plugin_permission_resources",
                        {
                            **candidate_query,
                            "parent_ref": roots["data"]["items"][0]["resource_id"],
                        },
                    )
                    assert children["success"], children
                    selected = next(
                        item["resource_id"] for item in children["data"]["items"] if item["version_id"] == "permitted"
                    )
                    result = await _native_invoke(
                        native,
                        2,
                        "plugin_permissions_update",
                        {
                            "installation_id": identity,
                            "content_digest": view["content_digest"],
                            "expected_revision": view["revision"],
                            "decisions": [
                                {
                                    "capability": item["capability"],
                                    "decision": "allow",
                                    "resource_refs": [selected]
                                    if item["capability"] in {"instances.read", "instances.write"}
                                    else item["requested_resource_refs"],
                                }
                                for item in view["items"]
                            ],
                            "resource_revision": children["data"]["catalog_revision"],
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
                    assert probe["count"] == 1 and probe["first"] == "permitted"
                    assert Path(probe["native_path"]).is_relative_to(data_path / "plugin_deps")
                    assert (
                        probe["tasks"] == (7 if os.environ.get("ECL_PLUGIN_SMOKE_LIVE_DOWNLOAD") else 5)
                        and probe["exported"]
                    )
                    assert probe["download_checked"] == bool(os.environ.get("ECL_PLUGIN_SMOKE_LIVE_DOWNLOAD"))
                    assert probe["network_checked"] == bool(os.environ.get("ECL_PLUGIN_SMOKE_LIVE_NETWORK"))
                    profile = json.loads((game_path / "versions/permitted/.ecl/instance.json").read_bytes())
                    assert profile == {
                        "schemaVersion": 1,
                        "pinned": True,
                        "custom": {"preserved": "permitted"},
                        "alias": "Worker profile",
                        "favorite": False,
                        "tags": [],
                    }
                    assert "alias" not in json.loads((game_path / "versions/private/.ecl/instance.json").read_bytes())
                    files_path = data_path / "plugin_data" / identity / "files"
                    exports = tuple(files_path.glob("snapshot-*.json"))
                    assert len(exports) == 1
                    assert all(json.loads(path.read_bytes())["api_version"] == 2 for path in exports)
                    assert all(json.loads(path.read_bytes())["replaced"] for path in exports)
                    if os.environ.get("ECL_PLUGIN_SMOKE_LIVE_DOWNLOAD"):
                        assert (files_path / "download.bin").read_bytes() == b"Microsoft Connect Test"
                        assert not (files_path / "cancel-download.bin").exists()
                    saved = await _native_invoke(
                        native,
                        108,
                        "plugin_update_setting",
                        {
                            "plugin_name": "demo",
                            "key": "label",
                            "value": "saved",
                            "expected_revision": 0,
                        },
                    )
                    assert saved["success"], saved
                    settings = await _native_invoke(native, 109, "plugin_get_settings", {"plugin_name": "demo"})
                    assert settings["success"] and settings["data"]["values"]["label"] == "saved", settings
                    await _check_user_task_cancel(native, data_path)
                    reviewed = await _native_invoke(
                        native, 105, "plugin_permissions_get", {"installation_id": identity}
                    )
                    revoked = await _native_invoke(
                        native,
                        106,
                        "plugin_permissions_update",
                        {
                            "installation_id": identity,
                            "content_digest": reviewed["data"]["content_digest"],
                            "expected_revision": reviewed["data"]["revision"],
                            "decisions": [
                                {"capability": item["capability"], "decision": "deny", "resource_refs": []}
                                for item in reviewed["data"]["items"]
                            ],
                        },
                    )
                    assert revoked["success"] and not revoked["data"]["required_satisfied"], revoked
                    await _wait_cleanup(native, identity)
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
