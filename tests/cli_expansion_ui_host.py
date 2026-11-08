from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from copy import deepcopy
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

from playwright.async_api import async_playwright
from plugin_development_ui_host import QuietHandler, _call, _shutdown, _wait_connection

from ECL.utils.config import default_config


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "output/playwright/cli-expansion" / uuid4().hex[:8]
    data_path = output / "data"
    data_path.mkdir(parents=True)
    (data_path / "home").mkdir()
    shutil.copytree(root / "examples/plugins/worker_no_hot_reload", data_path / "plugins/worker_no_hot_reload")
    config = deepcopy(default_config)
    config["user_agreement"] = {"accepted": True, "uuid": "cli-acceptance"}
    config["ui"]["locale"] = "zh-CN"
    (data_path / "setting.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    wrapper = output / "launcher.py"
    wrapper.write_text(
        "from pathlib import Path\nimport os, sys\n"
        "Path.home = classmethod(lambda cls: Path(os.environ['ECL_TEST_HOME']))\n"
        "from ECL.bootstrap import run_process\nraise SystemExit(run_process(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root / "frontend/dist")))
    Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = dict(
        os.environ,
        PYTHONUTF8="1",
        _PYTAURI_DIST="pytauri-wheel",
        PYTHONPATH=str(root),
        ECL_TEST_HOME=str(data_path / "home"),
    )
    log_file = (output / "launcher.log").open("ab")
    base = [sys.executable, str(wrapper), "--data-dir", str(data_path)]
    process = subprocess.Popen(
        [
            *base,
            "--language",
            "en-US",
            "--start-minimized",
            "--disable-plugin",
            "worker_no_hot_reload",
            "--frontend-dist",
            f"http://127.0.0.1:{server.server_port}/",
            "--dev-channel",
            "--webview-debug-port",
            str(port),
            "--open-page",
            "java",
        ],
        cwd=root,
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    print(f"OUTPUT {output}", flush=True)
    try:
        await _wait_connection(port, process)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = next(
                page
                for context in browser.contexts
                for page in context.pages
                if page.url.startswith("http://127.0.0.1")
            )
            page.set_default_timeout(20000)
            await page.wait_for_function(
                "document.documentElement.lang === 'en-US' && location.hash.includes('/settings/java')"
            )
            assert await page.evaluate("window.__TAURI__.window.getCurrentWindow().isMinimized()")
            info = await _call(page, "launcher_info")
            assert info["session_language"] == "en-US"
            saved = await _call(page, "settings_get", {"section": "ui"})
            assert saved["locale"] == "zh-CN"
            restored = await asyncio.to_thread(
                subprocess.run,
                [*base, "--open-page", "java"],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=20,
                creationflags=0x08000000,
            )
            assert restored.returncode == 0
            warning = (
                page.get_by_role("dialog")
                .filter(has_text="CurseForge API Key 未配置")
                .get_by_role("button", name="OK", exact=True)
            )
            if await warning.is_visible():
                await warning.click()
            await page.wait_for_timeout(350)
            await page.screenshot(path=str(output / "java-en-US.png"))
            for target, suffix in (("plugins", "/more/plugins"), ("accounts", "/")):
                result = await asyncio.to_thread(
                    subprocess.run,
                    [*base, "--open-page", target],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    timeout=20,
                    creationflags=0x08000000,
                )
                assert result.returncode == 0, result.stderr.decode(errors="replace")
                await page.wait_for_function("suffix => location.hash.endsWith(suffix)", arg=suffix)
                if target == "plugins":
                    row = page.locator(".plugin-row").filter(has_text="worker_no_hot_reload")
                    await row.wait_for()
                    assert await row.get_by_role("button", name="Enable", exact=True).is_disabled()
                    assert await row.get_by_role("button", name="Reload", exact=True).is_disabled()
                    entries = await _call(page, "plugin_list")
                    assert entries[0]["error"] == "PLUGIN_DISABLED_FOR_SESSION"
                    await row.get_by_role("note").hover()
                if target == "accounts":
                    await page.locator(".account-modal-body").wait_for()
                await page.screenshot(path=str(output / f"{target}-en-US.png"))
            rejected = await asyncio.to_thread(
                subprocess.run,
                [*base, "--language", "ja-JP"],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=20,
                creationflags=0x08000000,
            )
            assert rejected.returncode != 0
            assert await page.evaluate("document.documentElement.lang") == "en-US"
            await _call(page, "settings_set", {"section": "ui", "data": {**saved, "flowDebug": True}})
            await page.wait_for_timeout(250)
            persisted = json.loads((data_path / "setting.json").read_text(encoding="utf-8"))
            assert persisted["ui"]["locale"] == "zh-CN"
            assert await page.evaluate("document.documentElement.lang") == "en-US"
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "checks": [
                            "minimize",
                            "language",
                            "java",
                            "plugins",
                            "accounts",
                            "forwarding",
                            "reject-process-change",
                            "preserve-saved-language",
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            await browser.close()
    finally:
        _shutdown(process, server, log_file)


if __name__ == "__main__":
    asyncio.run(main())
