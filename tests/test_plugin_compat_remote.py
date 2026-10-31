# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证第三方实例来源的准入拒绝与元数据净化边界。
#
# 公开接口：
#   - test_sanitize_keeps_bounded_fields() -> None
#   - test_sanitize_limits_fields_and_text() -> None
#   - test_sanitize_rejects_missing_fields(payload) -> None
#   - test_compat_registration_requires_game_grant(request) -> None
# ============================================================

from __future__ import annotations

from time import monotonic
from uuid import uuid4

import pytest

from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.instance_compat import (
    limit_metadata_fields,
    limit_metadata_text,
    limit_metadata_warnings,
    sanitize_external_metadata,
)
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture


def test_sanitize_keeps_bounded_fields() -> None:
    metadata = sanitize_external_metadata(
        {"fields": {"loader": "fabric", "mc": "1.21.1"}, "warnings": ["索引缺失"]},
        "demo",
    )
    assert metadata is not None
    assert metadata.source == "demo"
    assert metadata.fields == {"loader": "fabric", "mc": "1.21.1"}
    assert metadata.warnings == ["索引缺失"]


def test_sanitize_limits_fields_and_text() -> None:
    metadata = sanitize_external_metadata(
        {
            "fields": {f"k{index}": "v" * (limit_metadata_text + 20) for index in range(limit_metadata_fields + 5)},
            "warnings": ["w"] * (limit_metadata_warnings + 4),
        },
        "demo",
    )
    assert metadata is not None
    assert len(metadata.fields) == limit_metadata_fields
    assert len(next(iter(metadata.fields.values()))) == limit_metadata_text
    assert len(metadata.warnings) == limit_metadata_warnings


@pytest.mark.parametrize("payload", [None, [], {"fields": "text"}, {"fields": {}}])
def test_sanitize_rejects_missing_fields(payload: object) -> None:
    assert sanitize_external_metadata(payload, "demo") is None


def test_compat_registration_requires_game_grant(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    with pytest.raises(ProtocolError) as error:
        broker.handle(
            RpcRequest(
                "compat.register",
                {"source": "demo", "title": "示例", "callback_ref": "a" * 32},
                monotonic() + 5,
                uuid4().hex,
            )
        )
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"
