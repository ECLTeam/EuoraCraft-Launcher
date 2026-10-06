# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：下载配置的局部写入模型，仅校验可更新字段，不承担文件访问授权。
#
# 公开接口：
#   - class ResourceInstallBinding — 资源类型记忆的实例目标，空字段表示明确不绑定。
#   - class DownloadSettingsPatch — 下载源和按资源类型合并的配置补丁。
# ============================================================

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

        :param value: 配置边界传入的文本
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
