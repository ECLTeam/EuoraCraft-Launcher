# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例启动设置契约和有效值解析，不写入配置或启动进程。
#
# 公开接口：
#   - class InstanceLaunchOverrides — 校验并迁移实例独立设置。
#   - class EffectiveLaunchSettings — 各启动入口共用的有效启动选项。
#   - class LaunchSettingsResolver — 按全局、实例和单次覆盖顺序解析设置。
#   - scan_instance_java_references(java_home_path, minecraft_paths) -> tuple[str, ...]
#     — 扫描实例独立设置，返回以手动 Java 指向该运行时的实例标签。
# ============================================================

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ECL.utils.config import default_config


class InstanceLaunchOverrides(BaseModel):
    """
    保留额外文件字段并校验实例设置，兼容旧版独立开关和空值继承。
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")
    schema_version: int = Field(default=2, alias="schemaVersion", ge=1, le=2)
    isolation_mode: Literal["inherit", "enabled", "disabled"] = Field(default="inherit", alias="isolationMode")
    custom_memory: bool = Field(default=False, alias="customMemory")
    memory: int = Field(default=4096, ge=512, le=65536)
    memory_mode: Literal["inherit", "auto", "manual"] = Field(default="inherit", alias="memoryMode")
    custom_java: bool = Field(default=False, alias="customJava")
    java_path: str = Field(default="", alias="javaPath")
    java_mode: Literal["inherit", "auto", "manual"] = Field(default="inherit", alias="javaMode")
    jvm_args: str = Field(default="", alias="jvmArgs")
    game_args: str = Field(default="", alias="gameArgs")
    pre_launch_command: str | None = Field(default=None, alias="preLaunchCommand")
    wrapper_command: str = Field(default="", alias="wrapperCommand")
    post_exit_command: str = Field(default="", alias="postExitCommand")
    env_vars: str = Field(default="", alias="envVars")
    window_title: str = Field(default="", alias="windowTitle")
    command_overrides: list[Literal["wrapperCommand", "postExitCommand", "envVars", "windowTitle"]] = Field(
        default_factory=list, alias="commandOverrides"
    )
    launcher_visibility: Literal["inherit", "none", "minimize", "quit"] = Field(
        default="inherit", alias="launcherVisibility"
    )
    lock_memory: bool | None = Field(default=None, alias="lockMemory")
    process_priority: Literal["idle", "below_normal", "normal", "above_normal", "high"] | None = Field(
        default=None, alias="processPriority"
    )
    width: int | None = Field(default=None, ge=320, le=16384)
    height: int | None = Field(default=None, ge=240, le=16384)
    fullscreen: bool | None = None
    renderer: Literal["default", "software", "directx12", "vulkan"] | None = None
    prefer_high_performance_gpu: bool | None = Field(default=None, alias="preferHighPerformanceGpu")
    use_java_exe: bool | None = Field(default=None, alias="useJavaExe")
    disable_crash_analysis: bool | None = Field(default=None, alias="disableCrashAnalysis")

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy(cls, value: object) -> object:
        """
        从旧独立开关补齐模式，且不修改调用方字典。

        :param value: 从配置文件读出的原始值
        :return: 可供字段校验的迁移副本
        """
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "isolationMode" not in migrated and isinstance(migrated.get("isolated"), bool):
            migrated["isolationMode"] = "enabled" if migrated["isolated"] else "disabled"
        migrated.setdefault("javaMode", "manual" if migrated.get("customJava") else "inherit")
        migrated.setdefault("memoryMode", "manual" if migrated.get("customMemory") else "inherit")
        if "commandOverrides" not in migrated:
            migrated["commandOverrides"] = [
                name for name in ("wrapperCommand", "postExitCommand", "envVars", "windowTitle") if migrated.get(name)
            ]
        return migrated


def scan_instance_java_references(java_home_path: Path, minecraft_paths: tuple[Path, ...]) -> tuple[str, ...]:
    """
    扫描实例独立设置，返回以手动 Java 指向该运行时的实例标签。

    仅当实例设置明确选择手动 Java 且解析后的路径归属该运行时根目录时计入引用。
    实例根目录不可访问或设置文件损坏时抛出 ValueError，由调用方保留未知状态，
    避免在无法确认占用关系时误判 Java 未被引用。

    :param java_home_path: 目标 Java 运行时的根目录
    :param minecraft_paths: 当前配置的实例根目录集合
    :return: 引用该运行时的实例标签
    :raises ValueError: 实例根目录不可访问或设置文件无法解析
    """
    references: list[str] = []
    for root in minecraft_paths:
        versions = root / "versions"
        if not root.is_dir():
            raise ValueError("实例根目录暂不可访问，无法确认 Java 引用")
        if not versions.is_dir():
            continue
        for directory in versions.iterdir():
            settings_file = directory / ".ecl" / "settings.json"
            if not directory.is_dir() or not settings_file.is_file():
                continue
            try:
                if settings_file.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("settings limit")
                settings = InstanceLaunchOverrides.model_validate_json(settings_file.read_bytes())
            except (OSError, ValueError) as exc:
                raise ValueError("实例设置暂无法读取，无法确认 Java 引用") from exc
            if (
                settings.java_mode == "manual"
                and settings.java_path
                and Path(settings.java_path).expanduser().resolve(strict=False).parent.parent == java_home_path
            ):
                references.append(f"实例：{directory.name}")
    return tuple(references)


class EffectiveLaunchSettings(BaseModel):
    """
    保存经过合并和范围校验的游戏启动选项，字段对应启动协调器。
    """

    java_path: str | None = None
    memory: int = Field(default=4096, ge=512, le=65536)
    lock_memory: bool = False
    process_priority: Literal["idle", "below_normal", "normal", "above_normal", "high"] = "normal"
    width: int = Field(default=854, ge=320, le=16384)
    height: int = Field(default=480, ge=240, le=16384)
    fullscreen: bool = False
    version_isolation: bool | None = None
    jvm_args: list[str] = Field(default_factory=list)
    game_args: list[str] = Field(default_factory=list)
    pre_launch_command: str = ""
    wrapper_command: str = ""
    post_exit_command: str = ""
    env_vars: str = ""
    window_title: str = ""
    launcher_visibility: Literal["none", "minimize", "quit"] = "none"
    renderer: str = "default"
    prefer_high_performance_gpu: bool = False
    use_java_exe: bool = False
    disable_crash_analysis: bool = False


class LaunchSettingsResolver:
    """
    为全部启动入口解析默认、全局、实例及单次启动设置。
    """

    global_field_names: dict[str, str] = {
        "width": "game_width",
        "height": "game_height",
        "window_title": "window_title_template",
    }
    command_field_names: dict[str, str] = {
        "wrapperCommand": "wrapper_command",
        "postExitCommand": "post_exit_command",
        "envVars": "env_vars",
        "windowTitle": "window_title",
    }
    optional_instance_fields: tuple[str, ...] = (
        "lock_memory",
        "process_priority",
        "width",
        "height",
        "fullscreen",
        "renderer",
        "pre_launch_command",
        "prefer_high_performance_gpu",
        "use_java_exe",
        "disable_crash_analysis",
    )

    @staticmethod
    def parse_arguments(value: object) -> list[str]:
        """
        解析配置参数文本或字符串列表，仅拆分而不执行 shell。

        :param value: 参数文本或数组
        :return: 独立参数数组
        :raises ValueError: 引号不配对或数组元素类型错误
        """
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return LaunchSettingsResolver._tokenize_arguments(value)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        raise ValueError("启动参数必须是文本或字符串数组")

    @staticmethod
    def _tokenize_arguments(value: str) -> list[str]:  # noqa: C901 - 显式状态机与前端参数编辑规则一致
        """
        保留 Windows 路径反斜杠，解析引号及显式空参数，不执行 shell。
        """
        arguments: list[str] = []
        current: list[str] = []
        quote: str | None = None
        escaped = False
        started = False
        for index, character in enumerate(value):
            if escaped:
                current.append(character)
                escaped = False
                continue
            if character == "\\":
                following = value[index + 1] if index + 1 < len(value) else ""
                if following and (following in "\\\"'" or following.isspace()):
                    escaped = True
                else:
                    current.append(character)
                started = True
                continue
            if quote is not None:
                if character == quote:
                    quote = None
                else:
                    current.append(character)
                continue
            if character in "\"'":
                quote = character
                started = True
            elif character.isspace():
                if started:
                    arguments.append("".join(current))
                    current = []
                    started = False
            else:
                current.append(character)
                started = True
        if quote is not None:
            raise ValueError("启动参数的引号未闭合")
        if escaped:
            current.append("\\")
        if started:
            arguments.append("".join(current))
        return arguments

    @classmethod
    def resolve(
        cls,
        global_settings: Mapping[str, object],
        instance: InstanceLaunchOverrides,
        requested: Mapping[str, object] | None = None,
    ) -> EffectiveLaunchSettings:
        """
        返回本次启动有效设置，实例和命令行覆盖均不回写配置。

        :param global_settings: 当前全局游戏配置
        :param instance: 已校验并迁移的实例独立设置
        :param requested: 只含调用方显式传入字段的单次覆盖
        :return: 可提交给游戏启动协调器的选项
        :raises ValueError: 有效设置值或参数格式不合法
        """
        config = {**default_config["game"], **global_settings}
        values: dict[str, object] = {}
        for field_name in EffectiveLaunchSettings.model_fields:
            config_key = cls.global_field_names.get(field_name, field_name)
            if config_key in config:
                values[field_name] = config[config_key]
        values["java_path"] = None if config.get("java_auto", True) else str(config.get("java_path") or "") or None
        cls._apply_runtime_modes(config, instance, values)
        cls._apply_instance_fields(instance, values)
        values["jvm_args"] = [*cls.parse_arguments(config.get("jvm_args")), *cls.parse_arguments(instance.jvm_args)]
        values["game_args"] = [
            *cls.parse_arguments(config.get("game_args_tail")),
            *cls.parse_arguments(instance.game_args),
        ]
        cls._apply_requested(requested or {}, values)
        return EffectiveLaunchSettings.model_validate(values)

    @staticmethod
    def _apply_runtime_modes(
        config: Mapping[str, object], instance: InstanceLaunchOverrides, values: dict[str, object]
    ) -> None:
        java_mode = instance.java_mode
        if java_mode == "auto":
            values["java_path"] = None
        elif java_mode == "manual":
            if not instance.java_path.strip():
                raise ValueError("手动 Java 模式需要指定可执行文件")
            values["java_path"] = instance.java_path
        memory_auto = (
            config.get("memory_auto", True) if instance.memory_mode == "inherit" else instance.memory_mode == "auto"
        )
        if memory_auto:
            values["memory"] = max(
                2048, min(8192, round(psutil.virtual_memory().total / (1024 * 1024 * 4 * 256)) * 256)
            )
        else:
            values["memory"] = instance.memory if instance.memory_mode == "manual" else config.get("memory_size", 4096)

    @classmethod
    def _apply_instance_fields(cls, instance: InstanceLaunchOverrides, values: dict[str, object]) -> None:
        for field_name in cls.optional_instance_fields:
            override = getattr(instance, field_name)
            if override is not None:
                values[field_name] = override
        for alias, field_name in cls.command_field_names.items():
            override = getattr(instance, field_name)
            if alias in instance.command_overrides:
                values[field_name] = override
        if instance.launcher_visibility != "inherit":
            values["launcher_visibility"] = instance.launcher_visibility
        values["version_isolation"] = (
            True if instance.isolation_mode == "enabled" else False if instance.isolation_mode == "disabled" else None
        )

    @classmethod
    def _apply_requested(cls, requested: Mapping[str, object], values: dict[str, object]) -> None:
        for field_name, value in requested.items():
            if field_name not in EffectiveLaunchSettings.model_fields:
                continue
            if field_name in {"jvm_args", "game_args"}:
                values[field_name] = [*cls.parse_arguments(values[field_name]), *cls.parse_arguments(value)]
            elif value is not None or field_name == "java_path":
                values[field_name] = str(value) if field_name == "java_path" and value is not None else value
