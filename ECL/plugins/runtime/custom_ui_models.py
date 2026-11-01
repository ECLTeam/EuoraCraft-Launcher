# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验包内独立前端声明及专用窗口消息，不接受宿主 URL 和任意 IPC。
#
# 公开接口：
#   - class CustomUiDeclaration — 单文件离线前端及宿主窗口尺寸。
#   - class CustomUiRequest — 专用桥的严格消息模型。
# ============================================================

from __future__ import annotations

from pydantic import Field
from pydantic import JsonValue as PydanticJsonValue

from ECL.plugins.runtime.models import RequestModel


class CustomUiDeclaration(RequestModel):
    """
    前端必须是代码快照内的单个 HTML 文件，作者在打包前内联自己的资源。
    """

    entry_file: str = Field(max_length=200, pattern=r"^(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.html$")
    title: str = Field(min_length=1, max_length=100)
    width: int = Field(default=800, strict=True, ge=480, le=1920)
    height: int = Field(default=600, strict=True, ge=360, le=1080)


class CustomUiRequest(RequestModel):
    """
    消息身份与内容均有界，实际所有者来自所属窗口的 Broker。
    """

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    method: str = Field(min_length=1, max_length=64)
    params: dict[str, PydanticJsonValue] = Field(default_factory=dict, max_length=16)
    mutation_key: str = Field(pattern=r"^[0-9a-f]{32}$")
