# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：用真实 Worker 进程验证界面注入的授权、转发与总开关。
#
# 公开接口：
#   - test_real_worker_injects_slots_and_clears_on_stop(tmp_path) -> None
#   - test_master_switch_prevents_worker_injection(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.worker_service import PluginWorkerService

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/plugins/worker_ui_slot"


def _prepare(tmp_path: Path, *, allow: bool = True) -> tuple[PluginWorkerService, dict, list[tuple[str, dict]]]:
    received: list[tuple[str, dict]] = []
    service = PluginWorkerService(
        tmp_path / "data",
        slot_sink=lambda name, payload: received.append((name, payload)),
        ui_injection_allowed=lambda: allow,
    )
    code = tmp_path / "code"
    code.mkdir()
    metadata = json.loads((EXAMPLE / "plugin.json").read_bytes())
    (code / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (code / "main.py").write_text((EXAMPLE / "main.py").read_text(encoding="utf-8"), encoding="utf-8")
    service.register(metadata, code, "directory")
    view = service.permissions(metadata["name"])
    service.update_permissions(
        metadata["name"],
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="ui.inject", decision="allow"),),
        PermissionManagementCaller.main_webview,
    )
    return service, metadata, received


def test_real_worker_injects_slots_and_clears_on_stop(tmp_path: Path) -> None:
    service, metadata, received = _prepare(tmp_path)
    try:
        service.enable(metadata["name"])
        kinds = [payload["kind"] for _name, payload in received]
        assert kinds == ["html", "vue"], received
        html = received[0][1]
        assert html["slot_id"] == "plugin-slot-content-bottom"
        assert "来自插件的 HTML 片段" in html["html"]
        vue = received[1][1]
        assert vue["component_name"] == "DemoSlotCard"
        assert "来自插件的 Vue 组件" in vue["script"]
    finally:
        service.stop(metadata["name"])
    assert received[-1] == (metadata["name"], {"kind": "clear"})


def test_master_switch_prevents_worker_injection(tmp_path: Path) -> None:
    service, metadata, received = _prepare(tmp_path, allow=False)
    try:
        # 拦截发生在 Worker 的加载钩子里，宿主按执行失败上报，不冒充分具体原因。
        with pytest.raises(ProtocolError):
            service.enable(metadata["name"])
        assert [payload for _name, payload in received if payload.get("kind") != "clear"] == []
    finally:
        service.close()
