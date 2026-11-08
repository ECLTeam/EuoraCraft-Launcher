# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：绑定扩展运行及房间，对有界 JSON 进行分块传输。
#
# 公开接口：
#   - class JsonTransfers — 插件业务接口。
# ============================================================

from __future__ import annotations

import base64
import json
from threading import RLock
from time import monotonic
from uuid import uuid4

from ECL.plugin_api.contracts import _json_value


class JsonTransfers:
    """
    在 16 KiB 控制命令上分块传递有界图片和列表，引用绑定当前扩展运行及房间。
    """

    max_bytes = 1024 * 1024

    def __init__(self) -> None:
        self._values: dict[str, tuple[str, float, bytearray, int]] = {}
        self._lock = RLock()

    def handle(self, payload: dict, dispatch) -> dict:
        """
        执行分块控制或完整请求；过期缓存和已完成上传立即释放。
        """
        session = payload["session"]
        event = payload.get("event")
        with self._lock:
            self._values = {key: value for key, value in self._values.items() if monotonic() - value[1] < 30}
            if event == "transfer_put":
                return self._put(session, payload)
            if event == "transfer_get":
                owner, _created, content, _total = self._values[payload["transfer_ref"]]
                if owner != session or type(payload.get("offset")) is not int:
                    raise ValueError("传输引用无效")
                offset = payload["offset"]
                if not 0 <= offset <= len(content):
                    raise ValueError("传输位置无效")
                block = bytes(content[offset : offset + 8192])
                return {
                    "content": base64.b64encode(block).decode(),
                    "next_offset": offset + len(block) if offset + len(block) < len(content) else None,
                }
            if event == "transfer_execute":
                owner, _created, content, total = self._values.pop(payload["transfer_ref"])
                if owner != session or len(content) != total:
                    raise ValueError("传输未完整提交")
                decoded = _json_value(json.loads(content))
                if (
                    not isinstance(decoded, dict)
                    or decoded.get("session") != session
                    or decoded.get("binding") != payload.get("binding")
                ):
                    raise ValueError("传输会话不匹配")
                payload = decoded
        result = dispatch(payload)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) <= 12000:
            return result
        if len(encoded) > self.max_bytes:
            raise ValueError("扩展结果超过大小上限")
        with self._lock:
            self._check_capacity(len(encoded))
            reference = uuid4().hex
            self._values[reference] = (session, monotonic(), bytearray(encoded), len(encoded))
        return {"transfer_ref": reference, "transfer_size": len(encoded)}

    def _check_capacity(self, size: int, *, new: bool = True) -> None:
        if (new and len(self._values) >= 8) or sum(
            len(value[2]) for value in self._values.values()
        ) + size > 2 * self.max_bytes:
            raise ValueError("扩展传输缓存已满")

    def _put(self, session: str, payload: dict) -> dict:
        chunk = base64.b64decode(payload["content"], validate=True)
        total, offset = payload.get("total"), payload.get("offset")
        if type(total) is not int or not 0 < total <= self.max_bytes or len(chunk) > 8192 or type(offset) is not int:
            raise ValueError("传输分块无效")
        reference = payload.get("transfer_ref")
        if reference is None:
            self._check_capacity(len(chunk))
            if offset != 0:
                raise ValueError("首块必须从头开始")
            reference = uuid4().hex
            self._values[reference] = (session, monotonic(), bytearray(), total)
        owner, _created, content, expected = self._values[reference]
        if owner != session or expected != total or offset != len(content) or offset + len(chunk) > total:
            raise ValueError("传输块顺序或身份无效")
        self._check_capacity(len(chunk), new=False)
        content.extend(chunk)
        return {"transfer_ref": reference}
