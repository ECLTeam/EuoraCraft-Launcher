from __future__ import annotations

import asyncio
import ctypes
import json
from ctypes import wintypes

import psutil


async def check_network_permission_layout(channel, process):
    sequence = 30000

    async def evaluate(expression):
        nonlocal sequence
        sequence += 1
        await channel.send(
            json.dumps(
                {
                    "id": sequence,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
                }
            )
        )
        async with asyncio.timeout(15):
            while True:
                reply = json.loads(await channel.recv())
                if reply.get("id") == sequence:
                    assert "exceptionDetails" not in reply["result"], reply
                    return reply["result"]["result"].get("value")

    async def wait(expression):
        async with asyncio.timeout(15):
            while not await evaluate(expression):
                await asyncio.sleep(0.05)

    own_pids = {process.pid, *(child.pid for child in psutil.Process(process.pid).children(recursive=True))}
    user = ctypes.WinDLL("user32", use_last_error=True)
    process_id = wintypes.DWORD()
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.IsWindowVisible.argtypes = [wintypes.HWND]

    @callback_type
    def callback(handle, _):
        user.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        if process_id.value in own_pids and user.IsWindowVisible(handle) and user.GetWindowTextLengthW(handle):
            windows.append(handle)
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(callback, 0)
    assert len(windows) == 1, windows
    user.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    assert user.SetWindowPos(windows[0], None, 20, 20, 960, 600, 0x14)
    try:
        await wait("!!document.getElementById('app')?.__vue_app__")
        await evaluate(
            "document.getElementById('app').__vue_app__.config.globalProperties.$i18n.locale='de-DE';location.hash='#/more/plugins';true"
        )
        await wait("!!document.querySelector('.plugin-row .plugin-actions button')")
        await evaluate("document.querySelector('.plugin-row .plugin-actions button').click();true")
        await wait("!!document.querySelector('.permission-origins')")
        await wait("document.querySelector('.permission-origins').getBoundingClientRect().width>0")
        await wait(
            "document.querySelector('.permission-origins').closest('.modal-overlay').getAttribute('aria-modal')==='true'"
        )
        await evaluate("""(async()=>{
            document.querySelector('.permission-origins').scrollIntoView({block:'center',behavior:'instant'});
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
            return true;
        })()""")
        result = await evaluate("""(()=>{
            const sites=document.querySelector('.permission-origins');
            const row=sites.closest('.permission-row');
            const box=row.querySelector('.n-checkbox');
            const label=box.querySelector('.n-checkbox__label');
            const text=[...label.childNodes].find(node=>node.nodeType===3&&node.textContent.trim());
            const range=document.createRange();range.selectNodeContents(text);
            const fragments=[...range.getClientRects()].filter(rect=>rect.width>0);
            const footer=row.closest('.modal-container').querySelector('.modal-footer').getBoundingClientRect();
            return {sites:sites.children.length,lines:fragments.length,footer:footer.bottom<=innerHeight+1,
                noOverlap:row.querySelector('.permission-label').getBoundingClientRect().right<=box.getBoundingClientRect().left+1}
        })()""")
        assert result == {"sites": 16, "lines": 1, "footer": True, "noOverlap": True}, result
        await evaluate(
            "document.querySelector('.plugin-permissions').closest('.modal-container').querySelector('.modal-header button').click();true"
        )
        await wait(
            "!document.querySelector('.permission-origins') || document.querySelector('.permission-origins').getBoundingClientRect().width===0"
        )
    finally:
        await evaluate("document.getElementById('app').__vue_app__.config.globalProperties.$i18n.locale='zh-CN';true")
        assert user.SetWindowPos(windows[0], None, 20, 20, 1200, 800, 0x14)
