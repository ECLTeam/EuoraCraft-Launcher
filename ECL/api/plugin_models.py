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
#   - class PluginSettingsQuery / PluginSettingsUpdate — 内容与定义版本绑定的配置管理请求。
# ============================================================

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from pydantic import JsonValue as PydanticJsonValue

from ECL.plugins.grant_models import PermissionDecision


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


class PluginSettingsQuery(BaseModel):
    """
    校验可信管理页选择的插件名称，不授予该插件跨安装访问能力。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    plugin_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PluginSettingsUpdate(PluginSettingsQuery):
    """
    绑定用户看到的内容、定义和配置版本，拒绝迟到页面覆盖新定义。
    """

    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: StrictInt = Field(ge=0)
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    value: PydanticJsonValue
