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
    指定操作名称，以及开始、成功和失败日志的输出级别。

    界面状态同步可以降低开始与成功日志的级别，失败日志仍由 IPC 处理器
    按原策略输出；不保存请求体或结果数据。``timed`` 只对网络或重型操作
    置位，使日志保留有意义的耗时而不为本地轻量读写输出 0 毫秒。
    """

    title: str
    kind: Literal["action", "query", "poll", "channel"]
    normal_level: Literal["info", "debug"] = "info"
    timed: bool = False


class IpcLogPolicy:
    """
    为每个正式命令明确选择日志策略，新增命令须同时维护该表。
    """

    commands: ClassVar[Mapping[str, IpcLogSpec]] = MappingProxyType(
        {
            "frontend_ready": IpcLogSpec("处理前端就绪", "action"),
            "launcher_errors_pending": IpcLogSpec("返回尚未被前端确认呈现的严重错误", "poll"),
            "launcher_errors_ack": IpcLogSpec("确认前端已经接收一批严重错误并释放其内存副本", "action"),
            "custom_download_start": IpcLogSpec("提交自定义下载", "action"),
            "custom_download_defaults": IpcLogSpec("读取本次应用实际数据目录对应的下载初始设置，不创建目录", "query"),
            "custom_download_retry": IpcLogSpec("将本会话失败或取消的下载重新登记为新任务", "action"),
            "system_ping": IpcLogSpec("检查连接", "poll"),
            "system_memory": IpcLogSpec("获取内存信息", "poll"),
            "launcher_info": IpcLogSpec("获取启动器信息及本次运行的主窗口模式", "query"),
            "launcher_check_update": IpcLogSpec(
                "按当前版本通道检查 GitHub Releases 是否有新版本", "action", timed=True
            ),
            "launcher_update_status": IpcLogSpec("返回当前运行形态是否允许自动更新", "poll"),
            "launcher_update_download": IpcLogSpec("下载当前通道最新版本的安装包并落盘替换计划", "action", timed=True),
            "launcher_update_apply": IpcLogSpec("拉起引导脚本完成替换，并请求当前启动器退出以重启", "action"),
            "info_card_get": IpcLogSpec("获取信息卡片", "query", timed=True),
            "user_agreement_get": IpcLogSpec("读取当前用户协议接受状态", "query"),
            "user_agreement_save": IpcLogSpec("保存用户已接受协议的本地状态", "action"),
            "user_agreement_clear": IpcLogSpec("清除用户协议接受状态，但保留匿名标识供再次确认", "action"),
            "export_logs": IpcLogSpec("将当前启动器日志打包为 ZIP 文件", "action", timed=True),
            "logs_get_history": IpcLogSpec("返回最近缓存的启动器日志，供前端日志终端打开时补全历史", "channel"),
            "process_instances": IpcLogSpec("返回注册表内登记的子进程实例列表", "poll"),
            "process_input": IpcLogSpec("向指定子进程实例写入一行标准输入", "action"),
            "process_stop": IpcLogSpec("停止指定子进程实例；Minecraft 实例转发到游戏服务以正确结算统计", "action"),
            "debug_process_spawn": IpcLogSpec("在调试模式下启动一个子进程实例，供开发自测实例终端", "action"),
            "debug_reset_launcher_data": IpcLogSpec("还原启动器设置，并保留已登录账户", "action"),
            "debug_clear_plugins": IpcLogSpec("清理插件数据", "action"),
            "debug_devtools_open": IpcLogSpec("打开 WebView 开发者工具（F12 调试窗口）", "action"),
            "settings_get": IpcLogSpec("读取设置", "query"),
            "settings_set": IpcLogSpec("保存设置", "action"),
            "settings_download_patch": IpcLogSpec("保存下载源与资源目标", "action"),
            "frontend_log": IpcLogSpec("记录前端通过 IPC 上报的运行日志，供统一归档排查", "channel"),
            "window_list": IpcLogSpec("窗口：读取列表", "query"),
            "window_open": IpcLogSpec("窗口：打开", "action"),
            "window_focus": IpcLogSpec("激活窗口", "action"),
            "window_close": IpcLogSpec("窗口：关闭", "action"),
            "window_update_bounds": IpcLogSpec("调整窗口位置和大小", "action"),
            "game_versions": IpcLogSpec("查询 Minecraft 版本列表或分类目录", "query", timed=True),
            "game_loader_versions": IpcLogSpec("查询指定 Minecraft 版本可用的模组加载器版本", "query", timed=True),
            "game_fabric_api_versions": IpcLogSpec(
                "查询指定 Minecraft 版本可用的 Fabric API 版本", "query", timed=True
            ),
            "game_scan": IpcLogSpec("扫描一个或多个 Minecraft 根目录中的本地实例", "action", timed=True),
            "game_java_scan": IpcLogSpec("扫描系统和用户配置路径中的 Java 运行时", "action", timed=True),
            "game_java_inventory": IpcLogSpec("查询 Java 管理清单", "query"),
            "game_java_register": IpcLogSpec("登记现有 Java", "action"),
            "game_java_set_enabled": IpcLogSpec("调整 Java 可用状态", "action"),
            "game_java_forget": IpcLogSpec("移除 Java 登记", "action"),
            "game_java_select": IpcLogSpec("验证 Java 选择", "query"),
            "game_java_catalog": IpcLogSpec("查询 Java 下载目录", "query", timed=True),
            "game_java_install_plan": IpcLogSpec("生成 Java 安装计划", "query"),
            "game_java_install": IpcLogSpec("安装 Java", "action", timed=True),
            "game_java_check_updates": IpcLogSpec("检查 Java 更新", "query", timed=True),
            "game_java_remove": IpcLogSpec("移除托管 Java", "action", timed=True),
            "game_java_cleanup": IpcLogSpec("清理 Java 遗留文件", "action", timed=True),
            "game_install": IpcLogSpec("安装游戏版本", "action", timed=True),
            "game_uninstall": IpcLogSpec("从指定 Minecraft 根目录卸载一个实例", "action"),
            "game_config_get": IpcLogSpec("读取 Minecraft 根目录中的 ecl.json", "query"),
            "game_config_set": IpcLogSpec("原子替换 Minecraft 根目录中的 ecl.json", "action"),
            "game_config_patch": IpcLogSpec("合并更新 Minecraft 根目录中的 ecl.json", "action"),
            "game_instances": IpcLogSpec("返回由启动器管理的运行中 Minecraft 实例", "poll"),
            "game_version_stats": IpcLogSpec("返回指定 Minecraft 版本的持久化运行统计", "query"),
            "game_version_settings_get": IpcLogSpec("读取版本目录中的独立启动设置", "query"),
            "game_version_settings_effective": IpcLogSpec(
                "查询与实际启动相同的实例有效设置，供界面展示继承结果", "query"
            ),
            "game_version_settings_set": IpcLogSpec("原子写入版本目录中的独立启动设置", "action"),
            "game_instance_profile_get": IpcLogSpec("读取单个实例的 ECL 原始覆盖资料", "query"),
            "game_instance_profile_patch": IpcLogSpec("合并保存实例资料覆盖字段", "action"),
            "game_instance_profile_reset": IpcLogSpec("删除指定实例覆盖字段，使其恢复自动解析", "action"),
            "game_instance_icon_set": IpcLogSpec("设置实例自动、内置、加载器或本地图片图标", "action"),
            "game_instance_pin_order_set": IpcLogSpec("保存全部置顶实例的拖拽顺序", "action"),
            "game_instance_categories_get": IpcLogSpec("返回内置与用户自定义实例分类", "query"),
            "game_instance_categories_upsert": IpcLogSpec("新建或更新用户自定义实例分类", "action"),
            "game_instance_categories_delete": IpcLogSpec("删除用户自定义实例分类", "action"),
            "game_instance_shortcut_create": IpcLogSpec(
                "以实例有效图标创建 Windows 桌面或指定位置的快捷方式", "action"
            ),
            "game_instance_folder_open": IpcLogSpec("游戏实例：打开目录", "action"),
            "game_instance_mods_list": IpcLogSpec("实例模组：读取列表", "query"),
            "game_instance_mod_toggle": IpcLogSpec("实例模组：切换启用状态", "action"),
            "game_instance_mod_add": IpcLogSpec("实例模组：添加", "action"),
            "game_instance_mod_remove": IpcLogSpec("实例模组：删除", "action"),
            "game_instance_mods_folder_open": IpcLogSpec("打开实例模组目录", "action"),
            "game_instance_clone": IpcLogSpec("游戏实例：复制", "action", timed=True),
            "game_instance_import": IpcLogSpec("游戏实例：导入", "action", timed=True),
            "game_instance_export": IpcLogSpec("游戏实例：导出", "action", timed=True),
            "game_modpack_online_install": IpcLogSpec("在线安装整合包", "action", timed=True),
            "game_instance_files_check": IpcLogSpec("实例文件：检查完整性", "action", timed=True),
            "game_instance_files_repair": IpcLogSpec("实例文件：修复", "action", timed=True),
            "game_instance_delete": IpcLogSpec("游戏实例：删除", "action"),
            "game_operation_get": IpcLogSpec("后台任务：读取状态", "poll"),
            "game_operation_cancel": IpcLogSpec("后台任务：请求取消", "action"),
            "game_world_list": IpcLogSpec("存档：读取列表", "query"),
            "game_world_detail": IpcLogSpec("存档：读取详情", "action"),
            "game_world_patch": IpcLogSpec("存档：修改配置", "action"),
            "game_world_copy": IpcLogSpec("存档：复制", "action", timed=True),
            "game_world_import": IpcLogSpec("存档：导入", "action", timed=True),
            "game_world_export": IpcLogSpec("存档：导出", "action", timed=True),
            "game_world_icon_set": IpcLogSpec("存档：设置图标", "action"),
            "game_world_delete": IpcLogSpec("存档：删除", "action"),
            "game_world_backup_list": IpcLogSpec("存档备份：读取列表", "query"),
            "game_world_backup_create": IpcLogSpec("存档备份：创建", "action", timed=True),
            "game_world_backup_restore": IpcLogSpec("存档备份：恢复", "action", timed=True),
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
            "game_server_status_refresh": IpcLogSpec("服务器：刷新状态", "action"),
            "game_resource_list": IpcLogSpec("游戏资源：读取列表", "query"),
            "game_resource_install": IpcLogSpec("游戏资源：安装", "action", timed=True),
            "game_resource_toggle": IpcLogSpec("游戏资源：切换启用状态", "action"),
            "game_resource_delete": IpcLogSpec("游戏资源：删除", "action"),
            "game_resource_manifest_export": IpcLogSpec("游戏资源：导出清单", "action"),
            "game_resource_search": IpcLogSpec("游戏资源：搜索", "query", timed=True),
            "game_resource_identify": IpcLogSpec("游戏资源：识别", "query", timed=True),
            "game_resource_update_check": IpcLogSpec("游戏资源：检查更新", "query", timed=True),
            "game_resource_update": IpcLogSpec("游戏资源：更新", "action", timed=True),
            "game_schematic_preview": IpcLogSpec("投影：预览", "query"),
            "game_schematic_assets": IpcLogSpec("投影：读取素材", "query"),
            "game_schematic_material_manifest_export": IpcLogSpec("投影：导出材料清单", "action"),
            "game_schematic_session_open": IpcLogSpec("投影会话：打开", "action"),
            "game_schematic_session_chunks": IpcLogSpec("投影会话：读取区块", "query"),
            "game_schematic_session_close": IpcLogSpec("投影会话：关闭", "action"),
            "game_launch": IpcLogSpec("校验启动参数、补全文件并创建 Minecraft 进程", "action", timed=True),
            "game_launch_cancel": IpcLogSpec("取消当前仍处于准备或文件补全阶段的启动任务", "action"),
            "game_instance_stop": IpcLogSpec("通知指定的运行中 Minecraft 实例退出，超时后才强制结束", "action"),
            "game_crash_list": IpcLogSpec("列出指定实例文件夹内可分析的候选日志文件", "query"),
            "game_crash_analyze": IpcLogSpec(
                "在指定版本上下文中分析用户选择的 Minecraft 日志或 ZIP", "action", timed=True
            ),
            "game_crash_output": IpcLogSpec("按需读取当前会话报告中的脱敏游戏输出", "query"),
            "game_crash_export": IpcLogSpec("将当前会话报告导出为经过脱敏的 ZIP", "action"),
            "accounts_list": IpcLogSpec("获取账户列表", "query"),
            "accounts_current": IpcLogSpec("获取当前账户", "query"),
            "accounts_auth_providers": IpcLogSpec("获取插件注册的全部认证提供方定义", "query"),
            "accounts_add_plugin": IpcLogSpec("通过插件认证提供方新增账户", "action"),
            "accounts_add_offline": IpcLogSpec("添加离线账户", "action"),
            "accounts_default_skins": IpcLogSpec("获取可供离线账户选择的默认皮肤列表", "query"),
            "accounts_set_offline_skin": IpcLogSpec("设置离线账户的默认皮肤", "action"),
            "accounts_add_authlib": IpcLogSpec("添加外置登录账户", "action"),
            "accounts_select_authlib_profile": IpcLogSpec("为多角色外置账户选择本次登录使用的单个角色", "action"),
            "accounts_microsoft_login_config": IpcLogSpec("获取微软登录配置", "query"),
            "accounts_authlib_login_config": IpcLogSpec("获取外置登录可用性配置", "query"),
            "accounts_start_microsoft_login": IpcLogSpec("开始微软登录", "action", timed=True),
            "accounts_poll_microsoft_login": IpcLogSpec("获取微软登录状态", "poll"),
            "accounts_cancel_microsoft_login": IpcLogSpec("取消微软登录", "action"),
            "accounts_complete_microsoft_login": IpcLogSpec("完成微软登录", "action", timed=True),
            "accounts_switch": IpcLogSpec("切换账户", "action"),
            "accounts_remove": IpcLogSpec("删除账户", "action"),
            "accounts_refresh_profile": IpcLogSpec("刷新账户信息", "action"),
            "accounts_set_favorite": IpcLogSpec("设置账户是否收藏", "action"),
            "accounts_set_pinned": IpcLogSpec("设置账户是否置顶", "action"),
            "accounts_texture_urls": IpcLogSpec("返回账户完整皮肤与当前披风地址，图片裁切和渲染由前端完成", "query"),
            "wardrobe_list": IpcLogSpec("返回本地衣柜元数据，不包含纹理字节或本地绝对路径", "query"),
            "wardrobe_import": IpcLogSpec("将用户选择的 PNG 复制到启动器衣柜，并返回去重结果", "action"),
            "wardrobe_sync_account_skin": IpcLogSpec(
                "将账户当前穿戴的远程皮肤下载到本地衣柜，重复纹理沿用已有条目", "action", timed=True
            ),
            "wardrobe_update": IpcLogSpec("修改衣柜条目的名称或皮肤模型，不转换原始图片", "action"),
            "wardrobe_delete": IpcLogSpec("删除本地收藏；已经上传到外部账户的皮肤不受影响", "action"),
            "wardrobe_texture": IpcLogSpec("将衣柜中的小型 PNG 原样编码为 Data URL 供 WebView 渲染", "query"),
            "wardrobe_export": IpcLogSpec(
                "通过原生保存对话框导出衣柜中的原始 PNG，不经过前端 Base64 往返传输", "action"
            ),
            "wardrobe_apply_skin": IpcLogSpec(
                "将衣柜中的标准 64×64 皮肤上传到指定 Microsoft 账户", "action", timed=True
            ),
            "microsoft_reset_skin": IpcLogSpec("将正版账户皮肤重置为默认", "action", timed=True),
            "microsoft_set_cape": IpcLogSpec("为正版账户选择已解锁的披风", "action", timed=True),
            "microsoft_reset_cape": IpcLogSpec("取消正版账户当前佩戴的披风", "action", timed=True),
            "authlib_resolve_server": IpcLogSpec("解析外置登录网站实际使用的 API 地址", "query", timed=True),
            "authlib_servers": IpcLogSpec("获取外置登录服务器", "query"),
            "image_save_url": IpcLogSpec("下载背景图片并缓存到本地数据目录", "action", timed=True),
            "image_fetch_data_url": IpcLogSpec("下载远程图片并转换为受大小限制的 Data URL", "query", timed=True),
            "image_save_as": IpcLogSpec("保存背景图片", "action"),
            "skin_avatar_export": IpcLogSpec("导出皮肤头像", "action"),
            "image_read_file": IpcLogSpec("读取图片（带 LRU 缓存）", "query", timed=True),
            "image_list_files": IpcLogSpec("获取图片列表", "query"),
            "background_video_open": IpcLogSpec("为配置中的背景视频签发仅限当前进程使用的本地流 URL", "action"),
            "select_directory": IpcLogSpec("按用途选择游戏目录或下载文件夹，取消时保留前端原值", "query"),
            "select_java": IpcLogSpec("选择 Java", "query"),
            "select_image": IpcLogSpec("按使用场景选择图片；皮肤和披风只允许 PNG", "query"),
            "select_background_video": IpcLogSpec("打开本地背景视频选择器", "query"),
            "select_file": IpcLogSpec("选择文件", "query"),
            "select_files": IpcLogSpec("按实例工作台用途选择多个本地文件", "query"),
            "select_save_file": IpcLogSpec("按导出用途打开系统 ZIP 文件保存对话框", "query"),
            "open_folder": IpcLogSpec("打开目录", "action"),
            "open_url": IpcLogSpec("打开链接", "action"),
            "file_resolve": IpcLogSpec("规整本地路径，供前端转换为可访问的资源 URL", "query"),
            "fs_exists": IpcLogSpec("查询本地路径类型，不修改文件系统", "query"),
            "fs_read_dir": IpcLogSpec("读取指定目录的一层条目及基础元数据", "query"),
            "fs_read_file": IpcLogSpec("以 UTF-8 文本或 Base64 读取大小受限的本地文件", "query"),
            "get_mods": IpcLogSpec("列出指定 Minecraft 根目录中的本地模组", "query"),
            "toggle_mod": IpcLogSpec("切换指定本地模组的启用状态", "action"),
            "add_mod": IpcLogSpec("将用户选择的 Jar 文件复制到目标 mods 目录", "action"),
            "remove_mod": IpcLogSpec("删除目标 mods 目录中的一个文件", "action"),
            "open_mods_folder": IpcLogSpec("创建并使用系统文件管理器打开目标 mods 目录", "action"),
            "search_mods": IpcLogSpec(
                "搜索在线模组（Modrinth/CurseForge），映射为前端在线模组卡片结构", "query", timed=True
            ),
            "mod_source_config": IpcLogSpec("返回在线资源来源的可用性配置，供前端禁用未配置的来源选项", "query"),
            "get_mod_info": IpcLogSpec("获取在线模组项目详情", "query", timed=True),
            "get_mod_versions": IpcLogSpec("获取在线模组兼容版本列表", "query", timed=True),
            "download_mod": IpcLogSpec("下载在线模组到目标实例的 mods 目录", "action", timed=True),
            "download_mod_to_path": IpcLogSpec(
                "下载在线模组文件到用户指定的保存路径，不安装到任何实例", "action", timed=True
            ),
            "plugin_list": IpcLogSpec("获取插件列表", "query"),
            "plugin_info": IpcLogSpec("获取插件信息", "query"),
            "plugin_enable": IpcLogSpec("启用插件", "action"),
            "plugin_disable": IpcLogSpec("禁用插件", "action"),
            "plugin_unload": IpcLogSpec("卸载并删除用户插件", "action"),
            "plugin_reload": IpcLogSpec("重新加载插件", "action"),
            "plugin_install": IpcLogSpec("安装插件", "action"),
            "plugin_package_inspect": IpcLogSpec("在用户确认安装前校验归档并返回当前目标的来源与依赖摘要", "query"),
            "plugin_get_routes": IpcLogSpec("获取插件路由", "query"),
            "plugin_get_slots": IpcLogSpec("获取插件插槽", "query"),
            "plugin_get_vue_slots": IpcLogSpec("获取插件 Vue 插槽", "query"),
            "plugin_get_vue_components": IpcLogSpec("获取插件 Vue 组件", "query"),
            "plugin_call_command": IpcLogSpec("调用插件命令", "action"),
            "plugin_get_settings": IpcLogSpec("获取插件设置", "query"),
            "plugin_update_setting": IpcLogSpec("更新插件设置", "action"),
            "plugin_notify_sidebar_state": IpcLogSpec("通知插件侧栏的折叠状态", "action", normal_level="debug"),
            "connector_nodes_get": IpcLogSpec("读取联机节点配置", "query"),
            "connector_nodes_set": IpcLogSpec("保存联机节点配置", "action"),
            "connector_status": IpcLogSpec("查询联机服务的当前状态", "poll"),
            "launcher_preload_connector": IpcLogSpec("请求后台加载公共联机节点", "query", timed=True),
            "connector_host_port": IpcLogSpec("创建联机房间", "action", timed=True),
            "connector_host_instance": IpcLogSpec("为游戏实例创建联机房间", "action", timed=True),
            "connector_join": IpcLogSpec("加入联机房间", "action", timed=True),
            "connector_leave": IpcLogSpec("退出联机房间", "action"),
            "connector_kick": IpcLogSpec("移出房间玩家", "action"),
            "connector_detect_ports": IpcLogSpec("探测本机 Java 进程开放的候选端口", "action", timed=True),
            "connector_search_mc_port": IpcLogSpec("在候选端口中搜索确认 Minecraft 服务端口", "action", timed=True),
            "connector_nat_type": IpcLogSpec("检测网络 NAT 类型", "action", timed=True),
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
