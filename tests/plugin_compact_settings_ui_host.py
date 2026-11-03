from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright
from plugin_development_ui_host import (
    _call,
    _prepare_sources,
    _resize_native_window,
    _shutdown,
    _start_launcher,
    _wait_connection,
    _wait_for,
)
from plugin_toolbar_ui_host import _native_window, _theme


async def _dismiss_notices(page):
    for _ in range(3):
        button = page.get_by_role("dialog").get_by_role("button", name="确定", exact=True)
        if not await button.count():
            return
        await button.first.click()


async def _settings_geometry(page):
    await _dismiss_notices(page)
    await page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
    metrics = await page.locator(".plugin-settings-center").evaluate("""el=>{
        const header=el.querySelector('.plugin-settings-heading').getBoundingClientRect();
        const nav=el.querySelector('.plugin-settings-navigation').getBoundingClientRect();
        const detail=el.querySelector('.plugin-settings-detail').getBoundingClientRect();
        const choices=[...el.querySelectorAll('.plugin-settings-choice')].map(node=>({
            height:node.getBoundingClientRect().height,text:node.innerText,title:node.title
        }));
        const title=el.querySelector('h2').getBoundingClientRect();
        const version=el.querySelector('.plugin-settings-version')?.getBoundingClientRect();
        return {header:header.height,navLeft:nav.left,navRight:nav.right,detailLeft:detail.left,right:detail.right,
            bottom:Math.max(nav.bottom,detail.bottom),width:innerWidth,height:innerHeight,
            overflow:el.scrollWidth-el.clientWidth,choices,titleCenter:(title.top+title.bottom)/2,
            versionCenter:version ? (version.top+version.bottom)/2:null};
    }""")
    assert 44 <= metrics["header"] <= 48, metrics
    assert all(34 <= item["height"] <= 38 and "\n" not in item["text"] for item in metrics["choices"]), metrics
    assert metrics["versionCenter"] is not None and abs(metrics["titleCenter"] - metrics["versionCenter"]) < 2, metrics
    assert metrics["navLeft"] >= 12 and metrics["navRight"] < metrics["detailLeft"], metrics
    assert metrics["right"] <= metrics["width"] - 12 and metrics["bottom"] <= metrics["height"], metrics
    assert metrics["overflow"] <= 1, metrics
    return metrics


async def _matrix(page, process, output):
    user, handle = _native_window(process)
    records = []
    for skin in ("classic", "folia"):
        for mode in ("light", "dark"):
            await _theme(page, skin, mode)
            for width, height in ((960, 600), (1200, 800), (1440, 1000)):
                await _resize_native_window(page, user, handle, width, height)
                metrics = await _settings_geometry(page)
                last = page.locator(".plugin-settings-choice").last
                await last.scroll_into_view_if_needed()
                assert await last.is_visible()
                records.append({"skin": skin, "mode": mode, "width": width, "height": height, "geometry": metrics})
                await page.screenshot(path=str(output / f"settings-{skin}-{mode}-{width}.png"))
            await _resize_native_window(page, user, handle, 960, 600)
            for locale in ("zh-CN", "zh-TW", "en-US", "de-DE", "ja-JP", "ru-RU"):
                await _theme(page, skin, mode, locale)
                metrics = await _settings_geometry(page)
                records.append({"skin": skin, "mode": mode, "locale": locale, "geometry": metrics})
                await page.screenshot(path=str(output / f"settings-{skin}-{mode}-{locale}.png"))
    return records


async def _status_hints(page, output):
    assert await page.locator(".plugin-error").count() == 0
    row = page.locator(".plugin-row").filter(has_text="显式拒绝热重载示例")
    await row.scroll_into_view_if_needed()
    note = row.get_by_role("note", name="待审阅", exact=True)
    description = row.locator(".plugin-status-description")
    assert await description.inner_text() == "请先审阅插件权限。"
    assert await note.get_attribute("aria-describedby") == await description.get_attribute("id")
    assert await description.evaluate("el=>{const r=el.getBoundingClientRect();return r.width<=1&&r.height<=1}")
    await note.focus()
    tooltip = page.locator(".plugin-error-tooltip")
    await tooltip.wait_for()
    assert await tooltip.inner_text() == "请先审阅插件权限。"
    await page.get_by_role("textbox", name="搜索插件...", exact=True).hover()
    assert await tooltip.is_visible()
    assert await tooltip.evaluate(
        "el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight}"
    )
    await page.screenshot(path=str(output / "status-focus-hint.png"))
    await page.keyboard.press("Escape")
    await tooltip.wait_for(state="hidden")
    await page.keyboard.press("Tab")
    await note.hover()
    await tooltip.wait_for()
    await page.get_by_role("textbox", name="搜索插件...", exact=True).hover()
    await tooltip.wait_for(state="hidden")


async def _settings_flow(page):
    await _theme(page, "classic", "light")
    await page.keyboard.press("Escape")
    await page.locator(".plugin-settings-center").wait_for(state="hidden")
    row = page.locator(".plugin-row").filter(has_text="插件开发与统一配置示例")
    await row.get_by_role("button", name="启用", exact=True).click()
    await _wait_for(
        lambda: _call(page, "plugin_list"),
        lambda items: any(p["name"] == "worker_development" and p["status"] == "enabled" for p in items),
    )
    await row.get_by_text("已启用", exact=True).wait_for()
    await row.locator(".plugin-status-hint").wait_for(state="hidden")
    await page.get_by_role("button", name="插件设置", exact=True).click()
    search = page.locator(".plugin-settings-search input")
    await search.fill("worker_development")
    choice = page.locator(".plugin-settings-choice")
    assert await choice.count() == 1
    assert await choice.inner_text() == "插件开发与统一配置示例"
    assert "worker_development" in (await choice.get_attribute("title"))
    await choice.click()
    field = page.get_by_role("textbox", name="问候内容", exact=True)
    await field.fill("紧凑设置验收")
    await field.press("Tab")
    await _wait_for(
        lambda: _call(page, "plugin_get_settings", {"plugin_name": "worker_development"}),
        lambda settings: settings["values"]["greeting"] == "紧凑设置验收",
    )
    await search.fill("worker_no_hot_reload")
    await page.locator(".plugin-settings-choice").click()
    await search.fill("worker_development")
    await page.locator(".plugin-settings-choice").click()
    await field.wait_for()
    assert await field.input_value() == "紧凑设置验收"
    assert await page.get_by_role("button", name="打开插件目录", exact=True).is_visible()


async def _backgrounds(page, output):
    await page.locator(".plugin-settings-search input").fill("worker_")
    image = await page.evaluate("""()=>{
        const canvas=document.createElement('canvas');canvas.width=canvas.height=320;const ctx=canvas.getContext('2d');
        const gradient=ctx.createLinearGradient(0,0,320,320);gradient.addColorStop(0,'#476f68');
        gradient.addColorStop(1,'#e1b9ae');ctx.fillStyle=gradient;ctx.fillRect(0,0,320,320);return canvas.toDataURL();
    }""")
    await page.evaluate(
        "image=>document.getElementById('app').__vue_app__.config.globalProperties.$pinia._s.get('theme').setBackgroundImage(image,undefined,false)",
        image,
    )
    for skin in ("classic", "folia"):
        for mode in ("light", "dark"):
            await _theme(page, skin, mode)
            await _settings_geometry(page)
            await page.screenshot(path=str(output / f"settings-image-{skin}-{mode}.png"))
    await _theme(page, "classic", "light")
    await page.screenshot(path=str(output / "settings-final.png"))


async def main():
    if sys.platform != "win32":
        raise RuntimeError("Native compact plugin settings acceptance requires Windows")
    root = Path(__file__).resolve().parents[1]
    output = root / "output/playwright/plugin-compact-settings" / uuid4().hex[:8]
    output.mkdir(parents=True)
    print(f"OUTPUT {output}", flush=True)
    data, launcher = _prepare_sources(root, output)
    server, port, process, log_file = _start_launcher(root, output, data, launcher, "--no-plugin-dev")
    try:
        await _wait_connection(port, process)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = next(
                p
                for c in browser.contexts
                for p in c.pages
                if p.url.startswith(f"http://127.0.0.1:{server.server_port}/")
            )
            page.set_default_timeout(15000)
            await page.wait_for_function(
                "document.getElementById('app').__vue_app__ && document.body.innerText.length > 10"
            )
            await page.add_locator_handler(
                page.get_by_role("button", name="稍后", exact=True), lambda locator: locator.click()
            )
            await page.add_locator_handler(
                page.get_by_role("dialog").get_by_role("button", name="确定", exact=True),
                lambda locator: locator.click(),
            )
            await page.evaluate("location.hash='#/more/plugins'")
            await page.locator(".plugin-row").first.wait_for()
            await _dismiss_notices(page)
            settings = page.get_by_role("button", name="插件设置", exact=True)
            await settings.focus()
            await page.keyboard.press("Enter")
            await page.locator(".plugin-settings-center").wait_for()
            await page.keyboard.press("Escape")
            await page.locator(".plugin-settings-center").wait_for(state="hidden")
            await _wait_for(lambda: settings.evaluate("el=>el===document.activeElement"), bool)
            await page.keyboard.press("Enter")
            await page.locator(".plugin-settings-center").wait_for()
            await page.screenshot(path=str(output / "settings-initial.png"))
            await _settings_geometry(page)
            records = await _matrix(page, process, output)
            await _theme(page, "classic", "light")
            await page.keyboard.press("Escape")
            await page.locator(".plugin-settings-center").wait_for(state="hidden")
            await _status_hints(page, output)
            await settings.click()
            await _settings_flow(page)
            await _backgrounds(page, output)
            await page.keyboard.press("Escape")
            await page.locator(".plugin-settings-center").wait_for(state="hidden")
            await page.locator(".plugins-search input").fill("worker_")
            await page.screenshot(path=str(output / "plugins-final.png"))
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "records": records,
                        "image_backgrounds": 4,
                        "status_hints": True,
                        "settings_saved": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print("Compact settings and status hints passed", flush=True)
            await browser.close()
    except Exception:
        if "page" in locals():
            with suppress(Exception):
                await page.screenshot(path=str(output / "failure.png"))
                print(await page.get_by_role("dialog").all_text_contents(), flush=True)
        raise
    finally:
        _shutdown(process, server, log_file)


if __name__ == "__main__":
    asyncio.run(main())
