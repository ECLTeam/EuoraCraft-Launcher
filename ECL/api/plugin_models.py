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
# ============================================================

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt

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
