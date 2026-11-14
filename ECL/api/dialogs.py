# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原生选择对话框的进程内独占槽位，保证同一时刻只弹出一个对话框。
#
# 公开接口：
#   - class NativeDialogSlot — 独占槽位；已被占用时拒绝新的对话框请求。
#   - native_dialog_slot — 全进程共享的槽位实例。
# ============================================================

from __future__ import annotations


class NativeDialogSlot:
    """
    记录原生选择对话框的占用状态。

    系统选择对话框应当表现为模态：用户在其打开期间再次触发选择只能是无效操作，
    不能再叠加一个对话框。槽位只在事件循环线程内进出，占用与释放之间没有其他
    协程能观测到中间状态，因此使用普通布尔标记即可，不需要额外加锁。
    """

    def __init__(self) -> None:
        self._is_open = False

    @property
    def is_open(self) -> bool:
        """
        返回当前是否已有原生对话框打开。

        :return: 已占用返回 True
        """
        return self._is_open

    def try_acquire(self) -> bool:
        """
        尝试占用槽位。

        :return: 占用成功返回 True；已有对话框打开时返回 False，调用方不应再弹窗
        """
        if self._is_open:
            return False
        self._is_open = True
        return True

    def release(self) -> None:
        """
        释放槽位；重复释放保持未占用状态。
        """
        self._is_open = False


native_dialog_slot = NativeDialogSlot()
