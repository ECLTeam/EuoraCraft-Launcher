# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：区分原始联机配置与连接时可用节点，隔离房间协调服务地址。
#
# 公开接口：
#   - class ConnectorNodeConfig — 保存尚未做地址业务校验的节点配置。
#   - class ConnectorNodeSettings — 连接时校验并规范化节点。
#   - class ConnectorNodeError — 节点配置无法用于连接时的领域错误。
# ============================================================

from __future__ import annotations

from typing import ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ECL.utils.errors import ConnectorError


class ConnectorNodeConfig(BaseModel):
    """
    保存原始节点配置，允许空地址和尚未完成的输入。

    读取设置时只检查字段结构和类型；连接前再检查节点地址。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["automatic", "append", "custom"] = "automatic"
    nodes: list[str] = Field(default_factory=list)


class ConnectorNodeError(ConnectorError):
    """
    节点配置无法用于连接，提供不包含原始输入的简短提示。
    """

    error_code: str = "CONNECTOR_NODES_INVALID"


class ConnectorNodeSettings(ConnectorNodeConfig):
    """
    校验联机节点设置，仅影响下一次连接而不迁移活动房间。

    自定义模式要求至少一个有效节点，不允许退回公共节点。
    """

    nodes: list[str] = Field(default_factory=list, max_length=32)
    schemes: ClassVar[frozenset[str]] = frozenset({"tcp", "udp", "quic", "faketcp", "ws", "wss"})

    @classmethod
    def for_connection(cls, settings: ConnectorNodeConfig) -> ConnectorNodeSettings:
        """
        在建立连接前清理并校验自定义节点，不改写保存的配置。

        :param settings: 已通过结构校验的原始配置
        :return: 规范化的连接节点
        :raises ConnectorNodeError: 自定义地址无效或仅自定义地址为空
        """
        nodes = [node.strip() for node in settings.nodes if node.strip()]
        if settings.mode == "custom" and not nodes:
            raise ConnectorNodeError("仅自定义模式尚未填写节点，请在联机设置中添加地址")
        try:
            return cls(mode=settings.mode, nodes=nodes)
        except ValidationError as exc:
            raise ConnectorNodeError("自定义节点地址无效，请检查协议、地址和端口") from exc

    @field_validator("nodes")
    @classmethod
    def validate_nodes(cls, nodes: list[str]) -> list[str]:
        """
        拒绝凭据、查询、片段及非法端口，并保序去重有效 URI。

        :param nodes: 不可信的用户节点列表
        :return: 规范化后的节点列表
        :raises ValueError: 节点格式不被 EasyTier 支持
        """
        normalized: list[str] = []
        for node in nodes:
            value = node.strip()
            if not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 for char in value):
                raise ValueError("节点地址不能为空或包含空白字符")
            parsed = urlsplit(value)
            if (
                parsed.scheme not in cls.schemes
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
            ):
                raise ValueError("请填写带端口的 tcp/udp/quic/faketcp/ws/wss 节点地址")
            if parsed.scheme not in {"ws", "wss"} and parsed.path not in {"", "/"}:
                raise ValueError("该节点协议不支持路径")
            host = parsed.hostname.lower()
            host = f"[{host}]" if ":" in host else host
            path = parsed.path if parsed.scheme in {"ws", "wss"} else ""
            canonical = urlunsplit((parsed.scheme, f"{host}:{parsed.port}", path, "", ""))
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @model_validator(mode="after")
    def require_custom_nodes(self) -> ConnectorNodeSettings:
        """
        保证仅自定义策略不会隐式连接公共节点。

        :return: 经过策略校验的配置
        :raises ValueError: 仅自定义模式没有节点
        """
        if self.mode == "custom" and not self.nodes:
            raise ValueError("仅自定义模式至少需要一个节点")
        return self
