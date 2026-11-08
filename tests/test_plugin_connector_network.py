from __future__ import annotations

import json
from pathlib import Path

from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.worker_service import PluginWorkerService


def test_real_external_onlinegame_and_optional_qomicex_start_independently(tmp_path: Path) -> None:
    sources = Path(__file__).resolve().parents[1] / "external_plugins"
    pages = []
    service = PluginWorkerService(tmp_path / "data", slot_sink=lambda name, payload: pages.append((name, payload)))
    try:
        for directory in ("ecl_onlinegame", "qomicex_compatible"):
            source = sources / directory
            metadata = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
            assert not metadata.get("dependencies")
            service.register(metadata, source, "directory")
            view = service.permissions(metadata["name"])
            decisions = tuple(
                PermissionDecision(
                    capability=item["capability"], decision="allow" if item["capability"] == "ui.inject" else "deny"
                )
                for item in metadata["permissions"]
            )
            service.update_permissions(
                metadata["name"],
                view["content_digest"],
                view["revision"],
                decisions,
                PermissionManagementCaller.main_webview,
            )
            service.enable(metadata["name"])
            if directory == "ecl_onlinegame":
                assert service.broker(metadata["name"]).call_command("status", {})["result"]["mode"] == "idle"
                assert any(payload["kind"] == "page" for _, payload in pages)
        service.stop("ECL-OnlineGame")
        result = service.broker("Qomicex-Compatible").call_command(
            "onlinegame.extension.v1", {"payload": {"event": "describe"}}
        )
        assert result["result"]["version"] == 1
        assert len(result["result"]["protocols"]) == 4
    finally:
        service.close()
