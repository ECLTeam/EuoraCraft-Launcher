# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原子文件写入：临时文件落地与替换重试。
#
# 公开接口：
#   - atomic_write_bytes(path, data) -> None — 通过同目录临时文件实现原子替换文件内容。
#   - atomic_write_text(path, data, encoding=…) -> None — 以原子替换方式写入文本文件。
# ============================================================

from __future__ import annotations

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


__all__ = ["atomic_write_bytes", "atomic_write_text"]
