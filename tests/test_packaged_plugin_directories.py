from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

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


async def _verify_browser_install(frontend_url: str, package_path: Path, capture_path: Path) -> None:
    """
    可选桌面验收只替代原生文件选择，预检、确认和安装仍调用真实冻结后端。
    """
    from playwright.async_api import async_playwright

    capture_path.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(channel=os.environ.get("ECL_PLUGIN_SMOKE_BROWSER_CHANNEL", "chrome"))
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.add_init_script("window.__eclSmokePackagePath = " + json.dumps(str(package_path)))
            await page.add_init_script(
                """(() => {
                const send = WebSocket.prototype.send;
                WebSocket.prototype.send = function(raw) {
                    const request = JSON.parse(raw);
                    if (request.method === 'frontend.invoke' && request.params.command === 'select_file') {
                        setTimeout(() => this.dispatchEvent(new MessageEvent('message', {data: JSON.stringify({
                            id: request.id, ok: true, data: {command: 'select_file', result: {success: true, data: {path: window.__eclSmokePackagePath}}}
                        })})), 0);
                        return;
                    }
                    return send.call(this, raw);
                };
            })();"""
            )
            await page.goto(frontend_url + "#/more/plugins")
            await page.locator(".plugins-toolbar button").filter(has_text="安装插件").click()
            await page.get_by_text("安装文件", exact=True).click()
            dialog = page.get_by_role("dialog").filter(has_text="确认插件安装")
            await dialog.wait_for(state="visible")
            assert "运行时" not in await dialog.inner_text()
            await page.screenshot(path=str(capture_path / "desktop-install.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(capture_path / "mobile-install.png"))
            bounds = await dialog.locator(".modal-container").bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
            await page.set_viewport_size({"width": 1280, "height": 900})
            await dialog.get_by_role("switch").click()
            await dialog.get_by_role("checkbox").check()
            await dialog.get_by_role("button", name="安装插件", exact=True).click()
            await dialog.wait_for(state="hidden")
            await page.locator(".plugin-status").filter(has_text="待重启").wait_for()
            await page.screenshot(path=str(capture_path / "desktop-pending.png"))
        finally:
            await browser.close()


@pytest.mark.skipif(not os.environ.get("ECL_PLUGIN_SMOKE_EXECUTABLE"), reason="需要显式提供真实单文件启动器产物")
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
    code = """from ECL.plugins import Plugin as BasePlugin
import os
import sys
import importlib.resources
import xxhash

class Plugin(BasePlugin):
    @BasePlugin.on_command('probe')
    def probe(self):
        return {
            'pid': os.getpid(),
            'sdk_module': BasePlugin.__module__,
            'has_host_context': self.framework is not None and self.name == 'demo',
            'frozen': bool(getattr(sys, 'frozen', False) or '__compiled__' in globals()),
            'resource': importlib.resources.files('ecl_smoke_resource').joinpath('value.txt').read_text(),
            'native': xxhash.xxh64(b'ecl').hexdigest(),
            'native_path': xxhash.__file__,
        }
"""
    package_path = make_package(
        tmp_path,
        code=code,
        wheel_paths=(pure_wheel, *native_wheels),
        dependencies=("ecl-smoke-resource==1.0.0", "xxhash==3.6.0"),
        permissions=({"scope": "commands", "action": "execute", "resource": "probe"},),
    )
    data_path = tmp_path / "data"
    for phase in ("install", "restart"):
        log_path = tmp_path / f"{phase}.log"
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                [str(executable_path), "--data-dir", str(data_path), "--dev-channel"],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            websocket = None
            try:
                websocket = await _connect(data_path, process, log_path)
                info = await _request(websocket, 1, "launcher.info")
                if phase == "install":
                    if os.environ.get("ECL_PLUGIN_SMOKE_BROWSER"):
                        await _request(websocket, 10, "frontend.invoke", {"command": "user_agreement_save"})
                        await _verify_browser_install(
                            info["frontendUrl"],
                            package_path,
                            Path(os.environ.get("ECL_PLUGIN_SMOKE_CAPTURES", str(tmp_path / "captures"))),
                        )
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
                    result = await _request(
                        websocket,
                        4,
                        "frontend.invoke",
                        {"command": "plugin_call_command", "payload": {"command": "demo:probe"}},
                    )
                    assert result["result"]["success"], result
                    probe = result["result"]["data"]
                    assert probe["pid"] == info["pid"]
                    assert probe["sdk_module"] == "ECL.plugins.plugin" and probe["has_host_context"]
                    assert probe["resource"] == "offline-resource" and len(probe["native"]) == 16
                    assert Path(probe["native_path"]).is_relative_to(data_path / "plugin_deps")
                    assert not (data_path / "plugin_envs").exists()
                    assert not (data_path / "plugin_runtimes").exists()
            finally:
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
