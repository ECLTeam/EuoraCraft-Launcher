# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：JSON 配置存储：分节读写与默认游戏目录探测。
#
# 公开接口：
#   - class ConfigStore — 以 JSON 文件为载体的启动器配置存储，归应用上下文所有。
#       - default_minecraft_path() -> Path — 获取默认 Minecraft 目录。
#       - get_config(section=…) -> Any — 获取配置数据
#       - list_sections() -> list[str] — 获取全部配置分区名称
#       - get_many(sections) -> dict[str, Any] — 批量获取配置分区
#       - save_config(section, data) -> None — 保存配置分区
# ============================================================

from __future__ import annotations

import json
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

from ECL.events import EventBus
from ECL.utils.errors import ConfigError, ConfigValidationError
from ECL.utils.files import atomic_write_text
from ECL.utils.logging import get_logger

default_config: dict[str, Any] = {
    "launcher": {
        "debug": False,
        "debug_log_level": "info",
        "disable_ssl_verify": False,
        # 启动器通道代理（账户登录、元数据等），与下载通道相互独立。
        "api_proxy_mode": "none",
        "api_proxy_url": "",
        # 下载通道代理（游戏文件、Mod 与资源包），经 ECL_DOWNLOAD_PROXY 下发。
        "proxy_mode": "none",
        "proxy_url": "",
        "request_timeout": 15,
        "request_retries": 2,
        # 开发者通道：向插件开发工具箱暴露本地 WebSocket 服务。
        "dev_channel": False,
        # 单实例互斥：同一数据目录仅允许一个主实例，重复启动时激活已运行窗口。
        "single_instance": True,
    },
    "game": {
        "minecraft_paths": [],
        "java_auto": True,
        "java_path": "",
        "memory_auto": True,
        "memory_size": 4096,
        # 锁定初始堆与最大堆一致，避免游戏启动后频繁扩容/收缩导致卡顿。
        "lock_memory": False,
        # 游戏进程优先级: idle / below_normal / normal / above_normal / high。
        "process_priority": "normal",
        "game_width": 854,
        "game_height": 480,
        "jvm_args": [],
        # Windows 下使用 mesa-loader-windows 提供的兼容渲染后端。
        "renderer": "default",
        "fullscreen": False,
        # 实例未单独指定时的游戏目录隔离策略。
        "instance_isolation_policy": "all",
        # 在 Minecraft 原始游戏参数后追加的全局参数，使用与命令行一致的引号规则。
        "game_args_tail": "",
        # 在创建 Minecraft 进程前执行的用户命令；为空时跳过。
        "pre_launch_command": "",
        # 包裹 java 命令的包装命令：含 {} 占位符时替换为完整命令，否则作为前缀拼接。
        "wrapper_command": "",
        # 游戏进程退出后在实例目录执行的用户命令；为空时跳过。
        "post_exit_command": "",
        # 传给游戏进程的自定义环境变量，多行 KEY=VALUE；优先级低于插件提供的变量。
        "env_vars": "",
        # 游戏主窗口标题模板，支持 {instance}/{version}/{account} 占位符；为空时不修改标题。
        "window_title_template": "",
        # 游戏启动成功后的启动器行为: none / minimize / quit。
        "launcher_visibility": "none",
        # Windows 下将实际使用的 Java 可执行文件登记为高性能 GPU 偏好。
        "prefer_high_performance_gpu": False,
        # Windows 下优先以 java.exe 取代选中的 javaw.exe，便于收集标准输出。
        "use_java_exe": False,
        # 禁止异常退出后自动创建崩溃分析报告，不影响用户手动分析日志。
        "disable_crash_analysis": False,
        "last_install_path": "",
        "last_manage_path": "",
    },
    "download": {"mirror_source": "official"},
    "ui": {
        "locale": "zh-CN",
        "theme": {
            "theme_id": "classic",
            "mode": "system",
            "primary_color": "#5B6FF5",
            "blur_amount": 18,
            "sidebar_collapsed": True,
            "navigation_mode": "sidebar",
            "titlebar_hidden": False,
            "window_chrome": "custom",
            "transparent_bg": False,
            "background_opacity": 1.0,
            "appearance": {
                "radius_window": 12,
                "radius_card": 8,
                "radius_control": 6,
                "radius_dialog": 10,
                "card_opacity": 100,
                "font_family": "",
                "sidebar_font_family": "",
                "terminal_font_family": "",
                "log_font_family": "",
            },
            "schedule": {"enabled": False, "dark_start": "20:00", "dark_end": "07:00"},
        },
        "background": {"type": "default", "path": "", "opacity": 1.0, "blur": 18},
    },
}


class ConfigStore:
    """
    以 JSON 文件为载体的启动器配置存储，归应用上下文所有。

    负责配置的读写、默认值填充与校验，并以事件总线向其他组件广播配置变更。
    """

    # 允许持久化的配置分区白名单。未知分区（例如决定前端来源的 tauri）禁止经
    # IPC 写入，防止前端或注入脚本篡改启动器级敏感配置。
    allowed_sections = frozenset(default_config) | {"user_agreement", "background"}

    def __init__(self, data_path: Path, event_bus: EventBus | None = None) -> None:
        self.logger = get_logger("config")  # 配置读写日志器。
        self.data_path = Path(data_path)  # 应用数据目录。
        self.config_path = self.data_path / "setting.json"  # 配置文件路径。
        self.config_data: dict[str, Any] | None = None  # 内存中的配置快照。
        self.events = event_bus or EventBus()  # 配置变更事件总线。

    @property
    def default_minecraft_path(self) -> Path:
        """
        获取默认 Minecraft 目录。
        :return: 默认 Minecraft 目录
        """
        return (self.data_path.parent / ".minecraft").resolve()

    def _create_default_config(self) -> dict[str, Any]:
        # 深拷贝默认值，避免调用方共享可变配置。
        config_data = deepcopy(default_config)
        self._ensure_default_minecraft_path(config_data)
        return config_data

    def _ensure_default_minecraft_path(self, config_data: dict[str, Any]) -> bool:
        # 缺少游戏目录时创建默认目录并回填相关字段。
        game_config = config_data.get("game")
        if not isinstance(game_config, dict):
            return False
        minecraft_paths = game_config.get("minecraft_paths")
        if isinstance(minecraft_paths, list) and minecraft_paths:
            return False
        minecraft_path = self.default_minecraft_path
        minecraft_path.mkdir(parents=True, exist_ok=True)
        game_config["minecraft_paths"] = [{"name": "默认路径", "path": str(minecraft_path)}]
        if not game_config.get("last_install_path"):
            game_config["last_install_path"] = str(minecraft_path)
        if not game_config.get("last_manage_path"):
            game_config["last_manage_path"] = str(minecraft_path)
        return True

    def _initialize_file(self) -> None:
        # 创建数据目录，并在首次启动时写入默认配置。
        self.data_path.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            return
        self.logger.info("配置文件不存在，正在创建: %s", self.config_path)
        self._write_config(self._create_default_config())

    def _write_config(self, config_data: dict[str, Any]) -> None:
        # 以原子替换方式写入配置，避免写入中断造成文件损坏。
        try:
            serialized_config = json.dumps(config_data, ensure_ascii=False, indent=4)
            atomic_write_text(self.config_path, serialized_config)
        except (OSError, TypeError, ValueError) as exc:
            raise ConfigError(f"写入配置文件失败: {self.config_path}") from exc
        self.config_data = deepcopy(config_data)

    def _load_config(self) -> dict[str, Any]:
        # 从磁盘加载配置，必要时补全默认路径或恢复损坏文件。
        try:
            self._initialize_file()
            loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("配置文件根节点必须是对象")
            needs_write = self._ensure_default_minecraft_path(loaded)
            if needs_write:
                self._write_config(loaded)
            return loaded
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return self._restore_default_config(exc)

    def _restore_default_config(self, cause: Exception) -> dict[str, Any]:
        # 备份损坏配置后恢复默认值。
        backup_path = self.config_path.with_suffix(".json.bak")
        self.logger.warning("配置文件无效，将恢复默认配置: %s", cause)
        with suppress(OSError):
            self.config_path.replace(backup_path)
        restored = self._create_default_config()
        self._write_config(restored)
        self.logger.info("已恢复默认配置，原配置备份路径: %s", backup_path)
        return restored

    def get_config(self, section: str | None = None) -> Any:
        """
        获取配置数据
        :param section: 配置分区，为 None 时返回全部配置
        :return: 配置数据
        """
        if self.config_data is None:
            self.config_data = self._load_config()
        if section is None:
            return deepcopy(self.config_data)
        return deepcopy(self.config_data.get(section))

    def list_sections(self) -> list[str]:
        """
        获取全部配置分区名称
        :return: 配置分区名称列表
        """
        return list(self.get_config())

    def get_many(self, sections: list[str]) -> dict[str, Any]:
        """
        批量获取配置分区
        :param sections: 配置分区名称列表
        :return: 配置分区及其数据
        """
        config_data = self.get_config()
        return {section: config_data.get(section) for section in dict.fromkeys(sections)}

    def save_config(self, section: str, data: Any) -> None:
        """
        保存配置分区
        :param section: 配置分区名称
        :param data: 配置数据
        """
        if not isinstance(section, str) or not section.strip():
            raise ConfigValidationError("配置分区名称不能为空")
        normalized_section = section.strip()
        if normalized_section not in self.allowed_sections:
            raise ConfigValidationError(f"配置分区不受支持: {normalized_section}")
        config_data = self.get_config()
        config_data[normalized_section] = deepcopy(data)
        self._write_config(config_data)
        # 广播配置变更事件，通知订阅组件。
        self.events.emit("config:updated", normalized_section, deepcopy(data))
