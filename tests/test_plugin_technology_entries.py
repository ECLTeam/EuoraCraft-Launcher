# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证关于页插件技术栈条目的声明校验、注册边界与会话失效行为。
#
# 公开接口：
#   - test_entry_definition_validates_fields() -> None
#   - test_register_and_list_technology_entries(panel_setup) -> None
#   - test_register_rejects_invalid_entries(panel_setup) -> None
#   - test_register_rejects_over_limit_entries(panel_setup) -> None
#   - test_view_fails_after_session_close(panel_setup) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.run_models import PluginContentBinding
from ECL.plugins.run_store import PluginRunError, PluginRunStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.models import TechnologyEntryDefinition


@pytest.fixture
def panel_setup(tmp_path: Path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("value = 1", encoding="utf-8")
    binding = PluginContentBinding.capture(root, "windows-x86_64-cp311")
    store = PluginRunStore()
    session = store.open_session(store.new_installation_id(), binding)

    def invoke(_method, params, _timeout):
        return {"ok": True, "data": {"value": {}}}

    broker = PluginHostBroker(session, tmp_path, invoke, run_state=lambda: "enabled")
    yield broker, session
    broker.close()
    session.close()


def request(broker: PluginHostBroker, method: str, params: dict, key: str | None = None) -> dict:
    return broker.handle(RpcRequest(method, params, monotonic() + 5, key or uuid4().hex))


def entry(key: str = "easytier", **extra) -> dict:
    return {
        "key": key,
        "name": "EasyTier",
        "description": "去中心化组网工具",
        "url": "https://github.com/EasyTier/EasyTier",
        **extra,
    }


def test_entry_definition_validates_fields() -> None:
    """合法条目放行，许可证去除首尾空白；非 http(s) 链接与超长字段拒绝。"""
    definition = TechnologyEntryDefinition.model_validate(entry(license="  LGPL-3.0  "))
    assert definition.license == "LGPL-3.0"
    assert definition.description == "去中心化组网工具"
    with pytest.raises(ValidationError):
        TechnologyEntryDefinition.model_validate(entry(url="ftp://example.com/tool"))
    with pytest.raises(ValidationError):
        TechnologyEntryDefinition.model_validate(entry(url="not-a-url"))
    with pytest.raises(ValidationError):
        TechnologyEntryDefinition.model_validate(entry(name=""))
    with pytest.raises(ValidationError):
        TechnologyEntryDefinition.model_validate(entry(key="9bad-key"))
    with pytest.raises(ValidationError):
        TechnologyEntryDefinition.model_validate(entry(unknown_field=1))


def test_register_and_list_technology_entries(panel_setup) -> None:
    broker, _session = panel_setup
    result = request(broker, "technology.register", {"entries": [entry(), entry(key="easytier_pyo3")]})
    assert result == {"registered_count": 2}
    listed = request(broker, "technology.list", {})["entries"]
    assert [item["key"] for item in listed] == ["easytier", "easytier_pyo3"]
    assert broker.technology_entries_view()[0]["url"] == "https://github.com/EasyTier/EasyTier"


def test_register_rejects_invalid_entries(panel_setup) -> None:
    broker, _session = panel_setup
    with pytest.raises(ProtocolError) as error:
        request(broker, "technology.register", {"entries": [entry(), entry()]})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    request(broker, "technology.register", {"entries": [entry()]})
    with pytest.raises(ProtocolError) as error:
        request(broker, "technology.register", {"entries": [entry()]})
    assert error.value.error_code == "PLUGIN_REGISTRATION_CONFLICT"
    with pytest.raises(ProtocolError) as error:
        request(broker, "technology.register", {"entries": "not-a-list"})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    with pytest.raises(ProtocolError) as error:
        request(broker, "technology.register", {"extra": 1})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"


def test_register_rejects_over_limit_entries(panel_setup) -> None:
    """单次注册超过 16 条拒绝，恰好 16 条放行。"""
    broker, _session = panel_setup
    with pytest.raises(ProtocolError) as error:
        request(broker, "technology.register", {"entries": [entry(key=f"key_{index}") for index in range(17)]})
    assert error.value.error_code == "PLUGIN_SCHEMA_INVALID"
    request(broker, "technology.register", {"entries": [entry(key=f"key_{index}") for index in range(16)]})
    assert len(broker.technology_entries_view()) == 16


def test_view_fails_after_session_close(panel_setup) -> None:
    """插件禁用或会话关闭后条目随会话失效，不再对管理页可见。"""
    broker, session = panel_setup
    request(broker, "technology.register", {"entries": [entry()]})
    session.close()
    with pytest.raises((PluginRunError, ProtocolError)):
        broker.technology_entries_view()
