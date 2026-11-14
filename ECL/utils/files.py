# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原子文件写入与目录删除：临时文件落地、替换重试与只读文件清理。
#
# 公开接口：
#   - class AtomicWritePolicy — Windows 短暂占用时的替换重试延迟序列。
#   - atomic_write_bytes(path, data) -> None — 通过同目录临时文件实现原子替换文件内容。
#   - atomic_write_text(path, data, encoding=…) -> None — 以原子替换方式写入文本文件。
#   - class RemoveTreePolicy — 递归删除目录时的占用重试延迟序列。
#   - remove_tree(path) -> None — 递归删除目录，必要时清除只读属性并重试。
# ============================================================

from __future__ import annotations

import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from time import sleep
from uuid import uuid4


class AtomicWritePolicy:
    """
    原子替换文件时的 Windows 短暂占用重试策略。

    延迟序列用于兼容杀毒软件和索引器短暂占用刚写入的文件。
    """

    windows_replace_retry_delays: tuple[float, ...] = (0.02, 0.05, 0.1, 0.2)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """
    通过同目录临时文件实现原子替换文件内容。

    :param path: 需要写入的文件路径
    :param data: 待持久化的字节数据
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        for delay in (*AtomicWritePolicy.windows_replace_retry_delays, None):
            try:
                temporary.replace(destination)
                break
            except PermissionError:
                if delay is None:
                    raise
                # Windows 上杀毒软件、索引器或另一条刚结束的替换操作可能短暂占用目标。
                # 临时文件仍在同一目录，重试不会破坏原子替换语义。
                sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: str | Path, data: str, encoding: str = "utf-8") -> None:
    """
    以原子替换方式写入文本文件。

    :param path: 要原子替换的文件路径
    :param data: 要持久化的文本
    :param encoding: 文本编码
    """
    atomic_write_bytes(path, data.encode(encoding))


class RemoveTreePolicy:
    """
    递归删除目录时的只读属性与短暂占用处理策略。

    Windows 上从源码仓库复制来的插件目录带有 Git 只读对象文件，直接删除会被
    系统拒绝；杀毒软件和索引器也可能短暂占用刚释放的文件，因此先清除只读
    属性，再按延迟序列重试。
    """

    retry_delays: tuple[float, ...] = (0.05, 0.1, 0.2, 0.4, 0.8)


def _clear_readonly_and_retry(action: Callable[[str], None], target: str, _error: object) -> None:
    """
    清除只读属性后重试同一次删除动作。

    :param action: 标准库回调中失败的删除函数
    :param target: 失败的路径
    :param _error: 标准库提供的异常或异常三元组，此处不区分
    """
    Path(target).chmod(stat.S_IWRITE)
    action(target)


def remove_tree(path: str | Path) -> None:
    """
    递归删除目录树，必要时清除只读属性并按延迟序列重试。

    与 `shutil.rmtree` 的区别只在失败处理：仍有占用时按策略重试，路径已消失
    视为成功；重试用尽后原样抛出 `OSError`，由调用方保留恢复日志或报告失败。

    :param path: 待删除的目录
    :raises OSError: 全部重试用尽后仍无法删除时抛出
    """
    target = Path(path)
    for delay in (*RemoveTreePolicy.retry_delays, None):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=_clear_readonly_and_retry)
            else:
                shutil.rmtree(target, onerror=_clear_readonly_and_retry)
            return
        except OSError:
            # 目录已被其他路径删掉时视为成功，避免把并发删除报成失败。
            if not target.exists():
                return
            if delay is None:
                raise
            sleep(delay)


__all__ = ["AtomicWritePolicy", "RemoveTreePolicy", "atomic_write_bytes", "atomic_write_text", "remove_tree"]
