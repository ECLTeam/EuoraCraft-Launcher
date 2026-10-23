# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：下载配置的局部写入模型与本次运行实际使用的下载源解析，不承担文件访问授权。
#
# 公开接口：
#   - class ResourceInstallBinding — 资源类型记忆的实例目标，空字段表示明确不绑定。
#   - class DownloadSettingsPatch — 下载源和按资源类型合并的配置补丁。
#   - resolve_mirror_source() — 按界面语言解析游戏下载源，不回写配置。
#   - resolve_mod_source() — 按界面语言解析模组源，不回写配置。
# ============================================================

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 唯一展示下载源切换项的界面语言，其余语言固定使用官方源。
_download_source_switch_locale = "zh-CN"


class ResourceInstallBinding(BaseModel):
    """
    保存一个资源类型明确选择的安装目标。

    两个空字符串表示用户选择不绑定实例；这里的路径只用于选择恢复，实际
    文件操作仍必须经过实例目标校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    game_path: str = Field(alias="gamePath", max_length=8192)
    instance_directory_name: str = Field(alias="versionId", max_length=255)

    @field_validator("game_path", "instance_directory_name")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """
        拒绝不能安全进入后续路径或标识解析的控制字符。

        :param value: 从配置文件读出的文本
        :return: 保留大小写和目录语义的原始文本
        :raises ValueError: 文本包含 NUL 时抛出
        """
        if "\0" in value:
            raise ValueError("安装目标包含非法字符")
        return value


class DownloadSettingsPatch(BaseModel):
    """
    描述一次下载配置局部更新，未传字段保持已有值。

    映射按资源类型键合并；显式空绑定不会删除其他类型的目标。拒绝显式 null，
    避免将删除整个映射误认为局部更新。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    mirror_source: Literal["official", "bmclapi"] | None = None
    mod_source: Literal["official", "mcim"] | None = None
    resource_install_cache: dict[str, ResourceInstallBinding] | None = Field(default=None, alias="resourceInstallCache")
    resource_save_directories: dict[str, str] | None = Field(default=None, alias="resourceSaveDirectories")

    @field_validator(
        "mirror_source", "mod_source", "resource_install_cache", "resource_save_directories", mode="before"
    )
    @classmethod
    def reject_null(cls, value: object) -> object:
        """
        区分未传字段与显式清空整个配置分区。

        :param value: 显式传入的字段值
        :return: 非空字段的原始值
        :raises ValueError: 显式传入 null 时抛出
        """
        if value is None:
            raise ValueError("下载配置补丁不能使用 null")
        return value

    @field_validator("resource_install_cache", "resource_save_directories")
    @classmethod
    def validate_keys(
        cls, value: dict[str, ResourceInstallBinding] | dict[str, str]
    ) -> dict[str, ResourceInstallBinding] | dict[str, str]:
        """
        限制补丁大小并拒绝空或非法资源类型键。

        :param value: 一次合并的资源类型映射
        :return: 已校验映射
        :raises ValueError: 映射过大或键格式不合法时抛出
        """
        if len(value) > 128 or any(not key.strip() or len(key) > 256 or "\0" in key for key in value):
            raise ValueError("资源类型映射格式无效")
        if any(isinstance(entry, str) and (len(entry) > 8192 or "\0" in entry) for entry in value.values()):
            raise ValueError("保存目录格式无效")
        return value


def _uses_download_source_switch(configuration: Mapping[str, object]) -> bool:
    """
    判断当前界面语言是否提供下载源切换。

    缺少界面语言配置时按默认的简体中文处理，避免旧配置丢失切换能力。

    :param configuration: 启动器有效配置
    :return: 是否按用户保存的下载源与模组源运行
    """
    ui_config = configuration.get("ui")
    locale = ui_config.get("locale") if isinstance(ui_config, Mapping) else None
    return locale is None or locale == _download_source_switch_locale


def _configured_download_source(configuration: Mapping[str, object], key: str) -> object:
    """
    读取下载配置中的保存值，不校验取值。

    :param configuration: 启动器有效配置
    :param key: ``mirror_source`` 或 ``mod_source``
    :return: 保存的原始值；配置缺失时为 None
    """
    download_config = configuration.get("download")
    return download_config.get(key) if isinstance(download_config, Mapping) else None


def resolve_mirror_source(configuration: Mapping[str, object]) -> str:
    """
    解析本次运行实际使用的游戏下载源，不改动配置文件中的保存值。

    非简体中文界面不提供下载源切换，固定返回官方源；简体中文界面沿用用户
    保存的选择，以便切回简体中文后恢复原设置。配置缺失或取值非法时回退
    官方源，保证调用方始终拿到可用的源名称。

    :param configuration: 启动器有效配置
    :return: 实际使用的下载源名称，``official`` 或 ``bmclapi``
    """
    if not _uses_download_source_switch(configuration):
        return "official"
    return "bmclapi" if _configured_download_source(configuration, "mirror_source") == "bmclapi" else "official"


def resolve_mod_source(configuration: Mapping[str, object]) -> str:
    """
    解析本次运行实际使用的模组源，不改动配置文件中的保存值。

    规则与游戏下载源一致：非简体中文界面固定使用官方源，简体中文界面沿用
    用户保存的选择；配置缺失或取值非法时回退官方源。

    :param configuration: 启动器有效配置
    :return: 实际使用的模组源名称，``official`` 或 ``mcim``
    """
    if not _uses_download_source_switch(configuration):
        return "official"
    return "mcim" if _configured_download_source(configuration, "mod_source") == "mcim" else "official"
