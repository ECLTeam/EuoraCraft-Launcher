# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：环境变量应用：将进程环境映射到配置键。
#
# 公开接口：
#   - class Environment — 面向组合根的环境变量读取器，路径一经创建即固定不变。
#       - get_env() -> dict[str, str | None] — 获取环境变量。
#       - get_value(default, *keys) -> str | None — 读取环境变量。
#       - apply_to_config(config) -> dict[str, Any] — 使用环境变量覆盖配置。
# ============================================================

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from ECL.utils.logging import get_logger


class Environment:
    """
    面向组合根的环境变量读取器，路径一经创建即固定不变。
    """

    def __init__(self, app_path: Path) -> None:
        # 记录应用路径并准备 .env 文件与环境变量缓存。
        self.logger = get_logger("Environment")  # 环境读取日志器。
        self.app_path = Path(app_path)  # 应用根目录。
        self.env_path = self.app_path / ".env"  # 可选的环境变量文件。
        self.env_data: dict[str, str | None] | None = None  # 合并后的环境变量缓存。

    def get_env(self) -> dict[str, str | None]:
        """
        获取环境变量。
        """
        if self.env_data is not None:
            return dict(self.env_data)
        self.env_data = dict(dotenv_values(self.env_path)) if self.env_path.exists() else {}
        self.env_data.update({key: value for key, value in os.environ.items() if key.startswith("ECL_")})
        for key in ("LITTLESKIN_CLIENT_ID", "LITTLESKIN_CLIENT_SECRET", "LITTLESKIN_REDIRECT_URI"):
            if (value := os.environ.get(key)) and value.strip():
                self.env_data[key] = value
        system_client_id = os.environ.get("MICROSOFT_CLIENT_ID") or os.environ.get("ECL_MICROSOFT_CLIENT_ID")
        if system_client_id:
            self.env_data["MICROSOFT_CLIENT_ID"] = system_client_id
        elif not self.env_data.get("MICROSOFT_CLIENT_ID") and self.env_data.get("ECL_MICROSOFT_CLIENT_ID"):
            self.env_data["MICROSOFT_CLIENT_ID"] = self.env_data["ECL_MICROSOFT_CLIENT_ID"]
        system_cf_key = os.environ.get("CURSEFORGE_API_KEY") or os.environ.get("ECL_CURSEFORGE_API_KEY")
        if system_cf_key:
            self.env_data["CURSEFORGE_API_KEY"] = system_cf_key
        elif not self.env_data.get("CURSEFORGE_API_KEY") and self.env_data.get("ECL_CURSEFORGE_API_KEY"):
            self.env_data["CURSEFORGE_API_KEY"] = self.env_data["ECL_CURSEFORGE_API_KEY"]
        return dict(self.env_data)

    def get_value(self, *keys: str, default: str | None = None) -> str | None:
        """
        读取环境变量。

        :param default: 字段缺失时采用的默认值
        :param keys: 需要批量读取的配置键
        """
        env_data = self.get_env()
        for key in keys:
            value = env_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return default

    def _convert_env_value(self, value: str) -> bool | int | float | str:
        # 将环境变量字符串转换为最合适的标量类型。
        lower = value.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        if value.lstrip("-").isdigit():
            return int(value)
        try:
            return float(value)
        except ValueError:
            return value

    def apply_to_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """
        使用环境变量覆盖配置。

        键路径按已存在的配置层级贪心匹配，使含下划线的真实配置键可以被
        ECL_CONFIG_LAUNCHER_DEBUG_LOG_LEVEL 这类环境变量正确寻址到
        launcher.debug_log_level，而不是被拆成无意义的嵌套层级。

        :param config: 游戏或启动器配置数据
        """
        result = deepcopy(config)
        env_data = self.get_env()
        prefix = "ECL_CONFIG_"
        for env_key, env_value in env_data.items():
            if not env_key.startswith(prefix) or env_value is None:
                continue
            key_segments = [seg.lower() for seg in env_key[len(prefix) :].split("_")]
            current = result
            index = 0
            while True:
                matched: tuple[str, int] | None = None
                for end in range(len(key_segments), index, -1):
                    candidate = "_".join(key_segments[index:end])
                    if candidate in current and isinstance(current[candidate], dict):
                        matched = (candidate, end)
                        break
                if matched is None:
                    if index == 0:
                        # 顶层未命中已知分区时，将首个段作为新分区（如默认配置中不含的 tauri），
                        # 使 ECL_CONFIG_TAURI_FRONTENDDIST 仍能寻址到 tauri.frontenddist。
                        section = key_segments[0]
                        if not isinstance(current.get(section), dict):
                            current[section] = {}
                        current = current[section]
                        index = 1
                        continue
                    # 剩余段无法命中已存在的层级时，合并为单个键写入当前层
                    current["_".join(key_segments[index:])] = self._convert_env_value(env_value)
                    break
                candidate, end = matched
                if end == len(key_segments):
                    current[candidate] = self._convert_env_value(env_value)
                    break
                current = current[candidate]
                index = end
        return result
