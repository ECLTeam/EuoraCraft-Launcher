# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义正式 IPC 命令的可读名称和输出策略，限制轮询失败日志频率。
#
# 公开接口：
#   - class IpcLogSpec — 不包含请求内容的操作名称和记录策略。
#   - class IpcLogPolicy — 全部正式命令的日志策略表。
#   - class IpcLogRuntime — 每个 API 实例独立的轮询失败与恢复记录。
# ============================================================

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from types import MappingProxyType
from typing import ClassVar, Literal


@dataclass(frozen=True, slots=True)
class IpcLogSpec:
    """
    指定短操作名称、成功结果归属及必要的实际执行开始日志。

    普通查询成功静默；服务或任务拥有结果时，IPC 只处理尚未记录的失败。
    ``log_start`` 仅供边界确实包围实际执行的同步长操作，不用于后台提交。
    不保存请求体或结果数据，只有重型操作的结果保留有意义的耗时。
    """

    title: str
    kind: Literal["action", "query", "poll", "channel"]
    timed: bool = False
    result_owner: Literal["ipc", "service", "task", "silent"] = "ipc"
    log_start: bool = False


class IpcLogPolicy:
    """
    为每个正式命令明确选择日志策略，新增命令须同时维护该表。
    """

    cancellation_codes: ClassVar[frozenset[str]] = frozenset(
        {
            "SAVE_CANCELLED",
            "OPERATION_CANCELLED",
            "INSTALL_CANCELLED",
            "PACK_DOWNLOAD_CANCELLED",
            "LAUNCH_CANCELLED",
            "MICROSOFT_LOGIN_CANCELLED",
        }
    )

    commands: ClassVar[Mapping[str, IpcLogSpec]] = MappingProxyType(
        {
            "frontend_ready": IpcLogSpec("处理前端就绪", "action", result_owner="service"),
            "launcher_errors_pending": IpcLogSpec("查询待呈现错误", "poll"),
            "launcher_errors_ack": IpcLogSpec("确认错误已呈现", "action", result_owner="silent"),
            "custom_download_start": IpcLogSpec("提交自定义下载", "action", result_owner="task"),
            "custom_download_defaults": IpcLogSpec("读取下载默认设置", "query"),
            "custom_download_retry": IpcLogSpec("重试自定义下载", "action", result_owner="task"),
            "system_ping": IpcLogSpec("检查连接", "poll"),
            "system_memory": IpcLogSpec("获取内存信息", "poll"),
            "launcher_info": IpcLogSpec("读取启动器信息", "query"),
            "launcher_check_update": IpcLogSpec("检查启动器更新", "action", timed=True, result_owner="service"),
            "launcher_update_status": IpcLogSpec("查询更新可用状态", "poll"),
            "launcher_update_download": IpcLogSpec("下载启动器更新", "action", timed=True, result_owner="service"),
            "launcher_update_apply": IpcLogSpec("应用启动器更新", "action", result_owner="service"),
            "launcher_announcements_get": IpcLogSpec("获取启动器公告", "query", timed=True),
            "user_agreement_get": IpcLogSpec("读取协议接受状态", "query"),
            "user_agreement_save": IpcLogSpec("接受用户协议", "action"),
            "user_agreement_clear": IpcLogSpec("重置协议接受状态", "action"),
            "export_logs": IpcLogSpec("导出启动器日志", "action", timed=True, log_start=True),
            "logs_get_history": IpcLogSpec("读取日志历史", "channel"),
            "process_instances": IpcLogSpec("读取子进程列表", "poll"),
            "process_input": IpcLogSpec("发送子进程输入", "action"),
            "process_stop": IpcLogSpec("停止子进程", "action", result_owner="service"),
            "debug_process_spawn": IpcLogSpec("创建测试子进程", "action", result_owner="service"),
            "debug_reset_launcher_data": IpcLogSpec("还原启动器设置", "action"),
            "debug_clear_plugins": IpcLogSpec("清理插件数据", "action"),
            "debug_devtools_open": IpcLogSpec("打开开发者工具", "action"),
            "settings_get": IpcLogSpec("读取设置", "query"),
            "settings_set": IpcLogSpec("保存设置", "action", result_owner="service"),
            "settings_download_patch": IpcLogSpec("保存下载源与资源目标", "action", result_owner="service"),
            "frontend_log": IpcLogSpec("转发前端日志", "channel"),
            "window_list": IpcLogSpec("窗口：读取列表", "query"),
            "window_open": IpcLogSpec("打开窗口", "action", result_owner="service"),
            "window_focus": IpcLogSpec("激活窗口", "action", result_owner="silent"),
            "window_close": IpcLogSpec("窗口：关闭", "action"),
            "window_update_bounds": IpcLogSpec("调整窗口位置和大小", "action", result_owner="silent"),
            "game_versions": IpcLogSpec("查询游戏版本", "query", timed=True),
            "game_loader_versions": IpcLogSpec("查询加载器版本", "query", timed=True),
            "game_fabric_api_versions": IpcLogSpec("查询 Fabric API 版本", "query", timed=True),
            "game_scan": IpcLogSpec("扫描本地实例", "query", timed=True),
            "game_java_scan": IpcLogSpec("扫描 Java", "action", timed=True),
            "game_java_inventory": IpcLogSpec("查询 Java 管理清单", "query"),
            "game_java_register": IpcLogSpec("登记现有 Java", "action"),
            "game_java_set_enabled": IpcLogSpec("调整 Java 可用状态", "action"),
            "game_java_forget": IpcLogSpec("移除 Java 登记", "action"),
            "game_java_select": IpcLogSpec("验证 Java 选择", "query"),
            "game_java_catalog": IpcLogSpec("查询 Java 下载目录", "query", timed=True),
            "game_java_install_plan": IpcLogSpec("生成 Java 安装计划", "query"),
            "game_java_install": IpcLogSpec("安装 Java", "action", timed=True, result_owner="task"),
            "game_java_check_updates": IpcLogSpec("检查 Java 更新", "query", timed=True),
            "game_java_remove": IpcLogSpec("移除托管 Java", "action", timed=True, result_owner="task"),
            "game_java_cleanup": IpcLogSpec("清理 Java 遗留文件", "action", timed=True, result_owner="task"),
            "game_install": IpcLogSpec("安装游戏版本", "action", timed=True, result_owner="service"),
            "game_uninstall": IpcLogSpec("卸载游戏实例", "action"),
            "game_config_get": IpcLogSpec("读取目录配置", "query"),
            "game_config_set": IpcLogSpec("保存目录配置", "action", result_owner="service"),
            "game_config_patch": IpcLogSpec("更新目录配置", "action", result_owner="service"),
            "game_instances": IpcLogSpec("查询运行中实例", "poll"),
            "game_version_stats": IpcLogSpec("读取实例运行统计", "query"),
            "game_version_settings_get": IpcLogSpec("读取实例启动设置", "query"),
            "game_version_settings_effective": IpcLogSpec("读取实例有效设置", "query"),
            "game_version_settings_set": IpcLogSpec("保存实例启动设置", "action", result_owner="service"),
            "game_instance_profile_get": IpcLogSpec("读取实例资料", "query"),
            "game_instance_profile_patch": IpcLogSpec("保存实例资料", "action"),
            "game_instance_profile_reset": IpcLogSpec("重置实例资料", "action"),
            "game_instance_icon_set": IpcLogSpec("设置实例图标", "action"),
            "game_instance_pin_order_set": IpcLogSpec("保存实例置顶顺序", "action"),
            "game_instance_categories_get": IpcLogSpec("读取实例分类", "query"),
            "game_instance_categories_upsert": IpcLogSpec("保存实例分类", "action"),
            "game_instance_categories_delete": IpcLogSpec("删除实例分类", "action"),
            "game_instance_shortcut_create": IpcLogSpec("创建实例快捷方式", "action"),
            "game_instance_folder_open": IpcLogSpec("游戏实例：打开目录", "action"),
            "game_instance_mods_list": IpcLogSpec("实例模组：读取列表", "query"),
            "game_instance_mod_toggle": IpcLogSpec("实例模组：切换启用状态", "action"),
            "game_instance_mod_add": IpcLogSpec("实例模组：添加", "action"),
            "game_instance_mod_remove": IpcLogSpec("实例模组：删除", "action"),
            "game_instance_mods_folder_open": IpcLogSpec("打开实例模组目录", "action"),
            "game_instance_clone": IpcLogSpec("游戏实例：复制", "action", timed=True, result_owner="task"),
            "game_modpack_prepare": IpcLogSpec("读取整合包安装信息", "action", timed=True, result_owner="task"),
            "game_modpack_preparation_get": IpcLogSpec("查询整合包安装信息", "poll"),
            "game_modpack_preparation_release": IpcLogSpec("释放整合包预览", "action", result_owner="silent"),
            "game_modpack_install": IpcLogSpec("安装整合包", "action", timed=True, result_owner="task"),
            "game_instance_export": IpcLogSpec("游戏实例：导出", "action", timed=True, result_owner="task"),
            "game_instance_export_preview": IpcLogSpec("整合包导出：扫描文件", "query", timed=True),
            "game_instance_files_check": IpcLogSpec("实例文件：检查完整性", "action", timed=True, result_owner="task"),
            "game_instance_files_repair": IpcLogSpec("实例文件：修复", "action", timed=True, result_owner="task"),
            "game_instance_delete": IpcLogSpec("游戏实例：删除", "action"),
            "game_operation_get": IpcLogSpec("后台任务：读取状态", "poll"),
            "game_operation_cancel": IpcLogSpec("后台任务：请求取消", "action", result_owner="service"),
            "game_world_list": IpcLogSpec("存档：读取列表", "query"),
            "game_world_detail": IpcLogSpec("存档：读取详情", "query"),
            "game_world_patch": IpcLogSpec("存档：修改配置", "action"),
            "game_world_copy": IpcLogSpec("存档：复制", "action", timed=True, result_owner="task"),
            "game_world_import": IpcLogSpec("存档：导入", "action", timed=True, result_owner="task"),
            "game_world_export": IpcLogSpec("存档：导出", "action", timed=True, result_owner="task"),
            "game_world_icon_set": IpcLogSpec("存档：设置图标", "action"),
            "game_world_delete": IpcLogSpec("存档：删除", "action"),
            "game_world_backup_list": IpcLogSpec("存档备份：读取列表", "query"),
            "game_world_backup_create": IpcLogSpec("存档备份：创建", "action", timed=True, result_owner="task"),
            "game_world_backup_restore": IpcLogSpec("存档备份：恢复", "action", timed=True, result_owner="task"),
            "game_world_backup_lock": IpcLogSpec("存档备份：锁定", "action"),
            "game_world_backup_delete": IpcLogSpec("存档备份：删除", "action"),
            "game_options_read": IpcLogSpec("游戏选项：读取", "query"),
            "game_options_patch": IpcLogSpec("游戏选项：修改配置", "action"),
            "game_screenshot_list": IpcLogSpec("游戏截图：读取列表", "query"),
            "game_screenshot_thumbnail": IpcLogSpec("游戏截图：读取缩略图", "query"),
            "game_screenshot_copy": IpcLogSpec("游戏截图：复制", "action"),
            "game_screenshot_save_as": IpcLogSpec("游戏截图：另存为", "action"),
            "game_screenshot_delete": IpcLogSpec("游戏截图：删除", "action"),
            "game_screenshot_set_cover": IpcLogSpec("游戏截图：设置封面", "action"),
            "game_screenshot_set_background": IpcLogSpec("游戏截图：设置背景", "action"),
            "game_server_list": IpcLogSpec("服务器：读取列表", "query"),
            "game_server_upsert": IpcLogSpec("服务器：保存", "action"),
            "game_server_delete": IpcLogSpec("服务器：删除", "action"),
            "game_server_reorder": IpcLogSpec("服务器：调整顺序", "action"),
            "game_server_status_refresh": IpcLogSpec("服务器：刷新状态", "query"),
            "game_resource_list": IpcLogSpec("游戏资源：读取列表", "query"),
            "game_resource_install": IpcLogSpec("游戏资源：安装", "action", timed=True, result_owner="task"),
            "game_resource_toggle": IpcLogSpec("游戏资源：切换启用状态", "action"),
            "game_resource_delete": IpcLogSpec("游戏资源：删除", "action"),
            "game_resource_manifest_export": IpcLogSpec("游戏资源：导出清单", "action"),
            "game_resource_search": IpcLogSpec("游戏资源：搜索", "query", timed=True),
            "game_resource_identify": IpcLogSpec("游戏资源：识别", "query", timed=True),
            "game_resource_update_check": IpcLogSpec("游戏资源：检查更新", "query", timed=True),
            "game_resource_update": IpcLogSpec("游戏资源：更新", "action", timed=True, result_owner="task"),
            "game_schematic_preview": IpcLogSpec("投影：预览", "query"),
            "game_schematic_assets": IpcLogSpec("投影：读取素材", "query"),
            "game_schematic_material_manifest_export": IpcLogSpec("投影：导出材料清单", "action"),
            "game_schematic_session_open": IpcLogSpec("投影会话：打开", "action"),
            "game_schematic_session_chunks": IpcLogSpec("投影会话：读取区块", "query"),
            "game_schematic_session_close": IpcLogSpec("投影会话：关闭", "action"),
            "game_launch": IpcLogSpec("启动游戏", "action", timed=True, result_owner="service"),
            "game_launch_cancel": IpcLogSpec("取消游戏启动", "action", result_owner="service"),
            "game_instance_stop": IpcLogSpec("停止游戏实例", "action", result_owner="service"),
            "game_crash_list": IpcLogSpec("读取崩溃日志列表", "query"),
            "game_crash_analyze": IpcLogSpec("分析游戏崩溃", "action", timed=True),
            "game_crash_output": IpcLogSpec("读取崩溃输出", "query"),
            "game_crash_export": IpcLogSpec("导出崩溃报告", "action"),
            "accounts_list": IpcLogSpec("获取账户列表", "query"),
            "accounts_current": IpcLogSpec("获取当前账户", "query"),
            "accounts_auth_providers": IpcLogSpec("读取账户登录方式", "query"),
            "accounts_add_plugin": IpcLogSpec("通过插件认证提供方新增账户", "action"),
            "accounts_add_offline": IpcLogSpec("添加离线账户", "action"),
            "accounts_preview_offline_uuid": IpcLogSpec("预览离线账户 UUID", "query"),
            "accounts_default_skins": IpcLogSpec("获取可供离线账户选择的默认皮肤列表", "query"),
            "accounts_set_offline_skin": IpcLogSpec("设置离线账户的默认皮肤", "action"),
            "accounts_add_authlib": IpcLogSpec("添加外置登录账户", "action"),
            "accounts_select_authlib_profile": IpcLogSpec("选择外置账户角色", "action"),
            "accounts_microsoft_login_config": IpcLogSpec("获取微软登录配置", "query"),
            "accounts_littleskin_login_config": IpcLogSpec("读取 LittleSkin 登录配置", "query"),
            "accounts_littleskin_management": IpcLogSpec("读取 LittleSkin 账户管理", "query"),
            "accounts_littleskin_closet": IpcLogSpec("读取 LittleSkin 云端衣柜", "query"),
            "accounts_littleskin_set_texture": IpcLogSpec("更换 LittleSkin 材质", "action", result_owner="service"),
            "accounts_littleskin_reset_texture": IpcLogSpec("清除 LittleSkin 材质", "action", result_owner="service"),
            "wardrobe_import_littleskin_texture": IpcLogSpec("下载 LittleSkin 云端材质", "action"),
            "accounts_start_littleskin_login": IpcLogSpec("提交 LittleSkin 登录", "action", result_owner="service"),
            "accounts_poll_littleskin_login": IpcLogSpec("查询 LittleSkin 登录状态", "poll"),
            "accounts_select_littleskin_profile": IpcLogSpec("选择 LittleSkin 角色", "action", result_owner="service"),
            "accounts_complete_littleskin_login": IpcLogSpec("保存 LittleSkin 账户", "action", result_owner="service"),
            "accounts_cancel_littleskin_login": IpcLogSpec("取消 LittleSkin 登录", "action", result_owner="silent"),
            "accounts_authlib_login_config": IpcLogSpec("获取外置登录可用性配置", "query"),
            "accounts_start_microsoft_login": IpcLogSpec("开始微软登录", "action", timed=True),
            "accounts_poll_microsoft_login": IpcLogSpec("获取微软登录状态", "poll"),
            "accounts_cancel_microsoft_login": IpcLogSpec("取消微软登录", "action", result_owner="service"),
            "accounts_complete_microsoft_login": IpcLogSpec("完成微软登录", "action", timed=True),
            "accounts_switch": IpcLogSpec("切换账户", "action", result_owner="service"),
            "accounts_remove": IpcLogSpec("删除账户", "action", result_owner="service"),
            "accounts_refresh_profile": IpcLogSpec("刷新账户信息", "action", result_owner="service"),
            "accounts_set_favorite": IpcLogSpec("设置账户是否收藏", "action"),
            "accounts_set_pinned": IpcLogSpec("设置账户是否置顶", "action"),
            "accounts_texture_urls": IpcLogSpec("读取账户材质地址", "query"),
            "wardrobe_list": IpcLogSpec("读取衣柜列表", "query"),
            "wardrobe_import": IpcLogSpec("导入衣柜纹理", "action"),
            "wardrobe_sync_account_texture": IpcLogSpec(
                "同步账户材质到衣柜", "action", timed=True, result_owner="service"
            ),
            "wardrobe_update": IpcLogSpec("修改衣柜条目", "action"),
            "wardrobe_delete": IpcLogSpec("删除衣柜条目", "action"),
            "wardrobe_texture": IpcLogSpec("读取衣柜纹理", "query"),
            "wardrobe_export": IpcLogSpec("导出衣柜纹理", "action", result_owner="service"),
            "wardrobe_apply_skin": IpcLogSpec("上传衣柜皮肤", "action", timed=True, result_owner="service"),
            "microsoft_reset_skin": IpcLogSpec("将正版账户皮肤重置为默认", "action", timed=True),
            "microsoft_set_cape": IpcLogSpec("为正版账户选择已解锁的披风", "action", timed=True),
            "microsoft_reset_cape": IpcLogSpec("取消正版账户当前佩戴的披风", "action", timed=True),
            "authlib_resolve_server": IpcLogSpec("解析外置登录地址", "query", timed=True),
            "authlib_servers": IpcLogSpec("获取外置登录服务器", "query"),
            "image_save_url": IpcLogSpec("下载背景图片并缓存到本地数据目录", "action", timed=True),
            "image_fetch_data_url": IpcLogSpec("读取远程图片", "query", timed=True),
            "image_save_as": IpcLogSpec("保存背景图片", "action"),
            "skin_avatar_export": IpcLogSpec("导出皮肤头像", "action"),
            "image_read_file": IpcLogSpec("读取本地图片", "query", timed=True),
            "image_list_files": IpcLogSpec("获取图片列表", "query"),
            "background_video_open": IpcLogSpec("打开背景视频流", "action"),
            "select_directory": IpcLogSpec("选择目录", "query"),
            "select_java": IpcLogSpec("选择 Java", "query"),
            "select_image": IpcLogSpec("选择图片", "query"),
            "select_background_video": IpcLogSpec("打开本地背景视频选择器", "query"),
            "select_file": IpcLogSpec("选择文件", "query"),
            "select_files": IpcLogSpec("选择多个文件", "query"),
            "select_save_file": IpcLogSpec("选择保存位置", "query"),
            "open_folder": IpcLogSpec("打开目录", "action"),
            "open_url": IpcLogSpec("打开链接", "action"),
            "file_resolve": IpcLogSpec("解析本地路径", "query"),
            "fs_exists": IpcLogSpec("查询路径类型", "query"),
            "fs_read_dir": IpcLogSpec("读取目录条目", "query"),
            "fs_read_file": IpcLogSpec("读取本地文件", "query"),
            "get_mods": IpcLogSpec("读取本地模组", "query"),
            "toggle_mod": IpcLogSpec("切换模组启用状态", "action"),
            "add_mod": IpcLogSpec("添加本地模组", "action"),
            "remove_mod": IpcLogSpec("删除本地模组", "action"),
            "open_mods_folder": IpcLogSpec("打开模组目录", "action"),
            "search_mods": IpcLogSpec("搜索在线模组", "query", timed=True),
            "mod_source_config": IpcLogSpec("读取资源来源状态", "query"),
            "get_mod_info": IpcLogSpec("获取在线模组项目详情", "query", timed=True),
            "get_mod_versions": IpcLogSpec("获取在线模组兼容版本列表", "query", timed=True),
            "download_mod": IpcLogSpec("安装在线模组", "action", timed=True, log_start=True),
            "download_mod_to_path": IpcLogSpec("保存在线模组文件", "action", timed=True, log_start=True),
            "plugin_permissions_get": IpcLogSpec("查询插件权限", "query"),
            "plugin_panels_get": IpcLogSpec("查询已获准插件提供的操作面板", "query"),
            "plugin_panel_data": IpcLogSpec("拉取插件面板当前数据", "query", timed=True),
            "plugin_panel_action": IpcLogSpec("执行插件面板操作", "action", timed=True),
            "plugin_permission_resources": IpcLogSpec("读取插件可审阅资源", "query"),
            "plugin_permissions_update": IpcLogSpec("审阅和更新插件授权", "action"),
            "plugin_runtime_info": IpcLogSpec("查询插件运行状态", "query"),
            "plugin_development_get": IpcLogSpec("查询插件开发模式", "query"),
            "plugin_open_directory": IpcLogSpec("打开插件开发目录", "action"),
            "plugin_list": IpcLogSpec("获取插件列表", "query"),
            "plugin_info": IpcLogSpec("获取插件信息", "query"),
            "plugin_enable": IpcLogSpec("启用插件", "action", result_owner="service"),
            "plugin_disable": IpcLogSpec("禁用插件", "action", result_owner="service"),
            "plugin_unload": IpcLogSpec("卸载并删除用户插件", "action", result_owner="service"),
            "plugin_reload": IpcLogSpec("重新加载插件", "action"),
            "plugin_install": IpcLogSpec("安装插件", "action"),
            "plugin_package_inspect": IpcLogSpec("检查插件安装包", "query"),
            "plugin_get_routes": IpcLogSpec("获取插件路由", "query"),
            "plugin_get_slots": IpcLogSpec("获取插件插槽", "query"),
            "plugin_get_vue_slots": IpcLogSpec("获取插件 Vue 插槽", "query"),
            "plugin_get_vue_components": IpcLogSpec("获取插件 Vue 组件", "query"),
            "plugin_call_command": IpcLogSpec("调用插件命令", "action"),
            "plugin_get_settings": IpcLogSpec("获取插件设置", "query"),
            "plugin_update_setting": IpcLogSpec("更新插件设置", "action"),
            "plugin_notify_sidebar_state": IpcLogSpec("同步侧栏折叠状态", "action", result_owner="silent"),
            "connector_nodes_get": IpcLogSpec("读取联机节点配置", "query"),
            "connector_nodes_set": IpcLogSpec("保存联机节点配置", "action", result_owner="service"),
            "connector_status": IpcLogSpec("查询联机状态", "poll"),
            "launcher_preload_connector": IpcLogSpec("预加载联机节点", "query", timed=True),
            "connector_host_port": IpcLogSpec("创建联机房间", "action", timed=True, result_owner="service"),
            "connector_host_instance": IpcLogSpec(
                "为游戏实例创建联机房间", "action", timed=True, result_owner="service"
            ),
            "connector_join": IpcLogSpec("加入联机房间", "action", timed=True, result_owner="service"),
            "connector_leave": IpcLogSpec("退出联机房间", "action", result_owner="service"),
            "connector_kick": IpcLogSpec("移出房间玩家", "action", result_owner="service"),
            "connector_detect_ports": IpcLogSpec("探测服务端口", "action", timed=True),
            "connector_search_mc_port": IpcLogSpec("查找游戏服务端口", "action", timed=True),
            "connector_nat_type": IpcLogSpec("检测网络 NAT 类型", "action", timed=True, result_owner="service"),
        }
    )


class IpcLogRuntime:
    """
    限制同一轮询失败的重复输出，恢复成功后只记录一次。
    """

    repeat_interval_seconds: float = 30.0

    def __init__(self) -> None:
        """
        创建当前 API 实例独立的失败记录与并发锁。
        """
        self._failures: dict[str, tuple[str, float]] = {}
        self._lock = RLock()

    def record_failure(self, command: str, code: str) -> bool:
        """
        判断本次轮询失败是否需要输出，错误码变化时立即记录。

        :param command: 正式命令名称
        :param code: 稳定失败码
        :return: 首次失败、失败码变化或超过采样间隔时返回 True
        """
        now = monotonic()
        with self._lock:
            previous = self._failures.get(command)
            if previous and previous[0] == code and now - previous[1] < self.repeat_interval_seconds:
                return False
            self._failures[command] = (code, now)
            return True

    def record_recovery(self, command: str) -> bool:
        """
        清除轮询失败记录，恢复日志只输出一次。

        :param command: 正式命令名称
        :return: 该命令之前有失败记录时返回 True
        """
        with self._lock:
            return self._failures.pop(command, None) is not None
