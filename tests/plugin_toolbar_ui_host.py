from __future__ import annotations

import asyncio
import ctypes
import json
import sys
from ctypes import wintypes
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import psutil
from playwright.async_api import async_playwright
from plugin_development_ui_host import (
    _prepare_sources,
    _resize_native_window,
    _shutdown,
    _start_launcher,
    _wait_connection,
)


def _native_window(process):
    user = ctypes.WinDLL("user32", use_last_error=True)
    owned = {process.pid, *(child.pid for child in psutil.Process(process.pid).children(recursive=True))}
    handles = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]

    @callback_type
    def visit(handle, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(handle, title, 256)
        if pid.value in owned and user.IsWindowVisible(handle) and "EuoraCraft Launcher" in title.value:
            handles.append(handle)
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(visit, 0)
    assert len(handles) == 1, handles
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    return user, handles[0]


async def _geometry(page):
    await page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
    geometry = await page.locator(".plugins-toolbar").evaluate("""el=>{
        const bar=el.getBoundingClientRect();
        const nodes=[el.querySelector('.plugins-search'),el.querySelector('.plugins-filters'),
            ...el.querySelectorAll('.plugins-toolbar-actions button')];
        const rects=nodes.map(node=>{const r=node.getBoundingClientRect();return {
            left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,center:(r.top+r.bottom)/2
        }});
        return {left:bar.left,right:bar.right,height:bar.height,overflow:el.scrollWidth-el.clientWidth,rects};
    }""")
    rects = geometry["rects"]
    assert len(rects) == 4, geometry
    assert max(r["center"] for r in rects) - min(r["center"] for r in rects) < 2, geometry
    assert geometry["height"] <= 56 and geometry["overflow"] <= 1, geometry
    assert rects[0]["width"] >= 100, geometry
    assert all(r["left"] >= geometry["left"] and r["right"] <= geometry["right"] for r in rects), geometry
    assert all(a["right"] <= b["left"] + 1 for a, b in pairwise(rects)), geometry
    return geometry


async def _theme(page, skin, mode, locale="zh-CN"):
    await page.evaluate(
        """([skin,mode,locale])=>{
        const app=document.getElementById('app').__vue_app__.config.globalProperties;
        const theme=app.$pinia._s.get('theme');theme.setThemeId(skin,false);theme.setThemeMode(mode,false);
        theme.setAppearance({card_opacity:60},false);theme.setBlurAmount(12,false);app.$i18n.locale=locale;
    }""",
        [skin, mode, locale],
    )


async def _layouts(page, process, output):
    user, handle = _native_window(process)
    records = []
    for skin in ("classic", "folia"):
        for mode in ("light", "dark"):
            await _theme(page, skin, mode)
            for width, height in ((960, 600), (1200, 800), (1440, 1000)):
                await _resize_native_window(page, user, handle, width, height)
                metrics = await _geometry(page)
                records.append({"skin": skin, "mode": mode, "width": width, "height": height, "geometry": metrics})
                await page.screenshot(path=str(output / f"toolbar-{skin}-{mode}-{width}.png"))
            await _resize_native_window(page, user, handle, 960, 600)
            for locale in ("zh-CN", "zh-TW", "en-US", "de-DE", "ja-JP", "ru-RU"):
                await _theme(page, skin, mode, locale)
                metrics = await _geometry(page)
                records.append({"skin": skin, "mode": mode, "locale": locale, "geometry": metrics})
                await page.screenshot(path=str(output / f"toolbar-{skin}-{mode}-{locale}.png"))
    return records


async def _interactions(page):
    await _theme(page, "classic", "light")
    search = page.get_by_role("textbox", name="搜索插件...", exact=True)
    # 用公开可访问名称定位齿轮，不能依赖按钮包含可见文字。
    settings = page.get_by_role("button", name="插件设置", exact=True)
    assert await settings.inner_text() == ""
    assert await settings.get_attribute("title") == "插件设置"
    await settings.hover()
    await search.fill("worker_development")
    assert await page.locator(".plugin-row").count() == 1
    await search.fill("")
    assert await page.locator(".plugin-row").count() == 20
    await page.locator(".plugins-filters").get_by_text("已启用", exact=True).click()
    await page.locator(".plugins-empty").wait_for()
    assert await page.locator(".plugin-row").count() == 0
    await page.locator(".plugins-filters").get_by_text("已禁用", exact=True).click()
    assert await page.locator(".plugin-row").count() == 20
    await page.locator(".plugins-filters").get_by_text("全部", exact=True).click()
    await settings.focus()
    await page.keyboard.press("Enter")
    await page.locator(".plugin-settings-center").wait_for()
    await page.keyboard.press("Escape")
    await page.locator(".plugin-settings-center").wait_for(state="hidden")
    assert await settings.evaluate("el=>el===document.activeElement")
    await page.keyboard.press("Tab")
    install = page.get_by_role("button", name="安装插件", exact=True)
    assert await install.evaluate("el=>el===document.activeElement")
    await page.keyboard.press("Enter")
    await page.locator(".n-dropdown-menu").get_by_text("安装文件", exact=True).wait_for()
    assert await page.locator(".n-dropdown-menu").get_by_text("安装目录", exact=True).is_visible()
    await page.keyboard.press("Escape")
    await settings.focus()
    await page.keyboard.press("Shift+Tab")
    assert await page.locator(".plugins-filters").evaluate("el=>el.contains(document.activeElement)")


async def _image_backgrounds(page, output):
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
            await _geometry(page)
            await page.screenshot(path=str(output / f"toolbar-image-{skin}-{mode}.png"))


async def main():
    if sys.platform != "win32":
        raise RuntimeError("Native toolbar acceptance requires Windows")
    root = Path(__file__).resolve().parents[1]
    output = root / "output/playwright/plugin-toolbar" / uuid4().hex[:8]
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
            warning = page.get_by_role("dialog").get_by_role("button", name="确定", exact=True)
            await page.add_locator_handler(warning, lambda locator: locator.click())
            await page.evaluate("location.hash='#/more/plugins'")
            await page.locator(".plugin-row").first.wait_for()
            await page.screenshot(path=str(output / "toolbar-initial.png"))
            await _geometry(page)
            records = [] if "--interactions-only" in sys.argv else await _layouts(page, process, output)
            await _interactions(page)
            await _image_backgrounds(page, output)
            await _theme(page, "classic", "light")
            await page.get_by_role("textbox", name="搜索插件...", exact=True).fill("worker_")
            await page.screenshot(path=str(output / "toolbar-final.png"))
            (output / "result.json").write_text(
                json.dumps(
                    {"passed": True, "records": records, "image_backgrounds": 4, "interactions": True},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"Toolbar: {len(records)} layout/locale records, 4 image backgrounds and interactions passed",
                flush=True,
            )
            await browser.close()
    except Exception:
        if "page" in locals():
            await page.screenshot(path=str(output / "failure.png"))
            print(await page.get_by_role("dialog").all_text_contents(), flush=True)
        raise
    finally:
        _shutdown(process, server, log_file)


if __name__ == "__main__":
    asyncio.run(main())
