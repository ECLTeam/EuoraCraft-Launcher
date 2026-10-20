# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义最小 Worker 宿主接口的严格输入和基础字段约束。
#
# 公开接口：
#   - class CommandDefinition — 命令注册契约。
#   - class SettingDefinition — 非执行式设置定义。
#   - class ResourceListRequest / InstanceScanRequest / InstanceGetRequest — 实例只读输入契约。
#   - validate_fields — 校验命令输入输出的基础字段。
# ============================================================

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from ECL.plugin_api.contracts import JsonValue, ProtocolError


class RequestModel(BaseModel):
    """
    拒绝额外字段和隐式字符串、布尔、数值转换的边界模型。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FieldSpec(RequestModel):
    """
    描述单个基础 JSON 字段，不接受可执行验证器或任意引用。
    """

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    type: Literal["string", "number", "integer", "bool"]
    required: StrictBool = True
    max_length: StrictInt = Field(default=4096, ge=1, le=16384)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    enum: list[str | int | float | bool] = Field(default_factory=list, max_length=64)

    def accepts(self, value: JsonValue) -> bool:
        """
        检查值的真实类型和已声明范围，不把布尔值当作整数。

        :param value: 经 JSON 校验的字段值
        :return: 是否满足字段约束
        """
        if self.enum and not any(type(item) is type(value) and item == value for item in self.enum):
            return False
        if self.type == "string":
            return isinstance(value, str) and len(value) <= self.max_length
        if self.type == "bool":
            return type(value) is bool
        if type(value) not in ({int} if self.type == "integer" else {int, float}):
            return False
        assert isinstance(value, int | float)
        return (
            math.isfinite(value)
            and (self.minimum is None or value >= self.minimum)
            and (self.maximum is None or value <= self.maximum)
        )


class CommandDefinition(RequestModel):
    """
    只登记本运行回调与基础输入输出，不开放跨插件调用。
    """

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    description: str = Field(default="", max_length=1000)
    input_fields: list[FieldSpec] = Field(default_factory=list, max_length=32)
    output_fields: list[FieldSpec] = Field(default_factory=list, max_length=32)
    callback_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    exported: StrictBool = False

    @field_validator("input_fields", "output_fields")
    @classmethod
    def unique_fields(cls, fields: list[FieldSpec]) -> list[FieldSpec]:
        """
        拒绝覆盖同名字段的模糊 schema。

        :param fields: 基础字段定义
        :return: 无重复的定义
        :raises ValueError: 名称重复时抛出
        """
        if len({item.name for item in fields}) != len(fields):
            raise ValueError("命令字段名称重复")
        return fields


class SettingDefinition(RequestModel):
    """
    定义自身设置的值类型和默认值，不接受可执行内容。
    """

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    type: Literal["bool", "string", "number", "select"]
    default: str | int | float | bool
    description: str = Field(default="", max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=64)

    def accepts(self, value: JsonValue) -> bool:
        """
        校验持久化与运行时设置值，拒绝不同基础类型间的隐式转换。

        :param value: 待提交设置值
        :return: 是否符合本项定义
        """
        if self.type == "bool":
            return type(value) is bool
        if self.type == "number":
            return type(value) in {int, float} and math.isfinite(value)
        if self.type == "select":
            return isinstance(value, str) and value in self.options
        return isinstance(value, str) and len(value) <= 16384

    @model_validator(mode="after")
    def valid_default(self) -> SettingDefinition:
        """
        定义提交时即校验默认值，避免损坏 schema 进入持久化服务。

        :return: 已校验设置定义
        :raises ValueError: 默认值或选项无效时抛出
        """
        if not self.accepts(self.default) or len(set(self.options)) != len(self.options):
            raise ValueError("设置默认值或选项无效")
        return self


def validate_fields(fields: list[FieldSpec], values: dict[str, JsonValue]) -> None:
    """
    校验完整命令载荷，拒绝额外字段、缺少必要字段或值不符合约束。

    :param fields: 注册时通过校验的字段定义
    :param values: 本次调用或回调结果
    :raises ProtocolError: 载荷不符合定义时抛出
    """
    names = {item.name for item in fields}
    if set(values) - names or any(
        (item.required and item.name not in values) or (item.name in values and not item.accepts(values[item.name]))
        for item in fields
    ):
        raise ProtocolError("插件命令字段不符合定义", "PLUGIN_SCHEMA_INVALID")


class ResourceListRequest(RequestModel):
    """
    仅发现本运行获准的实例资源，不接受其他能力或宿主路径。
    """

    capability: Literal["instances.read"]
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class InstanceScanRequest(RequestModel):
    """
    通过当前运行目录引用扫描，分页条件在游标中绑定。
    """

    root_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    force: StrictBool = False
    cursor: str | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=50, ge=1, le=100)


class InstanceGetRequest(RequestModel):
    """
    只接受本运行实例引用，不接受名称或路径替代。
    """

    instance_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
