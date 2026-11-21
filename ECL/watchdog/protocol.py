# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/EuoraCraft-Launcher/EuoraCraft-Launcher
#
# 文件作用：定义看门狗与宿主之间的管道帧格式，供两侧共享编解码实现。
#
# 公开接口：
#   - class FrameError — 管道帧损坏、超长或在帧中途读到 EOF 时抛出。
#   - encode_frame(value) -> bytes — 编码一帧完整字节。
#   - write_frame(stream, value) -> None — 向二进制流写入并冲刷一帧。
#   - read_frame(stream) -> dict[str, object] | None — 读取一帧；干净 EOF 返回 None。
# ============================================================

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from typing import BinaryIO

# 单帧上限：心跳与就绪帧远小于该值，超限即视为协议错乱。
max_frame_size = 65536


class FrameError(ValueError):
    """
    管道帧不完整或格式非法，接收方无法恢复帧边界。
    """


def encode_frame(value: Mapping[str, object]) -> bytes:
    """
    把控制消息编码为「4 字节大端长度 + 紧凑 JSON」的完整帧。

    :param value: 只含 JSON 兼容类型的消息字典
    :return: 可一次写入底层管道的帧字节
    :raises FrameError: 编码结果超过单帧上限时抛出
    """
    content = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(content) > max_frame_size:
        raise FrameError("看门狗帧内容超过长度上限")
    return struct.pack("!I", len(content)) + content


def write_frame(stream: BinaryIO, value: Mapping[str, object]) -> None:
    """
    向二进制流写入一帧并冲刷，调用方自行处理管道失败。

    :param stream: 对端的二进制可写流
    :param value: 只含 JSON 兼容类型的消息字典
    :raises FrameError: 编码结果超过单帧上限时抛出
    """
    stream.write(encode_frame(value))
    stream.flush()


def _read_exactly(stream: BinaryIO, count: int) -> bytes | None:
    """
    读取精确数量的字节；流边界处干净 EOF 返回 None，中途 EOF 视为帧损坏。

    :param stream: 对端的二进制可读流
    :param count: 需要读取的字节数
    :return: 读到的字节；帧边界处干净 EOF 为 None
    :raises FrameError: 帧中途遇到 EOF 时抛出
    """
    parts: list[bytes] = []
    while sum(len(part) for part in parts) < count:
        part = stream.read(count - sum(len(part) for part in parts))
        if not part:
            if parts:
                raise FrameError("看门狗帧在内容中途遇到 EOF")
            return None
        parts.append(part)
    return b"".join(parts)


def read_frame(stream: BinaryIO) -> dict[str, object] | None:
    """
    从二进制流读取一帧完整控制消息。

    :param stream: 对端的二进制可读流
    :return: 消息字典；对端在帧边界干净关闭时为 None
    :raises FrameError: 长度头非法、帧超长、内容非法或在帧中途遇到 EOF
    """
    header = _read_exactly(stream, 4)
    if header is None:
        return None
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= max_frame_size:
        raise FrameError("看门狗帧长度无效")
    content = _read_exactly(stream, size)
    if content is None:
        raise FrameError("看门狗帧在内容中途遇到 EOF")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise FrameError("看门狗帧内容不是消息对象")
    return value
