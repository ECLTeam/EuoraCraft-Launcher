# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：IPC 命令注册表：命令名到处理器方法的映射。
#
# 公开接口：
#   - class IpcCommandRegistry — 保存正式 IPC 命令名称。
#   - command_handlers(api) -> dict[str, Callable[..., Any]] — 返回 PyTauri 需要注册的唯一正式 IPC 命令表。
# ============================================================

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from pytauri import AppHandle
    from pytauri.ipc import WebviewWindow
except ImportError:  # pragma: no cover - lightweight test doubles omit injected types
    AppHandle = Any  # type: ignore[misc,assignment]
    WebviewWindow = Any  # type: ignore[misc,assignment]

from ECL.api.bridge import guard_ipc_handler
from ECL.api.frontend import FrontendApi


class IpcCommandRegistry:
    """
    保存正式 IPC 命令的稳定名称。
    """

    command_names = (
        "frontend_ready",
        "launcher_errors_pending",
        "launcher_errors_ack",
        "system_ping",
        "system_memory",
        "launcher_info",
        "launcher_check_update",
        "launcher_update_status",
        "launcher_update_download",
        "launcher_update_apply",
        "info_card_get",
        "user_agreement_get",
        "user_agreement_save",
        "user_agreement_clear",
        "export_logs",
        "logs_get_history",
        "process_instances",
        "process_input",
        "process_stop",
        "debug_process_spawn",
        "debug_reset_launcher_data",
        "debug_clear_plugins",
        "debug_devtools_open",
        "settings_get",
        "settings_set",
        "frontend_log",
        "window_list",
        "window_open",
        "window_focus",
        "window_close",
        "window_update_bounds",
        "game_versions",
        "game_loader_versions",
        "game_fabric_api_versions",
        "game_scan",
        "game_java_scan",
        "game_install",
        "game_uninstall",
        "game_config_get",
        "game_config_set",
        "game_config_patch",
        "game_instances",
        "game_version_stats",
        "game_version_settings_get",
        "game_version_settings_effective",
        "game_version_settings_set",
        "game_instance_profile_get",
        "game_instance_profile_patch",
        "game_instance_profile_reset",
        "game_instance_icon_set",
        "game_instance_pin_order_set",
        "game_instance_categories_get",
        "game_instance_categories_upsert",
        "game_instance_categories_delete",
        "game_instance_shortcut_create",
        "game_instance_folder_open",
        "game_instance_mods_list",
        "game_instance_mod_toggle",
        "game_instance_mod_add",
        "game_instance_mod_remove",
        "game_instance_mods_folder_open",
        "game_instance_clone",
        "game_instance_import",
        "game_instance_export",
        "game_modpack_online_install",
        "game_instance_files_check",
        "game_instance_files_repair",
        "game_instance_delete",
        "game_operation_get",
        "game_operation_cancel",
        "game_world_list",
        "game_world_detail",
        "game_world_patch",
        "game_world_copy",
        "game_world_import",
        "game_world_export",
        "game_world_icon_set",
        "game_world_delete",
        "game_world_backup_list",
        "game_world_backup_create",
        "game_world_backup_restore",
        "game_world_backup_lock",
        "game_world_backup_delete",
        "game_options_read",
        "game_options_patch",
        "game_screenshot_list",
        "game_screenshot_thumbnail",
        "game_screenshot_copy",
        "game_screenshot_save_as",
        "game_screenshot_delete",
        "game_screenshot_set_cover",
        "game_screenshot_set_background",
        "game_server_list",
        "game_server_upsert",
        "game_server_delete",
        "game_server_reorder",
        "game_server_status_refresh",
        "game_resource_list",
        "game_resource_install",
        "game_resource_toggle",
        "game_resource_delete",
        "game_resource_manifest_export",
        "game_resource_search",
        "game_resource_identify",
        "game_resource_update_check",
        "game_resource_update",
        "game_schematic_preview",
        "game_schematic_assets",
        "game_schematic_material_manifest_export",
        "game_schematic_session_open",
        "game_schematic_session_chunks",
        "game_schematic_session_close",
        "game_launch",
        "game_launch_cancel",
        "game_instance_stop",
        "game_crash_list",
        "game_crash_analyze",
        "game_crash_output",
        "game_crash_export",
        "accounts_list",
        "accounts_current",
        "accounts_auth_providers",
        "accounts_add_plugin",
        "accounts_add_offline",
        "accounts_default_skins",
        "accounts_set_offline_skin",
        "accounts_add_authlib",
        "accounts_select_authlib_profile",
        "accounts_microsoft_login_config",
        "accounts_authlib_login_config",
        "accounts_start_microsoft_login",
        "accounts_poll_microsoft_login",
        "accounts_cancel_microsoft_login",
        "accounts_complete_microsoft_login",
        "accounts_switch",
        "accounts_remove",
        "accounts_refresh_profile",
        "accounts_set_favorite",
        "accounts_set_pinned",
        "accounts_texture_urls",
        "wardrobe_list",
        "wardrobe_import",
        "wardrobe_sync_account_skin",
        "wardrobe_update",
        "wardrobe_delete",
        "wardrobe_texture",
        "wardrobe_export",
        "wardrobe_apply_skin",
        "microsoft_reset_skin",
        "microsoft_set_cape",
        "microsoft_reset_cape",
        "authlib_resolve_server",
        "authlib_servers",
        "image_save_url",
        "image_fetch_data_url",
        "image_save_as",
        "image_read_file",
        "image_list_files",
        "background_video_open",
        "select_directory",
        "select_java",
        "select_image",
        "select_background_video",
        "select_file",
        "select_files",
        "select_save_file",
        "open_folder",
        "open_url",
        "file_resolve",
        "fs_exists",
        "fs_read_dir",
        "fs_read_file",
        "get_mods",
        "toggle_mod",
        "add_mod",
        "remove_mod",
        "open_mods_folder",
        "search_mods",
        "mod_source_config",
        "get_mod_info",
        "get_mod_versions",
        "download_mod",
        "download_mod_to_path",
        "plugin_list",
        "plugin_info",
        "plugin_enable",
        "plugin_disable",
        "plugin_unload",
        "plugin_reload",
        "plugin_install",
        "plugin_package_inspect",
        "plugin_get_routes",
        "plugin_get_slots",
        "plugin_get_vue_slots",
        "plugin_get_vue_components",
        "plugin_call_command",
        "plugin_get_settings",
        "plugin_update_setting",
        "plugin_notify_sidebar_state",
        "connector_nodes_get",
        "connector_nodes_set",
        "connector_status",
        "launcher_preload_connector",
        "connector_host_port",
        "connector_host_instance",
        "connector_join",
        "connector_leave",
        "connector_kick",
        "connector_match_instances",
        "connector_easytier_status",
        "connector_easytier_download",
        "connector_detect_ports",
        "connector_search_mc_port",
        "connector_nat_type",
    )


def command_handlers(api: FrontendApi) -> dict[str, Callable[..., Any]]:
    """
    返回 PyTauri 需要注册的唯一正式 IPC 命令表。

    :param api: 已连接应用上下文的前端 API 门面
    :return: 命令名到处理器的稳定映射
    """

    def build_dispatch(operation: str) -> Callable[..., Any]:
        handler = getattr(api, operation)
        parameters = handler.__annotations__

        async def dispatch(
            body: dict[str, Any], app_handle: AppHandle = None, webview_window: WebviewWindow = None
        ) -> Any:
            if webview_window is not None:
                denied = api.authorize_window_command(operation, body, webview_window)
                if denied is not None:
                    return denied
            kwargs: dict[str, Any] = {}
            if "app_handle" in parameters and app_handle is not None:
                kwargs["app_handle"] = app_handle
            if "webview_window" in parameters and webview_window is not None:
                kwargs["webview_window"] = webview_window
            return await handler(body, **kwargs)

        dispatch.__name__ = operation
        dispatch.__qualname__ = operation
        return guard_ipc_handler(api, operation, dispatch)

    return {name: build_dispatch(name) for name in IpcCommandRegistry.command_names}


__all__ = ["IpcCommandRegistry", "command_handlers"]
