# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验可信主界面发起的插件授权与运行查询请求，不从 body 决定调用身份。
#
# 公开接口：
#   - class PluginPermissionQuery — 安装权限及运行查询。
#   - class PluginPermissionUpdate — 带内容与并发版本的授权更新。
#   - class PluginPermissionResources — 可信主窗口的有界资源候选查询。
#   - class PluginFileRecoveriesQuery / PluginFileRecoveryResolve — 有界恢复查询与审阅动作。
#   - class PluginInstanceProfileRecoveryResolve — 共享实例资料的受限另存恢复选择。
# ============================================================

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from ECL.plugins.grant_models import PermissionDecision
from ECL.plugins.runtime.models import FileRef


class PluginPermissionQuery(BaseModel):
    """
    通过宿主安装标识定位对象，不接受名称或客户端自报主体替代。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class PluginPermissionUpdate(PluginPermissionQuery):
    """
    将审阅内容与 revision 一同提交，拒绝并发覆盖或未经审阅的代码变化。
    """

    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: StrictInt = Field(ge=0)
    decisions: list[PermissionDecision] = Field(max_length=128)
    resource_revision: StrictInt | None = Field(default=None, ge=0)
    java_resource_revision: StrictInt | None = Field(default=None, ge=0)
    account_resource_revision: StrictInt | None = Field(default=None, ge=0)


class PluginPermissionResources(PluginPermissionQuery):
    """
    仅可信主窗口可发现审阅候选；不接受路径或客户端自报的资源属性。
    """

    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: Literal["instances.read", "instances.manage", "java.read", "java.use", "accounts.read"]
    parent_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    search: str = Field(default="", max_length=120)
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class PluginFileRecoveriesQuery(BaseModel):
    """
    查询所有安装含孤立安装的待恢复记录，不接受宿主路径。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    limit: StrictInt = Field(default=50, ge=1, le=100)


class PluginFileRecoveryResolve(BaseModel):
    """
    绑定主窗口审阅的实际对象版本及明确恢复选择。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    recovery_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_revision: StrictInt = Field(ge=0, le=2**53 - 1)
    expected_etag: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["restore", "save_as", "discard"]
    saved_name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    @model_validator(mode="after")
    def validate_choice(self) -> PluginFileRecoveryResolve:
        """
        只有另存需要名称，拒绝额外名称改变其他动作的含义。

        :return: 通过选择与名称一致性校验的请求
        """
        if self.action == "save_as":
            if self.saved_name is None:
                raise ValueError("另存需要明确名称")
            FileRef.valid_name(self.saved_name)
        elif "saved_name" in self.model_fields_set:
            raise ValueError("此恢复动作不接受另存名称")
        return self


class PluginInstanceProfileRecoveryResolve(BaseModel):
    """
    共享资料只允许原位置恢复、受限后缀另存或明确丢弃，不能指定任意文件路径。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    recovery_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_revision: StrictInt = Field(ge=0, le=2**53 - 1)
    expected_etag: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["restore", "save_as", "discard"]
    saved_suffix: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def validate_choice(self) -> PluginInstanceProfileRecoveryResolve:
        """
        另存必须给出后缀，其他动作不能附带被忽略的参数。

        :return: 动作和后缀一致的明确恢复请求
        """
        if self.action == "save_as":
            if self.saved_suffix is None:
                raise ValueError("另存需要明确后缀")
        elif "saved_suffix" in self.model_fields_set:
            raise ValueError("此恢复动作不接受另存后缀")
        return self
