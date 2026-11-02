from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
from contextlib import suppress
from ctypes import wintypes
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic
from uuid import uuid4

import httpx
import psutil
from playwright.async_api import async_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def _prepare_sources(root, output):
    data = output / "data"
    data.mkdir()
    (data / "home").mkdir()
    launcher = output / "launcher.py"
    launcher.write_text(
        "from pathlib import Path\nimport os, sys\n"
        "Path.home = classmethod(lambda cls: Path(os.environ['ECL_TEST_HOME']))\n"
        "from ECL.bootstrap import run_process\nraise SystemExit(run_process(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    for name in ("worker_development", "worker_no_hot_reload"):
        shutil.copytree(root / "examples/plugins" / name, data / "plugins" / name)
    for index in range(18):
        target = data / "plugins" / f"extra_{index:02}"
        shutil.copytree(root / "examples/plugins/worker_no_hot_reload", target)
        metadata = json.loads((target / "plugin.json").read_bytes())
        metadata["name"] = f"extra_{index:02}"
        metadata["title"] = f"Long plugin name / 很长的插件名称 {index:02} — configuration navigation"
        (target / "plugin.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return data, launcher


def _start_launcher(root, output, data, launcher):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root / "frontend/dist")))
    Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = dict(
        os.environ,
        PYTHONUTF8="1",
        _PYTAURI_DIST="pytauri-wheel",
        PYTHONPATH=str(root),
        ECL_TEST_HOME=str(data / "home"),
    )
    environment.pop("WEBVIEW2_USER_DATA_FOLDER", None)
    environment.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    log_file = (output / "launcher.log").open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            str(launcher),
            "--frontend-dist",
            url,
            "--data-dir",
            str(data),
            "--dev-channel",
            "--webview-debug-port",
            str(port),
        ],
        cwd=root,
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    return server, port, process, log_file


async def _wait_connection(port, process):
    end = monotonic() + 45
    async with httpx.AsyncClient(trust_env=False) as client:
        while True:
            try:
                response = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
                response.raise_for_status()
                break
            except httpx.HTTPError as exc:
                if process.poll() is not None or monotonic() > end:
                    raise AssertionError("Native launcher did not start") from exc
                await asyncio.sleep(0.1)


async def _call(page, command, body=None):
    reply = await page.evaluate(
        "([command,body])=>window.__TAURI__.pytauri.pyInvoke(command,body)", [command, body or {}]
    )
    assert reply["success"], (command, reply)
    return reply.get("data")


async def _wait_for(action, predicate, seconds=20):
    deadline = monotonic() + seconds
    last = None
    while monotonic() < deadline:
        last = await action()
        if predicate(last):
            return last
        await asyncio.sleep(0.15)
    raise AssertionError(last)


async def _command_result(page, command):
    return await page.evaluate(
        "command=>window.__TAURI__.pytauri.pyInvoke('plugin_call_command',{command,params:{}})", command
    )


async def _exercise_settings(page, data):
    assert (await _call(page, "accounts_list"))["accounts"] == []
    await page.evaluate("location.hash='#/more/plugins'")
    await page.locator(".plugin-row").first.wait_for()
    for title in ("插件开发与统一配置示例", "显式拒绝热重载示例"):
        row = page.locator(".plugin-row").filter(has_text=title)
        await row.get_by_role("button", name="启用", exact=True).click()
        await _wait_for(
            lambda: _call(page, "plugin_list"),
            lambda items, expected=title: any(p["title"] == expected and p["status"] == "enabled" for p in items),
        )
    print("Enabled two actual workers via UI", flush=True)
    await page.get_by_role("switch", name="开发模式", exact=True).click()
    await _wait_for(
        lambda: _call(page, "plugin_development_get"),
        lambda state: (
            state["enabled"] and len(state["items"]) == 2 and all(p["reload_count"] >= 1 for p in state["items"])
        ),
    )
    settings_button = page.get_by_role("button", name="插件设置", exact=True)
    await settings_button.focus()
    await page.keyboard.press("Enter")
    await page.locator(".plugin-settings-center").wait_for()
    await page.locator(".plugin-settings-choice").filter(has_text="插件开发与统一配置示例").click()
    field = page.locator('.plugin-settings-detail input[aria-label="问候内容"]')
    await field.fill("界面验收保存值")
    await field.press("Tab")
    await _wait_for(
        lambda: _call(page, "plugin_get_settings", {"plugin_name": "worker_development"}),
        lambda value: value["values"].get("greeting") == "界面验收保存值",
    )
    source = data / "plugins/worker_development/main.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace('code_version: str = "v1"', 'code_version: str = "v2"'),
        encoding="utf-8",
    )
    await _wait_for(
        lambda: _command_result(page, "worker_development:status"),
        lambda value: value["success"] and value["data"]["message"] == "v2: 界面验收保存值",
    )
    await field.wait_for()
    assert await field.input_value() == "界面验收保存值"
    print("Saved configuration and observed v2 after actual source edit", flush=True)


async def _verify_layouts(page, process, output):
    records = []
    owned = {process.pid, *(child.pid for child in psutil.Process(process.pid).children(recursive=True))}
    user = ctypes.WinDLL("user32", use_last_error=True)
    handles = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]

    @callback_type
    def enumerate_window(handle, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(handle, title, 256)
        if pid.value in owned and user.IsWindowVisible(handle) and "EuoraCraft Launcher" in title.value:
            handles.append(handle)
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(enumerate_window, 0)
    assert len(handles) == 1, handles
    handle = handles[0]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    outer, client = wintypes.RECT(), wintypes.RECT()
    user.GetWindowRect(handle, ctypes.byref(outer))
    user.GetClientRect(handle, ctypes.byref(client))
    frame_w = outer.right - outer.left - client.right
    frame_h = outer.bottom - outer.top - client.bottom
    ratio = await page.evaluate("devicePixelRatio")
    for skin in ("classic", "folia"):
        for mode in ("light", "dark"):
            await page.evaluate(
                "([skin,mode])=>{const s=document.getElementById('app').__vue_app__.config.globalProperties.$pinia._s.get('theme');s.setThemeId(skin,false);s.setThemeMode(mode,false);s.setAppearance({card_opacity:60},false);s.setBlurAmount(12,false)}",
                [skin, mode],
            )
            for width, height in ((960, 600), (1200, 800), (1440, 1000)):
                assert user.SetWindowPos(
                    handle, None, 10, 10, int(width * ratio) + frame_w, int(height * ratio) + frame_h, 0x14
                )
                await page.wait_for_function("([w,h])=>innerWidth===w&&innerHeight===h", arg=[width, height])
                await page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                geometry = await page.locator(".plugin-settings-center").evaluate(
                    "el=>{const n=el.querySelector('.plugin-settings-navigation').getBoundingClientRect(),d=el.querySelector('.plugin-settings-detail').getBoundingClientRect();return {left:n.left,navRight:n.right,detailLeft:d.left,right:d.right,bottom:Math.max(n.bottom,d.bottom),height:innerHeight,overflow:el.scrollWidth-el.clientWidth,bodyOverflow:el.closest('main').scrollHeight-el.closest('main').clientHeight}}"
                )
                assert (
                    geometry["left"] >= 12
                    and geometry["right"] <= width - 12
                    and geometry["bottom"] <= height
                    and geometry["navRight"] < geometry["detailLeft"]
                ), geometry
                assert geometry["overflow"] <= 1 and geometry["bodyOverflow"] <= 1, geometry
                last = page.locator(".plugin-settings-choice").last
                await last.scroll_into_view_if_needed()
                assert await last.is_visible()
                records.append({"skin": skin, "mode": mode, "width": width, "height": height, "geometry": geometry})
                await page.screenshot(path=str(output / f"settings-{skin}-{mode}-{width}.png"))
    for locale in ("zh-CN", "zh-TW", "en-US", "de-DE", "ja-JP", "ru-RU"):
        assert user.SetWindowPos(handle, None, 10, 10, int(960 * ratio) + frame_w, int(600 * ratio) + frame_h, 0x14)
        await page.evaluate(
            "locale=>document.getElementById('app').__vue_app__.config.globalProperties.$i18n.locale=locale", locale
        )
        await page.wait_for_function("innerWidth===960 && innerHeight===600")
        assert await page.locator(".plugin-settings-center").evaluate("el=>el.scrollWidth<=el.clientWidth+1")
        await page.screenshot(path=str(output / f"settings-{locale}.png"))
        records.append({"locale": locale, "width": 960, "height": 600})
    await page.evaluate("document.getElementById('app').__vue_app__.config.globalProperties.$i18n.locale='zh-CN'")
    await page.keyboard.press("Tab")
    assert await page.evaluate("document.activeElement.closest('.plugin-settings-center') !== null")
    await page.keyboard.press("Shift+Tab")
    assert await page.evaluate("document.activeElement.closest('.fullscreen-modal') !== null")
    await page.keyboard.press("Escape")
    await page.locator(".plugin-settings-center").wait_for(state="hidden")
    await _wait_for(
        lambda: page.get_by_role("button", name="插件设置", exact=True).evaluate("el=>el===document.activeElement"),
        bool,
    )
    print("Native 12-layout / 6-locale matrix passed", flush=True)
    return records


async def _exercise_opt_out(page, data):
    before = next(item for item in await _call(page, "plugin_list") if item["name"] == "worker_no_hot_reload")
    assert before["status"] == "enabled" and before["development"]["status"] == "opted_out", before
    refused = data / "plugins/worker_no_hot_reload/main.py"
    refused.write_text(
        refused.read_text(encoding="utf-8").replace('code_version: str = "v1"', 'code_version: str = "v2"'),
        encoding="utf-8",
    )
    await asyncio.sleep(2.5)
    after = next(item for item in await _call(page, "plugin_list") if item["name"] == "worker_no_hot_reload")
    assert after["status"] == "enabled" and after["development"]["status"] == "opted_out", after
    assert after["development"]["reload_count"] == before["development"]["reload_count"], after
    assert (await _call(page, "plugin_call_command", {"command": "worker_no_hot_reload:version", "params": {}}))[
        "version"
    ] == "v1"
    row = page.locator(".plugin-row").filter(has_text="显式拒绝热重载示例")
    await row.get_by_role("button", name="重载", exact=True).click()
    await _wait_for(
        lambda: _command_result(page, "worker_no_hot_reload:version"),
        lambda value: value["success"] and value["data"]["version"] == "v2",
    )
    await page.get_by_role("switch", name="开发模式", exact=True).click()
    await _wait_for(
        lambda: _call(page, "plugin_development_get"), lambda state: not state["enabled"] and not state["items"]
    )
    items = await _call(page, "plugin_list")
    assert all(
        item["status"] != "enabled" for item in items if item["name"] in {"worker_development", "worker_no_hot_reload"}
    )


def _shutdown(process, server, log_file):
    children = psutil.Process(process.pid).children(recursive=True) if process.poll() is None else []
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
    for child in children:
        with suppress(psutil.Error):
            child.terminate()
    server.shutdown()
    log_file.close()


async def main():
    if sys.platform != "win32":
        raise RuntimeError("This native launcher acceptance gate requires Windows")
    root = Path(__file__).resolve().parents[1]
    output = root / "output/playwright/plugin-development" / uuid4().hex[:8]
    output.mkdir(parents=True)
    print(f"OUTPUT {output}", flush=True)
    data, launcher = _prepare_sources(root, output)
    server, port, process, log_file = _start_launcher(root, output, data, launcher)
    try:
        await _wait_connection(port, process)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = next(
                page
                for context in browser.contexts
                for page in context.pages
                if page.url.startswith(f"http://127.0.0.1:{server.server_port}/")
            )
            page.set_default_timeout(15000)
            await page.wait_for_function(
                "document.getElementById('app').__vue_app__ && document.body.innerText.length > 10"
            )
            await page.add_locator_handler(
                page.get_by_role("button", name="稍后", exact=True), lambda locator: locator.click()
            )
            warning = (
                page.get_by_role("dialog")
                .filter(has_text="CurseForge API Key 未配置")
                .get_by_role("button", name="确定", exact=True)
            )
            await page.add_locator_handler(warning, lambda locator: locator.click())
            await _exercise_settings(page, data)
            records = await _verify_layouts(page, process, output)
            await _exercise_opt_out(page, data)
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "records": records,
                        "workflow": [
                            "enable",
                            "development",
                            "save-settings",
                            "source-v2",
                            "opt-out",
                            "manual-reload",
                            "disable-development",
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print("UI workflow passed", flush=True)
            await browser.close()
    finally:
        _shutdown(process, server, log_file)


if __name__ == "__main__":
    asyncio.run(main())
