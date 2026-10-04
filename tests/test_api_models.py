# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 api_models 模块的自动化测试。
#
# 公开接口：
#   - test_request_models_accept_valid_payloads() -> None
#   - test_request_models_reject_invalid_payloads(model, payload) -> None
#   - test_launch_request_accepts_memory_lock_and_priority_options() -> None
#   - test_launch_request_rejects_invalid_process_priority() -> None
#   - test_normalize_process_priority_falls_back_to_normal() -> None
#   - test_instance_repair_uses_configured_preferred_download_source() -> None
#   - test_schematic_preview_accepts_frontend_payload_without_enabled() -> None
#   - test_schematic_material_manifest_validates_and_routes_payload() -> None
#   - test_request_schema_contains_every_consolidated_typed_command() -> None
#   - test_invalid_ipc_payload_uses_stable_error_code() -> None
#   - test_version_stats_ipc_validates_and_forwards_target() -> None
# ============================================================

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ECL.api.game import GameHandlers
from ECL.api.models import (
    GameVersionRequest,
    InstallRequest,
    LaunchRequest,
    LoaderCatalogRequest,
    SchematicMaterialManifestRequest,
    SchematicPreviewRequest,
    SettingsQuery,
    WardrobeImportRequest,
    request_schemas,
)
from ECL.api.workspace import WorkspaceHandlers


def test_request_models_accept_valid_payloads() -> None:
    query = SettingsQuery.model_validate({"sections": ["launcher", "game"]})
    install = InstallRequest.model_validate(
        {"version_id": "1.21.1", "game_path": ".minecraft", "loader_type": "fabric"}
    )
    launch = LaunchRequest.model_validate({"version_id": "1.21.1", "game_path": ".minecraft"})
    version_stats = GameVersionRequest.model_validate({"version_id": "1.21.1", "game_path": ".minecraft"})
    wardrobe = WardrobeImportRequest.model_validate({"path": "skin.png", "kind": "skin", "model": "slim"})

    assert query.sections == ["launcher", "game"]
    assert install.loader_type.value == "fabric"
    assert launch.memory == 4096
    assert version_stats.version_id == "1.21.1"
    assert wardrobe.model.value == "slim"


@pytest.mark.parametrize(
    "model,payload",
    [
        (LoaderCatalogRequest, {"loader": "unknown", "game_version": "1.21.1"}),
        (LoaderCatalogRequest, {"loader": "fabric", "game_version": "1.21.1", "source": "mirror"}),
        (InstallRequest, {"game_path": ".minecraft"}),
        (LaunchRequest, {"version_id": "1.21.1", "game_path": ".minecraft", "memory": 1}),
        (LaunchRequest, {"version_id": "1.21.1", "game_path": "bad\0path"}),
        (WardrobeImportRequest, {"path": "cape.png", "kind": "cape", "model": "slim"}),
    ],
)
def test_request_models_reject_invalid_payloads(model, payload) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_launch_request_accepts_memory_lock_and_priority_options() -> None:
    launch = LaunchRequest.model_validate(
        {
            "version_id": "1.21.1",
            "game_path": ".minecraft",
            "lock_memory": True,
            "process_priority": "high",
        }
    )

    assert launch.lock_memory is True
    assert launch.process_priority == "high"


def test_launch_request_rejects_invalid_process_priority() -> None:
    with pytest.raises(ValidationError):
        LaunchRequest.model_validate({"version_id": "1.21.1", "game_path": ".minecraft", "process_priority": "urgent"})


def test_normalize_process_priority_falls_back_to_normal() -> None:
    from ECL.services.game.base import _GameState

    assert _GameState._normalize_process_priority("Above_Normal") == "above_normal"
    assert _GameState._normalize_process_priority("unknown") == "normal"
    assert _GameState._normalize_process_priority(None) == "normal"
    assert _GameState._normalize_process_priority("") == "normal"


@pytest.mark.asyncio
async def test_instance_repair_uses_configured_preferred_download_source() -> None:
    handler = object.__new__(WorkspaceHandlers)
    calls = []
    handler._get_effective_config = lambda: {"download": {"mirror_source": "bmclapi"}}
    handler.game = SimpleNamespace(
        resolve_version_isolation=lambda _game_path, _version_id: False,
        repair_instance_files=lambda game_path, version_id, source: (
            calls.append((game_path, version_id, source)) or {"operationId": "repair-1", "status": "running"}
        ),
    )

    result = await handler.game_instance_files_repair({"game_path": ".minecraft", "version_id": "1.21.1"})

    assert result["success"] is True
    assert calls[0][2] == "bmclapi"


@pytest.mark.asyncio
async def test_schematic_preview_accepts_frontend_payload_without_enabled() -> None:
    handler = object.__new__(WorkspaceHandlers)
    calls = []
    handler.game = SimpleNamespace(
        resolve_version_isolation=lambda _game_path, _version_id: True,
        schematic_preview=lambda game_path, version_id, resource_id, version_isolation: (
            calls.append((game_path, version_id, resource_id, version_isolation))
            or {"type": "schem", "size": [1, 1, 1], "regions": []}
        ),
    )

    response = await handler.game_schematic_preview(
        {
            "game_path": ".minecraft",
            "version_id": "1.21.1",
            "resource_type": "schematic",
            "resource_id": "build.schem",
        }
    )

    assert response["success"] is True
    assert calls[0][1:] == ("1.21.1", "build.schem", True)
    with pytest.raises(ValidationError):
        SchematicPreviewRequest.model_validate(
            {"game_path": ".minecraft", "version_id": "1.21.1", "resource_type": "schematic"}
        )


@pytest.mark.asyncio
async def test_schematic_session_commands_validate_and_route_payloads() -> None:
    handler = object.__new__(WorkspaceHandlers)
    calls: list[tuple[object, ...]] = []
    handler.game = SimpleNamespace(
        resolve_version_isolation=lambda _game_path, _version_id: True,
        schematic_session_open=lambda game_path, version_id, resource_id, isolation: (
            calls.append(("open", game_path, version_id, resource_id, isolation)) or {"sessionId": "a" * 32}
        ),
        schematic_session_chunks=lambda session_id, coords: (
            calls.append(("chunks", session_id, coords)) or {"chunks": []}
        ),
        schematic_session_close=lambda session_id: calls.append(("close", session_id)) or {"closed": True},
    )
    opened = await handler.game_schematic_session_open(
        {
            "game_path": ".minecraft",
            "version_id": "1.21.1",
            "resource_type": "schematic",
            "resource_id": "build.schem",
        }
    )
    chunks = await handler.game_schematic_session_chunks({"session_id": "a" * 32, "coords": [[1, 0, 2]]})
    closed = await handler.game_schematic_session_close({"session_id": "a" * 32})

    assert opened["success"] and chunks["success"] and closed["success"]
    assert calls[0][2:] == ("1.21.1", "build.schem", True)
    assert calls[1] == ("chunks", "a" * 32, [(1, 0, 2)])
    assert calls[2] == ("close", "a" * 32)


@pytest.mark.asyncio
async def test_schematic_material_manifest_validates_and_routes_payload() -> None:
    handler = object.__new__(WorkspaceHandlers)
    calls: list[tuple[object, ...]] = []
    handler.game = SimpleNamespace(
        resolve_version_isolation=lambda _game_path, _version_id: True,
        export_schematic_material_manifest=lambda *args: calls.append(args) or {"path": "C:/materials.json"},
    )
    body = {
        "game_path": ".minecraft",
        "version_id": "1.21.1",
        "session_id": "a" * 32,
        "output_path": "C:/materials.json",
        "output_format": "json",
        "locale": "ja-JP",
        "missing_blocks": ["minecraft:torch"],
    }

    response = await handler.game_schematic_material_manifest_export(body)

    assert response == {"success": True, "data": {"path": "C:/materials.json"}}
    assert calls == [
        (
            Path(".minecraft"),
            "1.21.1",
            "a" * 32,
            Path("C:/materials.json"),
            "json",
            "ja-JP",
            ["minecraft:torch"],
            True,
        )
    ]
    with pytest.raises(ValidationError):
        SchematicMaterialManifestRequest.model_validate({**body, "locale": "fr-FR"})


def test_request_schema_contains_every_consolidated_typed_command() -> None:
    schemas = request_schemas()

    assert set(schemas) == {
        "search_mods",
        "game_version_settings_effective",
        "custom_download_start",
        "select_directory",
        "settings_get",
        "settings_set",
        "frontend_log",
        "window_open",
        "window_focus",
        "window_close",
        "window_update_bounds",
        "game_versions",
        "game_loader_versions",
        "game_scan",
        "game_java_scan",
        "game_install",
        "game_launch",
        "game_uninstall",
        "game_config_get",
        "game_config_set",
        "game_config_patch",
        "game_version_stats",
        "game_version_settings_get",
        "game_version_settings_set",
        "game_instance_profile_get",
        "game_instance_profile_patch",
        "game_instance_profile_reset",
        "game_instance_icon_set",
        "game_instance_pin_order_set",
        "game_instance_categories_upsert",
        "game_instance_categories_delete",
        "game_instance_stop",
        "game_crash_list",
        "game_crash_analyze",
        "game_crash_output",
        "game_crash_export",
        "wardrobe_import",
        "wardrobe_sync_account_skin",
        "wardrobe_update",
        "wardrobe_delete",
        "wardrobe_texture",
        "wardrobe_export",
        "wardrobe_apply_skin",
        "accounts_texture_urls",
        "select_image",
        "skin_avatar_export",
        "select_file",
        "select_save_file",
        "microsoft_reset_skin",
        "microsoft_set_cape",
        "microsoft_reset_cape",
        "connector_host_port",
        "connector_join",
        "connector_kick",
        "connector_search_mc_port",
    }
    assert all(schema["type"] == "object" for schema in schemas.values())


@pytest.mark.asyncio
async def test_invalid_ipc_payload_uses_stable_error_code() -> None:
    handler = object.__new__(GameHandlers)

    response = await handler.game_loader_versions({"loader": "invalid"})

    assert response["success"] is False
    assert response["errorCode"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_version_stats_ipc_validates_and_forwards_target() -> None:
    handler = object.__new__(GameHandlers)
    calls = []
    handler.game = SimpleNamespace(
        get_version_stats=lambda game_path, version_id: (
            calls.append((game_path, version_id))
            or {"launchCount": 1, "lastRunDurationSeconds": 2, "totalRunDurationSeconds": 3}
        )
    )

    response = await handler.game_version_stats({"game_path": ".minecraft", "version_id": "1.21.1"})

    assert response["success"] is True
    assert response["data"]["totalRunDurationSeconds"] == 3
    assert calls[0][1] == "1.21.1"
