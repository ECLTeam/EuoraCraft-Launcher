# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 frontend_api_config 模块的自动化测试。
#
# 公开接口：
#   - class FakeAccounts
#       - list_accounts() -> Any
#       - microsoft_login_config() -> Any
#       - texture_urls(account_id) -> Any
#       - add_offline(username, custom_uuid=…, skin=…) -> Any
#       - default_skins() -> Any
#       - set_offline_skin(account_id, skin) -> Any
#       - add_authlib(server_url, username, password) -> Any
#       - resolve_authlib_server(server_url) -> Any
#       - select_authlib_profile(account_id, profile_id) -> Any
#       - upload_skin(account_id, model, texture) -> Any
#   - class FakePlugins
#       - on_frontend_ready() -> None
#       - list_plugins() -> Any
#       - set_sidebar_state(collapsed) -> None
#   - class FakeWardrobe
#       - list_items() -> Any
#       - read_texture(item_id) -> Any
#       - import_bytes(texture, kind, name, model) -> Any
#   - class FakeHttpResponse
#       - raise_for_status() -> Any
#       - iter_bytes(_chunk_size) -> Any
#   - class FakeHttp
#       - stream(method, url) -> Any
#   - class FakeInfoCard
#       - get_info_card() -> Any
#   - class FakeGame
#       - curseforge_available() -> Any
#       - scan_versions(paths, force, compatibility_options) -> Any
#       - scan_java(user_paths) -> Any
#       - install_version(body, **options) -> Any
#       - launch_instance(body, **options) -> Any
#       - analyze_crash_file(file_path, game_path, version_id) -> Any
#       - get_crash_output(report_id) -> Any
#       - export_crash_report(report_id, output_path=…) -> Any
#   - class FakeWebviewWindow
#       - show() -> None
#       - unminimize() -> None
#       - set_focus() -> None
#       - run_on_main_thread(handler) -> None
#   - test_launcher_config_excludes_runtime_metadata(tmp_path) -> None
#   - test_launcher_info_uses_runtime_metadata(tmp_path) -> None
#   - test_info_card_delegates_to_registered_service(tmp_path) -> None
#   - test_user_agreement_state_is_persisted_by_formal_system_api(tmp_path) -> None
#   - test_java_scan_delegates_to_game_service(tmp_path) -> None
#   - test_scan_versions_delegates_to_registered_service(tmp_path) -> None
#   - test_install_version_delegates_to_game_service_with_runtime_options(tmp_path) -> None
#   - test_launch_instance_delegates_to_game_service_with_settings(tmp_path) -> None
#   - test_crash_report_commands_validate_and_delegate(tmp_path) -> None
#   - test_crash_report_id_rejects_invalid_values(tmp_path) -> None
#   - test_select_save_file_uses_system_dialog_and_normalizes_zip_suffix(tmp_path, monkeypatch) -> None
#   - test_select_save_file_accepts_mod_default_directory_and_filename(tmp_path, monkeypatch) -> None
#   - test_select_save_file_instance_export_uses_mrpack_suffix(tmp_path, monkeypatch) -> None
#   - test_select_file_modpack_purpose_filters_modpack_extensions(tmp_path, monkeypatch) -> None
#   - test_offline_account_delegates_to_registered_service(tmp_path) -> None
#   - test_offline_account_forwards_optional_custom_uuid(tmp_path) -> None
#   - test_offline_account_forwards_optional_skin(tmp_path) -> None
#   - test_offline_default_skins_handler(tmp_path) -> None
#   - test_offline_set_skin_handler(tmp_path) -> None
#   - test_authlib_login_uses_account_manager_and_remembers_server(tmp_path, monkeypatch) -> None
#   - test_authlib_profile_selection_is_forwarded_to_account_manager(tmp_path) -> None
#   - test_authlib_server_url_is_resolved_through_ali(tmp_path) -> None
#   - test_frontend_ready_and_plugin_api_use_registered_framework(tmp_path) -> None
#   - test_adapter_main_window_is_visible_without_native_shadow() -> None
#   - test_new_config_creates_platform_appropriate_window_chrome(...) -> None
#   - test_adapter_main_window_chrome_follows_ui_setting(window_chrome, is_native) -> None
#   - test_adapter_focuses_main_window_once_at_startup_ready(monkeypatch) -> None
#   - test_adapter_startup_focus_failure_does_not_block_startup(monkeypatch, failure) -> None
#   - test_focus_window_restores_and_focuses_webview(tmp_path) -> None
#   - test_emit_to_frontend_stops_after_webview_closed(tmp_path) -> None
#   - test_microsoft_authorization_event_focuses_before_forwarding(tmp_path, monkeypatch) -> None
#   - test_adapter_forwards_launcher_notifications(tmp_path, monkeypatch) -> None
#   - test_sidebar_state_is_forwarded_to_plugins(tmp_path) -> None
#   - test_unexpected_ipc_error_returns_correlated_modal_and_emits_event(tmp_path, monkeypatch) -> None
#   - test_guarded_call_timeout_cancels_slow_operation(tmp_path) -> None
#   - test_guarded_call_without_timeout_runs_to_completion(tmp_path) -> None
#   - test_guarded_call_treats_auth_exception_as_known_error(tmp_path) -> None
#   - test_critical_persistence_error_returns_modal_metadata_and_emits_event(tmp_path, monkeypatch) -> None
#   - test_frontend_ready_pushes_cacheable_development_warning(tmp_path, monkeypatch) -> None
#   - test_important_backend_events_wait_until_frontend_is_ready(tmp_path, monkeypatch) -> None
#   - test_ready_frontend_events_are_marshaled_to_main_thread(tmp_path, monkeypatch) -> None
#   - test_serious_errors_remain_available_until_frontend_acknowledges_them(tmp_path) -> None
#   - test_wardrobe_list_delegates_to_registered_store(tmp_path) -> None
#   - test_wardrobe_sync_downloads_current_account_skin_into_store(tmp_path) -> None
#   - test_wardrobe_apply_skin_uploads_internal_texture_without_base64_body(tmp_path) -> None
#   - test_debug_maintenance_requires_debug_mode(tmp_path) -> None
#   - test_debug_maintenance_schedules_allowed_action(tmp_path) -> None
#   - test_frontend_ready_dispatches_cli_launch_once(tmp_path, monkeypatch) -> None
#   - test_frontend_ready_cli_launch_failure_emits_popup(tmp_path) -> None
# ============================================================

from __future__ import annotations

import asyncio
import json
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytauri_module = ModuleType("pytauri")
pytauri_module.__path__ = []  # type: ignore[attr-defined]
pytauri_module.EventTarget = SimpleNamespace(Any=lambda: object())  # type: ignore[attr-defined]
pytauri_module.Commands = object  # type: ignore[attr-defined]
pytauri_module.Builder = object  # type: ignore[attr-defined]
pytauri_module.Context = object  # type: ignore[attr-defined]
pytauri_module.AppHandle = object  # type: ignore[attr-defined]
pytauri_module.RunEventType = object  # type: ignore[attr-defined]
pytauri_module.RunEvent = SimpleNamespace(Ready=type("FakeReadyEvent", (), {}))  # type: ignore[attr-defined]
pytauri_module.Manager = SimpleNamespace(get_webview_window=lambda *_args: None)  # type: ignore[attr-defined]
pytauri_module.builder_factory = lambda: None  # type: ignore[attr-defined]
pytauri_module.context_factory = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]

pytauri_ffi_module = ModuleType("pytauri.ffi")
pytauri_ffi_module.Emitter = SimpleNamespace(emit_str_to=lambda *args: None)  # type: ignore[attr-defined]

pytauri_ipc_module = ModuleType("pytauri.ipc")
pytauri_ipc_module.WebviewWindow = object  # type: ignore[attr-defined]

pytauri_plugins_dialog_module = ModuleType("pytauri_plugins.dialog")
pytauri_plugins_dialog_module.DialogExt = object  # type: ignore[attr-defined]
pytauri_plugins_dialog_module.init = lambda: None  # type: ignore[attr-defined]

sys.modules["pytauri"] = pytauri_module
sys.modules["pytauri.ffi"] = pytauri_ffi_module
sys.modules["pytauri.ipc"] = pytauri_ipc_module
sys.modules["pytauri_plugins.dialog"] = pytauri_plugins_dialog_module

FrontendApi = import_module("ECL.api").FrontendApi
Adapter = import_module("ECL.adapters.tauri").Adapter
frontend_module = import_module("ECL.api.frontend")
files_module = import_module("ECL.api.files")
ConfigStore = import_module("ECL.utils").ConfigStore
EventBus = import_module("ECL.events").EventBus
AccountError = import_module("ECL.services.accounts").AccountError
LaunchOptions = import_module("ECL.cli").LaunchOptions
command_handlers = import_module("ECL.api.registry").command_handlers
_guarded_call = import_module("ECL.api.bridge")._guarded_call


class FakeAccounts:
    def list_accounts(self):
        account = {
            "id": "microsoft-account",
            "alias": "Player",
            "type": "microsoft",
            "uuid": "profile-id",
            "isCurrent": True,
        }
        return {"accounts": [account], "current": account}

    def microsoft_login_config(self):
        return {"available": True}

    def texture_urls(self, account_id):
        assert account_id == "microsoft-account"
        return {"skinUrl": "https://textures.example.com/skin.png", "skinModel": "slim"}

    def add_offline(self, username, custom_uuid=None, skin=None):
        self.last_offline_input = (username, custom_uuid)
        self.last_offline_skin = skin
        return {"id": "offline-id", "alias": username, "type": "offline", "uuid": "offline-uuid"}

    def default_skins(self):
        return [{"id": "alice", "name": "Alice", "skinUrl": "data:image/png;base64,AAAA"}]

    def set_offline_skin(self, account_id, skin):
        self.last_offline_skin = skin
        return {
            "accounts": [{"id": account_id, "alias": "Steve", "type": "offline", "skin": skin}],
            "current": {"id": account_id},
        }

    def add_authlib(self, server_url, username, password):
        self.last_authlib_input = (server_url, username, password)
        return {
            "id": "authlib-id",
            "alias": "AuthlibPlayer",
            "type": "authlib",
            "uuid": "authlib-uuid",
            "auth_server": server_url,
        }

    def resolve_authlib_server(self, server_url):
        return f"{server_url.rstrip('/')}/api/yggdrasil"

    def select_authlib_profile(self, account_id, profile_id):
        self.last_authlib_profile = (account_id, profile_id)
        return {
            "id": account_id,
            "alias": "SelectedPlayer",
            "type": "authlib",
            "uuid": profile_id,
            "isCurrent": True,
        }

    async def upload_skin(self, account_id, model, texture):
        self.uploaded_skin = (account_id, model, texture)
        return {"id": account_id, "alias": "Player", "type": "microsoft"}


class FakePlugins:
    def __init__(self):
        self.frontend_ready_count = 0
        self.sidebar_states = []

    def on_frontend_ready(self) -> None:
        self.frontend_ready_count += 1

    def list_plugins(self):
        return [{"name": "example", "status": "enabled"}]

    def set_sidebar_state(self, collapsed: bool) -> None:
        self.sidebar_states.append(collapsed)


class FakeWardrobe:
    def list_items(self):
        return []

    def read_texture(self, item_id):
        return (
            {
                "id": item_id,
                "kind": "skin",
                "model": "slim",
                "width": 64,
                "height": 64,
            },
            b"png",
        )

    def import_bytes(self, texture, kind, name, model):
        self.imported = (texture, kind, name, model)
        return (
            {
                "id": "synced-skin",
                "kind": kind,
                "name": name,
                "model": model,
                "width": 64,
                "height": 64,
            },
            False,
        )


class FakeHttpResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    def iter_bytes(self, _chunk_size):
        yield b"downloaded-png"


class FakeHttp:
    def stream(self, method, url):
        assert method == "GET"
        assert url == "https://textures.example.com/skin.png"
        return FakeHttpResponse()


class FakeInfoCard:
    def get_info_card(self):
        return {
            "mode": "rotate",
            "tips": ["测试提示"],
            "announcements": [],
            "welcome": None,
            "interval": 8000,
        }


class FakeGame:
    def read_version_settings(self, game_path, version_id):
        return {}

    def __init__(self):
        self.install_call = None
        self.launch_call = None
        self.requested_paths = None
        self.scan_force = False
        self.compatibility_options = None
        self.crash_call = None

    def curseforge_available(self):
        return True

    def scan_versions(self, paths, *, force=False, compatibility_options=None):
        self.requested_paths = paths
        self.scan_force = force
        self.compatibility_options = compatibility_options
        return [{"id": "1.20.1", "versionId": "1.20.1"}]

    def scan_java(self, user_paths):
        return [
            {
                "path": user_paths[0] if user_paths else "C:/Java/bin/java.exe",
                "version": "21.0.7",
                "major_version": 21,
                "java_type": "OpenJDK",
                "arch": "x64",
                "sources": ["system"],
            }
        ]

    def install_version(self, body, **options):
        self.install_call = (body, options)
        return {
            "taskId": str(body.get("task_id") or "install-task"),
            "versionId": body["version_id"],
            "versionName": body.get("version_name") or body["version_id"],
        }

    async def launch_instance(self, body, **options):
        self.launch_call = (body, options)
        return {
            "instanceId": "instance-id",
            "versionId": body["version_id"],
            "gamePath": str(options["game_path"]),
        }

    def resolve_version_isolation(self, game_path, version_id, requested=None):
        self.isolation_call = (game_path, version_id, requested)
        return True if requested is None else requested

    def open_instance_folder(self, game_path, version_id, folder, version_isolation):
        self.folder_call = (game_path, version_id, folder, version_isolation)
        return {"path": str(game_path / "versions" / version_id / folder)}

    def analyze_crash_file(self, file_path, game_path, version_id):
        self.crash_call = (file_path, game_path, version_id)
        return {
            "reportId": "a" * 32,
            "versionId": version_id,
            "exitCode": None,
            "detectedBy": ["manual"],
            "reasons": [],
            "sourceFiles": [Path(file_path).name],
            "hasOutput": True,
        }

    def get_crash_output(self, report_id):
        return {"name": "game-output.log", "content": f"output:{report_id}"}

    def export_crash_report(self, report_id, output_path=None):
        return {"path": str(output_path or f"C:/exports/{report_id}.zip")}


class FakeWebviewWindow:
    def __init__(self):
        self.visible = False
        self.minimized = True
        self.focused = False
        self.main_thread_calls = 0

    def show(self) -> None:
        self.visible = True

    def unminimize(self) -> None:
        self.minimized = False

    def set_focus(self) -> None:
        self.focused = True

    def run_on_main_thread(self, handler) -> None:
        self.main_thread_calls += 1
        handler()


def _build_api(tmp_path) -> FrontendApi:
    launcher = SimpleNamespace(
        app_path=tmp_path,
        data_path=tmp_path / "ECL_data",
        config={"launcher": {"debug": True}},
        debug=True,
        launcher_version="1.4.2-alpha.3+20260906",
        launcher_version_type="alpha",
    )
    bus = EventBus()
    context = SimpleNamespace(
        state=launcher,
        events=bus,
        config=ConfigStore(tmp_path / "ECL_data", bus),
        http=FakeHttp(),
        accounts=FakeAccounts(),
        connector=SimpleNamespace(),
        wardrobe=FakeWardrobe(),
        info_card=FakeInfoCard(),
        game=FakeGame(),
        plugins=FakePlugins(),
        processes=SimpleNamespace(),
    )
    return FrontendApi(context)


def test_dual_source_search_uses_real_game_service_and_typed_ipc(tmp_path, monkeypatch) -> None:
    from ECL.services.game import GameService

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["params"]))
        if "modrinth" in url:
            body = {
                "hits": [{"project_id": "mr-sodium", "slug": "sodium", "title": "Sodium", "author": "Author"}],
                "total_hits": 1,
            }
        else:
            body = {
                "data": [{"id": 123, "slug": "sodium", "name": "Sodium", "authors": [{"name": "Author"}]}],
                "pagination": {"totalCount": 1},
            }
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr("ECL.services.game.resources._proxied_get", fake_get)
    api = _build_api(tmp_path)
    api.game = GameService(
        FakeAccounts(), resource_path=tmp_path, curseforge_api_key="test", enable_version_watcher=False
    )
    try:
        response = asyncio.run(api.search_mods({"query": "", "source": "all", "loader_type": "fabric"}))
        assert response["success"] is True
        catalog = response["data"]
        assert catalog["total"] == 1 and catalog["totalExact"] is True
        assert catalog["sessionId"] and not catalog["hasMore"]
        assert catalog["items"][0]["groupId"] == "mod:modrinth:mr-sodium"
        assert [(entry["source"], entry["projectId"]) for entry in catalog["items"][0]["alternatives"]] == [
            ("modrinth", "mr-sodium"),
            ("curseforge", "123"),
        ]
        assert len(calls) == 2
        cf_params = next(params for url, params in calls if "curseforge" in url)
        assert cf_params["modLoaderType"] == 4
        assert cf_params["sortField"] == 6
        invalid = asyncio.run(api.search_mods({"query": "", "source": "all", "offset": 20}))
        assert invalid["success"] is False
        assert invalid["errorCode"] == "INVALID_REQUEST"
    finally:
        api.game.close()


def test_custom_download_defaults_and_validation_do_not_log_secrets(tmp_path, caplog) -> None:
    from ECL.services.custom_downloads import CustomDownloadService
    from ECL.services.operations import OperationManager

    api = _build_api(tmp_path)
    manager = OperationManager(tmp_path / "operations-data", EventBus())
    api.context.downloads = CustomDownloadService(manager, data_path=api.data_path)
    try:
        defaults = asyncio.run(api.custom_download_defaults({}))
        assert defaults == {
            "success": True,
            "data": {"downloadDirectory": str(api.data_path / "downloads"), "userAgent": "EuoraCraft-Launcher"},
        }
        assert not (api.data_path / "downloads").exists()
        invalid = asyncio.run(
            api.custom_download_start({"url": "https://host/file", "headers": {"Bad Key": "secret-value"}})
        )
        assert invalid["success"] is False and invalid["errorCode"] == "INVALID_REQUEST"
        assert "secret-value" not in str(invalid) and "secret-value" not in caplog.text
    finally:
        manager.close()


def test_download_folder_picker_defaults_and_legacy_game_picker(tmp_path, monkeypatch) -> None:
    import ECL.api.files as files_module

    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class Dialog:
        def blocking_pick_folder(self, **options):
            calls.append(options)
            return None

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: Dialog()))
    assert asyncio.run(api.select_directory({"purpose": "custom-download"})) == {"success": True, "data": {"path": ""}}
    assert calls[-1] == {"set_title": "选择下载文件夹", "set_directory": str(api.data_path / "downloads")}
    assert (api.data_path / "downloads").is_dir()
    selected = tmp_path / "different-folder"
    asyncio.run(api.select_directory({"purpose": "custom-download", "default_directory": str(selected)}))
    assert calls[-1]["set_directory"] == str(selected)
    assert not selected.exists()
    asyncio.run(api.select_directory({}))
    assert calls[-1] == {"set_title": "选择游戏目录"}
    count = len(calls)
    invalid = asyncio.run(api.select_directory({"purpose": "custom-download", "default_directory": "relative"}))
    assert invalid["success"] is False and len(calls) == count


def test_launcher_config_excludes_runtime_metadata(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.settings_get({"sections": ["launcher"]}))

    assert result["success"] is True
    assert result["data"]["launcher"] == {
        "debug": True,
        "debug_log_level": "info",
        "disable_ssl_verify": False,
        "api_proxy_mode": "none",
        "api_proxy_url": "",
        "proxy_mode": "none",
        "proxy_url": "",
        "request_timeout": 15,
        "request_retries": 2,
        "dev_channel": False,
        "single_instance": True,
    }


def test_launcher_info_uses_runtime_metadata(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.launcher.active_window_chrome = "system_shadow"
    api.launcher.config["ui"] = {"theme": {"window_chrome": "custom"}}

    result = asyncio.run(api.launcher_info({}))

    assert result["success"] is True
    assert result["data"]["debug"] is True
    assert result["data"]["version"] == "1.4.2-alpha.3+20260906"
    assert result["data"]["version_type"] == "alpha"
    assert result["data"]["active_window_chrome"] == "system_shadow"


def test_info_card_delegates_to_registered_service(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.info_card_get({}))

    assert result["success"] is True
    assert result["data"]["tips"] == ["测试提示"]
    assert result["data"]["interval"] == 8000


def test_user_agreement_state_is_persisted_by_formal_system_api(tmp_path) -> None:
    api = _build_api(tmp_path)

    saved = asyncio.run(api.user_agreement_save({}))
    loaded = asyncio.run(api.user_agreement_get({}))
    cleared = asyncio.run(api.user_agreement_clear({}))

    assert saved["success"] is True
    assert saved["data"]["accepted"] is True
    assert loaded == saved
    assert cleared["success"] is True


def test_java_scan_delegates_to_game_service(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.game_java_scan({}))

    assert result["success"] is True
    assert result["data"][0]["major_version"] == 21
    assert result["data"][0]["path"] == "C:/Java/bin/java.exe"


def test_scan_versions_delegates_to_registered_service(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.game_scan({"paths": ["D:/Games/.minecraft"]}))

    assert result == {
        "success": True,
        "data": [{"id": "1.20.1", "versionId": "1.20.1"}],
    }
    assert [Path(path) for path in api.game.requested_paths] == [Path("D:/Games/.minecraft")]
    assert api.game.compatibility_options == {"qomicex": {"instances_path": None}}

    forced_result = asyncio.run(api.game_scan({"paths": ["D:/Games/.minecraft"], "force": True}))
    assert forced_result["success"] is True
    assert api.game.scan_force is True


def test_install_version_delegates_to_game_service_with_runtime_options(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.game = FakeGame()
    game_path = tmp_path / ".minecraft"

    result = asyncio.run(
        api.game_install(
            {
                "version_id": "1.21.8",
                "game_path": str(game_path),
            }
        )
    )

    assert result == {
        "success": True,
        "data": {"taskId": "install-task", "versionId": "1.21.8", "versionName": "1.21.8"},
    }
    assert api.game.install_call[0]["version_id"] == "1.21.8"
    assert api.game.install_call[1]["game_path"] == game_path
    assert api.game.install_call[1]["source"] == "official"


def test_launch_instance_delegates_to_game_service_with_settings(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.game = FakeGame()

    result = asyncio.run(
        api.game_launch(
            {
                "version_id": "1.21.8",
                "game_path": str(tmp_path / ".minecraft"),
                "java_path": str(tmp_path / "java.exe"),
                "memory": 6144,
                "width": 1366,
                "height": 768,
                "fullscreen": True,
                "version_isolation": True,
            }
        )
    )

    assert result == {
        "success": True,
        "data": {
            "instanceId": "instance-id",
            "versionId": "1.21.8",
            "gamePath": str(tmp_path / ".minecraft"),
        },
    }
    assert api.game.launch_call[1]["memory"] == 6144
    assert api.game.launch_call[1]["width"] == 1366
    assert api.game.launch_call[1]["height"] == 768
    assert api.game.launch_call[1]["fullscreen"] is True
    assert api.game.launch_call[1]["version_isolation"] is True


def test_launch_instance_uses_global_window_settings_when_request_omits_them(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.game = FakeGame()
    game_config = api.config.get_config("game")
    game_config.update({"game_width": 1600, "game_height": 900, "fullscreen": True})
    api.config.save_config("game", game_config)

    result = asyncio.run(
        api.game_launch(
            {
                "version_id": "1.21.8",
                "game_path": str(tmp_path / ".minecraft"),
            }
        )
    )

    assert result["success"] is True
    assert api.game.launch_call[1]["width"] == 1600
    assert api.game.launch_call[1]["height"] == 900
    assert api.game.launch_call[1]["fullscreen"] is True


def test_launch_instance_applies_global_advanced_launch_settings(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.game = FakeGame()
    game_config = api.config.get_config("game")
    game_config.update(
        {
            "jvm_args": ["-XX:+UseG1GC"],
            "renderer": "vulkan",
            "game_args_tail": '--tail "two words"',
            "pre_launch_command": "prepare.cmd",
            "prefer_high_performance_gpu": True,
            "use_java_exe": True,
            "disable_crash_analysis": True,
        }
    )
    api.config.save_config("game", game_config)

    result = asyncio.run(
        api.game_launch(
            {
                "version_id": "1.21.8",
                "game_path": str(tmp_path / ".minecraft"),
                "game_args": ["--instance"],
            }
        )
    )

    assert result["success"] is True
    assert api.game.launch_call[1]["jvm_args"] == ["-XX:+UseG1GC"]
    assert api.game.launch_call[1]["game_args"] == ["--tail", "two words", "--instance"]
    assert api.game.launch_call[1]["pre_launch_command"] == "prepare.cmd"
    assert api.game.launch_call[1]["renderer"] == "vulkan"
    assert api.game.launch_call[1]["prefer_high_performance_gpu"] is True
    assert api.game.launch_call[1]["use_java_exe"] is True
    assert api.game.launch_call[1]["disable_crash_analysis"] is True


def test_launch_instance_resolves_saved_isolation_when_omitted(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.game = FakeGame()

    result = asyncio.run(
        api.game_launch(
            {
                "version_id": "1.21.8",
                "game_path": str(tmp_path / ".minecraft"),
            }
        )
    )

    assert result["success"] is True
    assert api.game.isolation_call[2] is None
    assert api.game.launch_call[1]["version_isolation"] is True


def test_instance_folder_resolves_saved_isolation_when_omitted(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.game = FakeGame()

    result = asyncio.run(
        api.game_instance_folder_open(
            {
                "game_path": str(tmp_path / ".minecraft"),
                "version_id": "1.21.8",
                "folder": "mods",
            }
        )
    )

    assert result["success"] is True
    assert api.game.isolation_call[2] is None
    assert api.game.folder_call[2:] == ("mods", True)


def test_crash_report_commands_validate_and_delegate(tmp_path) -> None:
    api = _build_api(tmp_path)
    log_path = tmp_path / "latest.log"
    log_path.write_text("crash", encoding="utf-8")
    report_id = "a" * 32

    analyzed = asyncio.run(
        api.game_crash_analyze(
            {
                "file_path": str(log_path),
                "game_path": str(tmp_path / ".minecraft"),
                "version_id": "1.21.8",
            }
        )
    )
    output = asyncio.run(api.game_crash_output({"report_id": report_id}))
    exported = asyncio.run(api.game_crash_export({"report_id": report_id}))

    assert analyzed["success"] is True
    assert analyzed["data"]["reportId"] == report_id
    assert api.game.crash_call == (log_path, tmp_path / ".minecraft", "1.21.8")
    assert output["data"]["content"] == f"output:{report_id}"
    assert exported["data"]["path"].endswith(f"{report_id}.zip")


def test_crash_report_id_rejects_invalid_values(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.game_crash_output({"report_id": "../report"}))

    assert result["success"] is False
    assert result["errorCode"] == "INVALID_REQUEST"


def test_select_save_file_uses_system_dialog_and_normalizes_zip_suffix(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakeSaveDialog:
        def blocking_save_file(self, **options):
            calls.append(options)
            return tmp_path / "crash-report"

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakeSaveDialog()))

    result = asyncio.run(api.select_save_file({"purpose": "crash-report"}))

    assert result == {"success": True, "data": {"path": str(tmp_path / "crash-report.zip")}}
    assert calls == [
        {
            "add_filter": ("ZIP 压缩包", ["zip"]),
            "set_file_name": "EuoraCraft-crash-report.zip",
            "set_title": "保存 Minecraft 崩溃报告",
        }
    ]


def test_select_save_file_accepts_mod_default_directory_and_filename(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []
    mods_directory = tmp_path / ".minecraft" / "mods"

    class FakeSaveDialog:
        def blocking_save_file(self, **options):
            calls.append(options)
            return mods_directory / "fabric-api.jar"

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakeSaveDialog()))

    result = asyncio.run(
        api.select_save_file(
            {
                "purpose": "mod-file",
                "default_directory": str(mods_directory),
                "default_name": "fabric-api.jar",
            }
        )
    )

    assert result == {"success": True, "data": {"path": str(mods_directory / "fabric-api.jar")}}
    assert calls == [
        {
            "add_filter": ("JAR 文件", ["jar", "zip"]),
            "set_file_name": "fabric-api.jar",
            "set_title": "另存模组文件",
            "set_directory": str(mods_directory),
        }
    ]


def test_select_save_file_instance_export_uses_mrpack_suffix(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakeSaveDialog:
        def blocking_save_file(self, **options):
            calls.append(options)
            return tmp_path / "my-pack"

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakeSaveDialog()))

    result = asyncio.run(api.select_save_file({"purpose": "instance-export"}))

    assert result == {"success": True, "data": {"path": str(tmp_path / "my-pack.mrpack")}}
    assert calls == [
        {
            "add_filter": ("Modrinth 整合包", ["mrpack"]),
            "set_file_name": "instance.mrpack",
            "set_title": "导出实例整合包",
        }
    ]


@pytest.mark.parametrize(
    ("purpose", "filename", "expected_name", "label", "extensions"),
    [
        ("custom-download", "file.bin", "file.bin", "所有文件", ["*"]),
        ("instance-shortcut", "测试实例", "测试实例.lnk", "Windows 快捷方式", ["lnk"]),
    ],
)
def test_select_save_file_advanced_purposes(tmp_path, monkeypatch, purpose, filename, expected_name, label, extensions):
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakeSaveDialog:
        def blocking_save_file(self, **options):
            calls.append(options)
            return tmp_path / filename

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakeSaveDialog()))
    result = asyncio.run(api.select_save_file({"purpose": purpose}))
    assert result == {"success": True, "data": {"path": str(tmp_path / expected_name)}}
    assert calls[0]["add_filter"] == (label, extensions)


def test_instance_shortcut_ipc_uses_application_runtime_paths(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api.context.state.resource_path = tmp_path / "resources"
    api.context.state.is_frozen = False
    target = SimpleNamespace(game_path=tmp_path, version_id="1.21.1", instance_path=tmp_path / "versions" / "1.21.1")
    monkeypatch.setattr(api.game, "resolve_instance", lambda *_args: target, raising=False)
    monkeypatch.setattr(
        api.game,
        "scan_versions",
        lambda _roots: [{"versionId": "1.21.1", "alias": "测试实例", "icon": {"type": "builtin", "value": "command"}}],
        raising=False,
    )
    calls = []

    class FakeShortcutService:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def create(self, instance_path, name, icon, output_path):
            assert (instance_path, name, icon.value) == (target.instance_path, "测试实例", "command")
            return {"path": str(output_path), "iconPath": "icon.ico"}

    monkeypatch.setattr(import_module("ECL.api.workspace"), "InstanceShortcutService", FakeShortcutService)
    result = asyncio.run(
        api.game_instance_shortcut_create(
            {"game_path": str(tmp_path), "version_id": "1.21.1", "output_path": str(tmp_path / "实例.lnk")}
        )
    )
    assert result["success"] is True
    assert calls == [
        (
            (api.context.state.data_path, api.context.state.resource_path),
            {"is_frozen": False, "app_path": tmp_path},
        )
    ]


def test_launch_uses_legacy_global_instance_overrides_until_saved(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    key = f"{str(tmp_path).replace(chr(92), '/').lower()}::1.21.1"
    legacy = {"customMemory": True, "memory": 6144, "customJava": True, "javaPath": "custom-java"}
    api.config._write_config({**api.config.get_config(), "version_settings": {key: legacy, "other::instance": legacy}})
    body = {"game_path": str(tmp_path), "version_id": "1.21.1"}
    assert asyncio.run(api.game_version_settings_get(body))["data"] == legacy
    effective = asyncio.run(api.game_version_settings_effective(body))["data"]
    assert (effective["memory"], effective["java_path"]) == (6144, "custom-java")
    assert asyncio.run(api.game_launch(body))["success"] is True
    assert api.game.launch_call[1]["memory"] == 6144
    assert api.game.launch_call[1]["java_path"] == "custom-java"
    monkeypatch.setattr(api.game, "read_version_settings", lambda *_args: {"memoryMode": "manual", "memory": 8192})
    assert asyncio.run(api.game_launch(body))["success"] is True
    assert api.game.launch_call[1]["memory"] == 8192
    assert api.config.get_config("version_settings")[key] == legacy
    monkeypatch.setattr(api.game, "write_version_settings", lambda *_args: {}, raising=False)
    assert asyncio.run(api.game_version_settings_set({**body, "data": {}}))["success"] is True
    assert api.config.get_config("version_settings") == {"other::instance": legacy}
    monkeypatch.setattr(api.game, "read_version_settings", lambda *_args: {})
    assert asyncio.run(api.game_version_settings_get(body))["data"] == {}


def test_select_file_modpack_purpose_filters_modpack_extensions(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakePickDialog:
        def blocking_pick_file(self, **options):
            calls.append(options)
            return str(tmp_path / "pack.mrpack")

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakePickDialog()))

    result = asyncio.run(api.select_file({"purpose": "modpack"}))

    assert result["success"] is True
    assert calls == [{"add_filter": ("文件", ["zip", "mrpack"]), "set_title": "选择整合包"}]


@pytest.mark.parametrize(
    ("purpose", "extensions", "title"),
    [
        ("plugin-package", ["eclplugin"], "选择插件包"),
    ],
)
def test_select_file_plugin_purposes_use_specific_filters(
    tmp_path, monkeypatch, purpose: str, extensions: list[str], title: str
) -> None:
    """
    插件归档与运行时离线包使用各自的原生文件过滤器。
    """
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakePickDialog:
        def blocking_pick_file(self, **options):
            calls.append(options)
            return str(tmp_path / "selected")

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakePickDialog()))
    result = asyncio.run(api.select_file({"purpose": purpose}))
    assert result["success"] is True
    assert calls == [{"add_filter": ("文件", extensions), "set_title": title}]


def test_offline_account_delegates_to_registered_service(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.accounts_add_offline({"username": "Steve"}))

    assert result == {
        "success": True,
        "data": {"id": "offline-id", "alias": "Steve", "type": "offline", "uuid": "offline-uuid"},
    }


@pytest.mark.parametrize(
    "resource_type,extensions",
    [
        ("shaderpack", ["zip"]),
        ("datapack", ["zip"]),
        ("resourcepack", ["zip"]),
        ("schematic", ["litematic", "schem", "schematic"]),
    ],
)
def test_resource_picker_filters_current_type(tmp_path, monkeypatch, resource_type, extensions) -> None:
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakePickDialog:
        def blocking_pick_files(self, **options):
            calls.append(options)
            return []

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakePickDialog()))
    response = asyncio.run(api.select_files({"purpose": "resource-files", "resource_type": resource_type}))
    assert response["success"] is True
    assert calls[0]["add_filter"][1] == extensions


@pytest.mark.parametrize("purpose,is_folder", [("world-import", False), ("world-import-folder", True)])
def test_world_picker_uses_zip_filter_or_directory(tmp_path, monkeypatch, purpose, is_folder) -> None:
    api = _build_api(tmp_path)
    api._webview = object()
    calls = []

    class FakePickDialog:
        def blocking_pick_file(self, **options):
            calls.append((False, options))
            return None

        def blocking_pick_folder(self, **options):
            calls.append((True, options))
            return None

    monkeypatch.setattr(files_module, "DialogExt", SimpleNamespace(file=lambda _webview: FakePickDialog()))
    response = asyncio.run(api.select_file({"purpose": purpose}))
    assert response == {"success": True, "data": {"path": ""}}
    assert calls[0][0] == is_folder
    if not is_folder:
        assert calls[0][1]["add_filter"][1] == ["zip"]


def test_offline_account_forwards_optional_custom_uuid(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(
        api.accounts_add_offline(
            {
                "username": "CustomPlayer",
                "uuid": "01234567-89ab-cdef-0123-456789abcdef",
            }
        )
    )

    assert result["success"] is True
    assert api.accounts.last_offline_input == (
        "CustomPlayer",
        "01234567-89ab-cdef-0123-456789abcdef",
    )


def test_offline_account_forwards_optional_skin(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.accounts_add_offline({"username": "Steve", "skin": "Alice"}))

    assert result["success"] is True
    assert api.accounts.last_offline_skin == "Alice"


def test_offline_default_skins_handler(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.accounts_default_skins({}))

    assert result == {
        "success": True,
        "data": [{"id": "alice", "name": "Alice", "skinUrl": "data:image/png;base64,AAAA"}],
    }


def test_offline_set_skin_handler(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.accounts_set_offline_skin({"account_id": "offline-id", "skin": "alice"}))

    assert result["success"] is True
    assert api.accounts.last_offline_skin == "alice"


def test_authlib_login_uses_account_manager_and_remembers_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    api = _build_api(tmp_path)

    result = asyncio.run(
        api.accounts_add_authlib(
            {
                "server_url": "https://skin.example.com/api/yggdrasil/",
                "email": "player@example.com",
                "password": "secret-password",
            }
        )
    )

    assert result["success"] is True
    assert result["data"]["id"] == "authlib-id"
    assert api.accounts.last_authlib_input == (
        "https://skin.example.com/api/yggdrasil/",
        "player@example.com",
        "secret-password",
    )
    # 外置登录服务器历史保存在独立用户文件中，setting.json 不再包含账户相关数据。
    assert api.config.get_config("authlib") is None
    servers_file = tmp_path / "home" / ".ECL" / "accounts" / "authlib" / "servers.json"
    assert json.loads(servers_file.read_text(encoding="utf-8")) == [
        {
            "url": "https://skin.example.com/api/yggdrasil/",
            "email": "player@example.com",
        }
    ]
    saved_servers = asyncio.run(api.authlib_servers({}))
    assert saved_servers["data"][0]["url"] == "https://skin.example.com/api/yggdrasil/"
    assert saved_servers["data"][0]["email"] == "player@example.com"


def test_authlib_profile_selection_is_forwarded_to_account_manager(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(
        api.accounts_select_authlib_profile({"account_id": "pending-authlib", "profile_id": "profile-two"})
    )

    assert result["success"] is True
    assert result["data"]["uuid"] == "profile-two"
    assert api.accounts.last_authlib_profile == ("pending-authlib", "profile-two")


def test_authlib_server_url_is_resolved_through_ali(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.authlib_resolve_server({"server_url": "skin.example.com"}))

    assert result == {
        "success": True,
        "data": "https://skin.example.com/api/yggdrasil",
    }


def test_frontend_ready_and_plugin_api_use_registered_framework(tmp_path) -> None:
    api = _build_api(tmp_path)
    webview_window = FakeWebviewWindow()

    ready_result = asyncio.run(api.frontend_ready({}, webview_window))
    plugin_result = asyncio.run(api.plugin_list({}))

    assert ready_result["success"] is True
    assert webview_window.visible is False
    assert api.plugins.frontend_ready_count == 1
    assert plugin_result["data"] == [{"name": "example", "status": "enabled"}]


def test_frontend_ready_replays_cached_startup_update_result(tmp_path) -> None:
    """
    前端晚于后端更新检测就绪时，仍能收到已缓存的同一份检测结果。

    该回放只传递缓存，不会触发新的更新网络请求。
    """

    api = _build_api(tmp_path)
    payload = {
        "status": "update_available",
        "current_version": "1.0.0",
        "channel": "release",
        "latest_version": "1.1.0",
        "latest_url": None,
        "latest_notes": None,
        "message": None,
    }
    api.startup_update = SimpleNamespace(result=lambda: payload)
    emitted: list[tuple[str, object]] = []
    api.emit_to_frontend = lambda event, data, **_kwargs: emitted.append((event, data))

    asyncio.run(api.frontend_ready({}, FakeWebviewWindow()))

    assert ("update:check_completed", payload) in emitted


def test_adapter_main_window_is_visible_without_native_shadow() -> None:
    adapter = object.__new__(Adapter)
    adapter.config = {"launcher": {"debug": False}, "tauri": {}}
    adapter.launcher_version = "0.0.1-alpha"

    window_config = adapter._build_config()["app"]["windows"][0]

    assert window_config["visible"] is True
    assert window_config["decorations"] is False
    assert window_config["transparent"] is True
    assert window_config["shadow"] is False
    assert window_config["minWidth"] == 960
    assert window_config["minHeight"] == 600


@pytest.mark.parametrize(
    ("platform", "expected_mode", "is_transparent", "has_shadow"),
    [
        ("win32", "system_shadow", False, True),
        ("linux", "custom", True, False),
        ("darwin", "custom", True, False),
    ],
)
def test_new_config_creates_platform_appropriate_window_chrome(
    tmp_path, monkeypatch, platform: str, expected_mode: str, is_transparent: bool, has_shadow: bool
) -> None:
    monkeypatch.setattr(import_module("ECL.adapters.tauri"), "sys", SimpleNamespace(platform=platform))
    adapter = object.__new__(Adapter)
    adapter.config = ConfigStore(tmp_path / "ECL_data").get_config()
    adapter.launcher_version = "0.0.1-alpha"

    window_config = adapter._build_config()["app"]["windows"][0]

    assert adapter._active_window_chrome == expected_mode
    assert window_config["decorations"] is False
    assert window_config["transparent"] is is_transparent
    assert window_config["shadow"] is has_shadow


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("window_chrome", ["custom", "system_shadow", "native", "unsupported", None])
def test_adapter_main_window_chrome_follows_ui_setting(window_chrome: str | None, platform: str, monkeypatch) -> None:
    monkeypatch.setattr(import_module("ECL.adapters.tauri"), "sys", SimpleNamespace(platform=platform))
    adapter = object.__new__(Adapter)
    adapter.config = {"tauri": {}, "ui": {"theme": {"window_chrome": window_chrome}}}
    adapter.launcher_version = "0.0.1-alpha"

    window_config = adapter._build_config()["app"]["windows"][0]

    expected_mode = (
        "native"
        if window_chrome == "native"
        else "system_shadow"
        if window_chrome == "system_shadow" and platform == "win32"
        else "custom"
    )
    assert adapter._active_window_chrome == expected_mode
    assert window_config["decorations"] is (expected_mode == "native")
    assert window_config["transparent"] is (expected_mode == "custom")
    assert window_config["shadow"] is (expected_mode != "custom")


def test_adapter_focuses_main_window_once_at_startup_ready(monkeypatch) -> None:
    adapter_module = import_module("ECL.adapters.tauri")
    adapter = object.__new__(Adapter)
    adapter._startup_focus_attempted = False
    adapter.logger = SimpleNamespace(warning=lambda *_args: None, exception=lambda *_args: None)
    webview_window = FakeWebviewWindow()
    lookup_labels: list[str] = []

    def get_main_window(_app_handle, label: str):
        lookup_labels.append(label)
        return webview_window

    monkeypatch.setattr(adapter_module.Manager, "get_webview_window", get_main_window)
    app_handle = object()
    ready_event = adapter_module.RunEvent.Ready()

    adapter._focus_main_window_on_ready(app_handle, object())
    assert lookup_labels == []
    assert webview_window.focused is False

    adapter._focus_main_window_on_ready(app_handle, ready_event)
    assert lookup_labels == ["main"]
    assert webview_window.focused is True

    adapter._focus_main_window_on_ready(app_handle, ready_event)
    assert lookup_labels == ["main"]


@pytest.mark.parametrize("failure", [None, OSError("focus denied"), RuntimeError("window closed")])
def test_adapter_startup_focus_failure_does_not_block_startup(monkeypatch, failure: Exception | None) -> None:
    adapter_module = import_module("ECL.adapters.tauri")
    adapter = object.__new__(Adapter)
    adapter._startup_focus_attempted = False
    warnings: list[str] = []
    errors: list[str] = []
    adapter.logger = SimpleNamespace(
        warning=lambda message: warnings.append(message), exception=lambda message: errors.append(message)
    )

    class FailingWindow:
        def set_focus(self) -> None:
            if failure is not None:
                raise failure

    monkeypatch.setattr(
        adapter_module.Manager,
        "get_webview_window",
        lambda _app_handle, _label: None if failure is None else FailingWindow(),
    )

    adapter._focus_main_window_on_ready(object(), adapter_module.RunEvent.Ready())

    assert adapter._startup_focus_attempted is True
    assert bool(warnings) is (failure is None)
    assert bool(errors) is (failure is not None)


def test_focus_window_restores_and_focuses_webview(tmp_path) -> None:
    api = _build_api(tmp_path)
    webview_window = FakeWebviewWindow()
    asyncio.run(api.frontend_ready({}, webview_window))

    assert api.focus_window() is True
    assert webview_window.minimized is False
    assert webview_window.visible is True
    assert webview_window.focused is True


class _ClosedWebviewWindow:
    def run_on_main_thread(self, handler) -> None:
        raise RuntimeError("runtime error: failed to send message to the webview")


def test_emit_to_frontend_stops_after_webview_closed(tmp_path) -> None:
    api = _build_api(tmp_path)
    api._webview = _ClosedWebviewWindow()

    api.emit_to_frontend("launcher:notify", {"message": "test"})

    assert api._webview is None
    api.emit_to_frontend("launcher:notify", {"message": "test"})  # 不应抛出异常


def test_microsoft_authorization_event_focuses_before_forwarding(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    calls = []
    monkeypatch.setattr(api, "focus_window", lambda: calls.append("focus"))
    monkeypatch.setattr(api, "emit_to_frontend", lambda event, payload: calls.append((event, payload)))
    adapter = object.__new__(Adapter)
    adapter.frontend_api_instance = api
    adapter.events = api.events

    adapter._register_events()
    # 共享事件桥负责转发，适配层额外订阅负责唤起窗口，两者互不冲突。
    event = {"status": "progress", "stage": "authorization_confirmed", "focus": True}
    api.events.emit("accounts:microsoft_login_status", event)

    assert "focus" in calls
    assert ("accounts_microsoft_login_status", event) in calls


def test_adapter_forwards_launcher_notifications(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    forwarded = []
    monkeypatch.setattr(api, "emit_to_frontend", lambda event, payload: forwarded.append((event, payload)))
    adapter = object.__new__(Adapter)
    adapter.frontend_api_instance = api
    adapter.events = api.events
    payload = {"type": "warning", "title": "Notice", "message": "Please check settings"}

    adapter._register_events()
    api.events.emit("launcher:notify", payload)

    assert forwarded == [("launcher:notify", payload)]


def test_sidebar_state_is_forwarded_to_plugins(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.plugin_notify_sidebar_state({"collapsed": True}))

    assert result["success"] is True
    assert api.plugins.sidebar_states == [True]

    invalid = asyncio.run(api.plugin_notify_sidebar_state({"collapsed": "yes"}))
    assert invalid == {
        "success": False,
        "message": "侧栏状态必须是布尔值",
        "errorCode": "INVALID_SIDEBAR_STATE",
        "presentation": "message",
    }


def test_unexpected_ipc_error_returns_correlated_modal_and_emits_event(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    emitted = []
    api.events.subscribe("launcher:error", emitted.append)
    monkeypatch.setattr(api.accounts, "list_accounts", lambda: (_ for _ in ()).throw(RuntimeError("secret detail")))

    result = asyncio.run(command_handlers(api)["accounts_list"]({}))

    assert result["success"] is False
    assert result["errorCode"] == "INTERNAL_ERROR"
    assert result["presentation"] == "modal"
    assert result["errorId"]
    assert "secret detail" not in result["message"]
    assert result["detail"] == "secret detail"
    assert emitted == [
        {
            "error_id": result["errorId"],
            "title": result["title"],
            "message": result["message"],
            "detail": "secret detail",
        }
    ]


def test_startup_preload_warms_connector_nodes_without_arguments(tmp_path) -> None:
    api = _build_api(tmp_path)
    calls: list[str] = []
    api.connector.fetch_nodes = lambda: calls.append("warmed")

    result = asyncio.run(command_handlers(api)["launcher_preload_connector"]({}))

    assert result["success"] is True
    assert calls == ["warmed"]
    invalid = asyncio.run(command_handlers(api)["launcher_preload_connector"]({"unexpected": True}))
    assert invalid["errorCode"] == "INVALID_REQUEST"


def test_guarded_call_timeout_cancels_slow_operation(tmp_path) -> None:
    api = _build_api(tmp_path)

    async def slow() -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"success": True}

    result = asyncio.run(_guarded_call(api, "test_slow", "INTERNAL_ERROR", slow(), timeout=0.05))

    assert result["success"] is False
    assert result["errorCode"] == "OPERATION_TIMEOUT"
    assert result["message"] == "操作超时，请检查网络后重试"


def test_guarded_call_without_timeout_runs_to_completion(tmp_path) -> None:
    api = _build_api(tmp_path)

    async def fast() -> dict[str, Any]:
        return {"success": True}

    result = asyncio.run(_guarded_call(api, "test_fast", "INTERNAL_ERROR", fast()))

    assert result == {"success": True}


def test_guarded_call_treats_auth_exception_as_known_error(tmp_path) -> None:
    from ECL.game import AuthException

    api = _build_api(tmp_path)

    async def failing() -> dict[str, Any]:
        raise AuthException("Microsoft 令牌刷新失败，请检查网络后重试或重新登录该账户")

    result = asyncio.run(_guarded_call(api, "test_auth", "SKIN_UPDATE_FAILED", failing()))

    assert result["success"] is False
    assert result["errorCode"] == "SKIN_UPDATE_FAILED"
    assert result["presentation"] != "modal"
    assert "Microsoft 令牌刷新失败" in result["message"]


def test_critical_persistence_error_returns_modal_metadata_and_emits_event(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    emitted = []
    api.events.subscribe("launcher:error", emitted.append)
    monkeypatch.setattr(
        api.accounts,
        "remove_account",
        lambda *_args: (_ for _ in ()).throw(AccountError("保存账号数据失败", "ACCOUNT_SAVE_FAILED")),
        raising=False,
    )

    result = asyncio.run(api.accounts_remove({"account_id": "account"}))

    assert result["success"] is False
    assert result["errorCode"] == "ACCOUNT_SAVE_FAILED"
    assert result["presentation"] == "modal"
    assert result["message"] == "账号数据保存失败，请重试。若问题持续，请导出日志以便排查。"
    assert result["detail"] == "保存账号数据失败"
    assert emitted[0]["error_id"] == result["errorId"]
    assert emitted[0]["message"] == result["message"]
    assert emitted[0]["detail"] == "保存账号数据失败"


def test_frontend_ready_pushes_cacheable_development_warning(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    webview_window = FakeWebviewWindow()
    emitted = []
    monkeypatch.setattr(
        frontend_module._Emitter,
        "emit_str_to",
        lambda *args: emitted.append((args[-2], json.loads(args[-1]))),
    )

    asyncio.run(api.frontend_ready({}, webview_window))

    development_popups = [
        payload
        for event, payload in emitted
        if event == "launcher:popup" and payload["id"] == "launcher-development-mode"
    ]
    assert development_popups == [
        {
            "id": "launcher-development-mode",
            "title": "开发模式提示",
            "content": (
                "当前启动器正以 **开发模式** 运行，部分功能可能尚未完成或存在不稳定行为。\n\n"
                "如果遇到问题，请保留相关日志以便排查。"
            ),
            "level": "warning",
            "dismissible": True,
            "cacheable": True,
        }
    ]


def test_important_backend_events_wait_until_frontend_is_ready(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api.launcher.debug = False
    popup = {
        "id": "queued-popup",
        "title": "排队弹窗",
        "content": "前端就绪前产生的消息。",
        "cacheable": True,
    }
    error = {
        "error_id": "queued-error",
        "title": "后端错误",
        "message": "前端就绪前发生错误。",
        "detail": "测试详情",
        "kind": "game_crash",
        "crash": {
            "reportId": "a" * 32,
            "versionId": "1.21.8",
            "detectedBy": ["exit_code"],
            "reasons": [],
            "sourceFiles": [],
            "hasOutput": False,
        },
    }

    api.emit_popup_to_frontend(popup)
    api.emit_error_to_frontend(error)

    emitted = []
    monkeypatch.setattr(
        frontend_module._Emitter,
        "emit_str_to",
        lambda *args: emitted.append((args[-2], json.loads(args[-1]))),
    )
    webview = FakeWebviewWindow()
    asyncio.run(api.frontend_ready({}, webview))

    assert emitted == [
        ("launcher:popup", popup),
        ("launcher:error", error),
    ]
    assert webview.main_thread_calls == 2


def test_ready_frontend_events_are_marshaled_to_main_thread(tmp_path, monkeypatch) -> None:
    api = _build_api(tmp_path)
    api.launcher.debug = False
    webview = FakeWebviewWindow()
    asyncio.run(api.frontend_ready({}, webview))
    emitted = []
    monkeypatch.setattr(
        frontend_module._Emitter,
        "emit_str_to",
        lambda *args: emitted.append((args[-2], json.loads(args[-1]))),
    )
    payload = {"error_id": "worker-error", "title": "崩溃", "message": "后台分析完成"}

    api.emit_error_to_frontend(payload)

    assert webview.main_thread_calls == 1
    assert emitted == [("launcher:error", payload)]


def test_serious_errors_remain_available_until_frontend_acknowledges_them(tmp_path) -> None:
    api = _build_api(tmp_path)
    payload = {"error_id": "recoverable-event", "title": "崩溃", "message": "后台分析完成"}

    api.emit_error_to_frontend(payload)
    pending = asyncio.run(api.launcher_errors_pending({}))
    acknowledged = asyncio.run(api.launcher_errors_ack({"error_ids": ["recoverable-event"]}))
    empty = asyncio.run(api.launcher_errors_pending({}))

    assert pending == {"success": True, "data": [payload]}
    assert acknowledged == {"success": True, "data": {"removed": 1}}
    assert empty == {"success": True, "data": []}


def test_wardrobe_list_delegates_to_registered_store(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.wardrobe_list({}))

    assert result["success"] is True
    assert result["data"] == []


def test_wardrobe_sync_downloads_current_account_skin_into_store(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.wardrobe_sync_account_skin({"account_id": "microsoft-account"}))

    assert result["success"] is True
    assert result["data"]["item"]["id"] == "synced-skin"
    assert api.wardrobe.imported == (
        b"downloaded-png",
        "skin",
        "Player 当前皮肤",
        "slim",
    )


def test_wardrobe_apply_skin_uploads_internal_texture_without_base64_body(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.wardrobe_apply_skin({"item_id": "skin-id", "account_id": "microsoft-account"}))

    assert result["success"] is True
    assert api.accounts.uploaded_skin == ("microsoft-account", "slim", b"png")


def test_debug_maintenance_requires_debug_mode(tmp_path) -> None:
    api = _build_api(tmp_path)
    api.launcher.debug = False

    result = asyncio.run(api.debug_reset_launcher_data({}))

    assert result["success"] is False
    assert result["errorCode"] == "DEBUG_MODE_REQUIRED"
    assert not (api.data_path / ".pending_debug_maintenance.json").exists()


def test_debug_maintenance_schedules_allowed_action(tmp_path) -> None:
    api = _build_api(tmp_path)

    result = asyncio.run(api.debug_clear_plugins({}))

    assert result["success"] is True
    assert result["data"]["action"] == "clear_plugins"
    assert result["data"]["restart_required"] is True
    assert (api.data_path / ".pending_debug_maintenance.json").is_file()


def test_frontend_ready_dispatches_cli_launch_once(tmp_path, monkeypatch) -> None:
    """主窗口就绪后应恰好派发一次命令行快捷启动，body 携带配置运行参数。"""
    import ECL.api.bridge as bridge_module

    api = _build_api(tmp_path)
    api.launcher.launch_options = LaunchOptions(launch_target="Foo")
    resolved_roots: list = []
    monkeypatch.setattr(
        bridge_module,
        "candidate_roots",
        lambda _config: resolved_roots,
    )
    monkeypatch.setattr(
        bridge_module,
        "resolve_launch_target",
        lambda _target, _roots: (Path("/games/mc"), "Foo"),
    )
    recorded: list[dict[str, Any]] = []

    async def fake_game_launch(body: dict[str, Any]) -> dict[str, Any]:
        recorded.append(body)
        return {"success": True, "data": {"instanceId": "i1"}}

    monkeypatch.setattr(api, "game_launch", fake_game_launch)
    webview = FakeWebviewWindow()

    async def scenario() -> None:
        await api.frontend_ready({}, webview)
        await api.frontend_ready({}, webview)  # 窗口刷新重复就绪不应重复派发
        assert api._cli_launch_task is not None
        await api._cli_launch_task

    asyncio.run(scenario())

    assert len(recorded) == 1
    body = recorded[0]
    assert body["version_id"] == "Foo"
    assert body["game_path"] == str(Path("/games/mc"))
    assert "memory" not in body  # 缺省字段交由统一实例设置解析器处理
    assert "lock_memory" not in body
    assert "process_priority" not in body


def test_frontend_ready_cli_launch_failure_emits_popup(tmp_path) -> None:
    """快捷启动目标解析失败时应发出弹窗事件且不抛出异常。"""
    api = _build_api(tmp_path)
    api.launcher.launch_options = LaunchOptions(launch_target="Missing")
    popups: list[dict[str, Any]] = []
    api.emit_popup_to_frontend = lambda payload: popups.append(payload)  # type: ignore[method-assign]
    webview = FakeWebviewWindow()

    async def scenario() -> None:
        await api.frontend_ready({}, webview)
        assert api._cli_launch_task is not None
        await api._cli_launch_task

    asyncio.run(scenario())

    assert any(popup.get("id") == "cli-launch-failed" for popup in popups)


def test_authenticated_launch_requests_wait_for_ready_and_use_temporary_overrides(tmp_path, monkeypatch) -> None:
    import ECL.api.bridge as bridge_module
    from ECL.services.single_instance import SingleInstanceService

    api = _build_api(tmp_path)
    service = SingleInstanceService(api.events, tmp_path, "test")
    api._single_instance = service
    api.events.subscribe("launcher:launch_request", api._on_cli_request)
    monkeypatch.setattr(bridge_module, "resolve_launch_target", lambda target, roots: (Path("/games/mc"), target))
    recorded = []

    async def launch(body):
        recorded.append(body)
        return {"success": True}

    monkeypatch.setattr(api, "game_launch", launch)
    payload = {"token": service._token, "action": "launch", "argv": ["--launch=Foo", "--memory=6144", "--windowed"]}
    assert service._dispatch(payload)
    assert recorded == []

    async def scenario():
        await api.frontend_ready({}, FakeWebviewWindow())
        await api._cli_launch_task
        assert recorded == [
            {"version_id": "Foo", "game_path": str(Path("/games/mc")), "memory": 6144, "fullscreen": False}
        ]
        assert service._dispatch({**payload, "argv": ["--launch=Bar"]})
        await asyncio.sleep(0)
        await api._cli_launch_task
        assert recorded[-1]["version_id"] == "Bar"
        assert "memory" not in recorded[-1]
        api._on_launcher_closing(None)
        await asyncio.sleep(0)
        assert api._launch_closing
        assert not api._pending_cli_launches

    asyncio.run(scenario())
    service.close()
