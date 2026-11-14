# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证原生选择对话框的进程内独占行为。
# ============================================================

from __future__ import annotations

import asyncio
import logging
from threading import Event
from types import SimpleNamespace

import pytest

from ECL.api import files as files_module
from ECL.api.dialogs import NativeDialogSlot, native_dialog_slot
from ECL.api.files import FileHandlers


@pytest.fixture(autouse=True)
def reset_dialog_slot():
    """每个用例前后清空独占槽位，避免用例失败时把占用状态带给后续用例。"""
    native_dialog_slot.release()
    yield
    native_dialog_slot.release()


def _dialog_extension(opened: Event, release: Event, opened_titles: list[str], *, fail: bool = False):
    """构造记录调用次数的原生对话框替身，进入后阻塞到用例放行。"""

    class _Dialog:
        def blocking_pick_folder(self, **options):
            opened_titles.append(str(options.get("set_title") or ""))
            opened.set()
            assert release.wait(timeout=10), "对话框未被放行"
            if fail:
                raise OSError("对话框失败")
            return "C:/picked"

    class _Extension:
        @staticmethod
        def file(_webview):
            return _Dialog()

    return _Extension


def _handler() -> FileHandlers:
    handler = object.__new__(FileHandlers)
    handler._webview = object()
    handler.logger = logging.getLogger("test.native_dialog")
    handler.events = SimpleNamespace(emit=lambda *args, **kwargs: None)
    return handler


async def test_concurrent_select_directory_opens_a_single_dialog(monkeypatch) -> None:
    """
    对话框打开期间的重复请求不再弹窗，按取消返回空路径。

    系统选择对话框应当是模态的；用户连点“浏览”只能得到一个对话框，第二次请求
    直接返回空路径，前端据此保留原值。
    """
    opened = Event()
    release = Event()
    opened_titles: list[str] = []
    monkeypatch.setattr(files_module, "DialogExt", _dialog_extension(opened, release, opened_titles))
    handler = _handler()

    first = asyncio.create_task(handler.select_directory({}))
    assert await asyncio.to_thread(opened.wait, 10), "第一个对话框没有打开"
    assert native_dialog_slot.is_open is True

    second = await handler.select_directory({})

    release.set()
    first_result = await first

    assert opened_titles == ["选择游戏目录"]
    assert second == {"success": True, "data": {"path": ""}}
    assert first_result == {"success": True, "data": {"path": "C:/picked"}}
    assert native_dialog_slot.is_open is False


async def test_dialog_failure_releases_the_slot(monkeypatch) -> None:
    """
    对话框抛错时槽位必须释放，否则后续所有选择都会退化为取消。
    """
    opened = Event()
    release = Event()
    opened_titles: list[str] = []
    monkeypatch.setattr(files_module, "DialogExt", _dialog_extension(opened, release, opened_titles, fail=True))
    handler = _handler()

    release.set()
    failed = await handler.select_directory({})

    assert failed["success"] is False
    assert native_dialog_slot.is_open is False

    monkeypatch.setattr(files_module, "DialogExt", _dialog_extension(Event(), release, opened_titles))
    recovered = await handler.select_directory({})
    assert recovered == {"success": True, "data": {"path": "C:/picked"}}
    assert opened_titles == ["选择游戏目录", "选择游戏目录"]


def test_slot_release_is_idempotent() -> None:
    """重复释放不会让槽位进入其他状态。"""
    slot = NativeDialogSlot()

    assert slot.try_acquire() is True
    assert slot.try_acquire() is False
    slot.release()
    slot.release()

    assert slot.is_open is False
    assert slot.try_acquire() is True
