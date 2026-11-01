from __future__ import annotations

import os
import socket
import struct

import pytest

from ECL.plugin_api import _posix_transport as transport
from ECL.plugin_api.contracts import WireMessage


@pytest.fixture
def channel(monkeypatch):
    reader, writer = socket.socketpair()
    # Windows 也使用真实流 socket 验证帧解析；系统凭据由原生 POSIX 套件单独验证。
    monkeypatch.setattr(transport.socket, "AF_UNIX", reader.family, raising=False)
    monkeypatch.setattr(transport.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(transport, "_credentials", lambda _: (os.getpid(), 1000))
    channel = transport.UnixChannel(reader)
    try:
        yield channel, writer
    finally:
        channel.close()
        writer.close()


def test_complete_buffered_frame_survives_receiver_descheduling(channel, monkeypatch):
    incoming, writer = channel
    content = WireMessage("hello", {"text": "完整消息"}).encode()
    writer.sendall(struct.pack("!I", len(content)) + content)
    original_ready = incoming._ready

    def delayed(*, writing, deadline):
        original_ready(writing=writing, deadline=deadline)
        # 模拟数据就绪后宿主线程被桌面工作延迟；数据已经完整到达内核缓冲区。
        monkeypatch.setattr(transport, "monotonic", lambda: deadline + 1)

    monkeypatch.setattr(incoming, "_ready", delayed)
    assert incoming.receive(0.1).params == {"text": "完整消息"}
