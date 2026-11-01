# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义受控文件对象的共同错误、物理身份及版本元数据。
#
# 公开接口：
#   - FileBoundaryError / FileIdentity / FileInformation — 跨平台文件边界模型。
# ============================================================

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from ECL.plugin_api.contracts import ProtocolError


class FileBoundaryError(ProtocolError):
    """
    只暴露稳定业务错误，不包含宿主路径、原始内容或原生调用参数。
    """


class FileIdentity(BaseModel):
    """
    绑定目录或文件对象，不以可修改的名称代替对象身份。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    volume: int
    index: int
    created: int


@dataclass(frozen=True, slots=True)
class FileInformation:
    """
    描述句柄确认的普通对象，纳秒时间不直接放入 JSON DTO。
    """

    identity: FileIdentity
    size: int
    modified_ns: int
    attributes: int
    links: int

    def etag(self, digest: str) -> str:
        """
        将物理身份与完整内容摘要组合为不透明版本。

        :param digest: 文件完整字节的 SHA-256
        :return: 可安全传输的版本字符串
        """
        value = [self.identity.model_dump(), self.size, self.modified_ns, digest]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
