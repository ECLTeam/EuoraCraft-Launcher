# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证崩溃分析回调的准入拒绝与返回值净化边界。
#
# 公开接口：
#   - test_sanitize_keeps_allowed_fields_only() -> None
#   - test_sanitize_truncates_text_and_limits_reasons() -> None
#   - test_sanitize_rejects_non_dict() -> None
#   - test_crash_registration_requires_game_grant(request) -> None
# ============================================================

from __future__ import annotations

from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.crash_extensions import limit_reason_text, limit_reasons, sanitize_extension_result
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture


def test_sanitize_keeps_allowed_fields_only() -> None:
    result = sanitize_extension_result(
        {
            "summary": "内存不足",
            "reasons": [{"title": "OOM", "detail": "堆溢出", "unknown": "丢弃"}],
            "reasons_raw": "丢弃",
            "verdict": "覆盖宿主结论",
        }
    )
    assert result == {"summary": "内存不足", "reasons": [{"title": "OOM", "detail": "堆溢出"}]}


def test_sanitize_truncates_text_and_limits_reasons() -> None:
    result = sanitize_extension_result(
        {
            "summary": "x" * (limit_reason_text + 100),
            "reasons": [{"title": "t" * (limit_reason_text + 10)} for _ in range(limit_reasons + 5)],
        }
    )
    assert len(result["summary"]) == limit_reason_text
    assert len(result["reasons"]) == limit_reasons
    assert len(result["reasons"][0]["title"]) == limit_reason_text


@pytest.mark.parametrize("payload", [None, "text", 3, ["a"]])
def test_sanitize_rejects_non_dict(payload: object) -> None:
    assert sanitize_extension_result(payload) == {}


def test_crash_registration_requires_game_grant(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        broker.handle(RpcRequest("crash.register", {"callback_ref": "a" * 32}, monotonic() + 5, uuid4().hex))
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"
