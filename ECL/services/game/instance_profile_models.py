# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：严格解析完整实例资料及其物理版本，供实例查询和共享资料写入共同使用。
#
# 公开接口：
#   - class InstanceProfilePatch — 插件允许修改的四字段严格增量。
#   - class InstanceProfileSnapshot — 完整资料与不透明物理版本。
#   - validate_profile — 校验当前资料而不丢弃未修改字段。
#   - read_profile_snapshot — 无写入地读取完整资料和存在状态。
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ECL.plugin_api.contracts import JsonValue
from ECL.utils.instance_reading import DirectoryIdentity, InstanceReadBoundary, InstanceReadError


class InstanceProfilePatch(BaseModel):
    """
    仅表示用户授权的四个资料字段，未传字段与空值具有不同含义。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    alias: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    favorite: StrictBool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)

    @field_validator("alias", mode="before")
    @classmethod
    def validate_alias(cls, value: object) -> object:
        """
        别名去首尾空白后必须仍有内容，不以空值隐式重置自动值。
        """
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        """
        校验上限后按大小写去重，保留第一项的展示文本。
        """
        if value is None:
            return None
        result: list[str] = []
        seen: set[str] = set()
        for tag in value:
            tag = tag.strip()
            if not tag or len(tag) > 40:
                raise ValueError("标签不能为空且不能超过40字符")
            if tag.casefold() not in seen:
                result.append(tag)
                seen.add(tag.casefold())
        return result

    @model_validator(mode="after")
    def validate_nonempty(self) -> InstanceProfilePatch:
        """
        拒绝空增量与显式null，false和空标签列表保持有效。
        """
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("资料增量不能为空或包含null")
        return self


def validate_profile(document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """
    验证资料当前格式并保留全部原字段，不从损坏文档生成默认值。

    :param document: 已经拒绝重复字段及非JSON值的对象
    :return: 未规范化或过滤字段的同一完整文档
    :raises InstanceReadError: 显式版本、已定义字段类型或范围不满足当前契约
    """
    try:
        if "schemaVersion" in document and (
            type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
        ):
            raise ValueError("资料版本不支持")
        public = {name: document[name] for name in ("alias", "description", "favorite", "tags") if name in document}
        if public:
            InstanceProfilePatch.model_validate(public)
        for name in ("pinned", "hidden"):
            if name in document and type(document[name]) is not bool:
                raise ValueError("资料状态无效")
        if "pinOrder" in document and (type(document["pinOrder"]) is not int or document["pinOrder"] < 0):
            raise ValueError("置顶顺序无效")
        if "categoryId" in document and (
            not isinstance(document["categoryId"], str) or not 1 <= len(document["categoryId"]) <= 64
        ):
            raise ValueError("分类无效")
        if "preferredExternalSource" in document and not isinstance(document["preferredExternalSource"], str):
            raise ValueError("资料来源无效")
        if "icon" in document:
            icon = document["icon"]
            if (
                not isinstance(icon, dict)
                or set(icon) != {"type", "value"}
                or any(not isinstance(v, str) for v in icon.values())
            ):
                raise ValueError("图标资料无效")
        return document
    except ValueError as exc:
        raise InstanceReadError("实例资料格式无效，原文件已保留", "PLUGIN_INSTANCE_METADATA_INVALID") from exc


@dataclass(frozen=True, slots=True)
class InstanceProfileSnapshot:
    """
    保存一次完整本地资料快照，不将外部资料或统计合并成写入基础。
    """

    document: dict[str, JsonValue]
    instance_identity: DirectoryIdentity
    directory_identity: DirectoryIdentity | None
    file_version: tuple[int, ...] | None
    sha256: str | None

    @property
    def etag(self) -> str:
        """
        计算绑定存在状态、实际对象及完整原始字节的不透明版本。

        :return: 64位小写十六进制
        """
        facts = [
            self.instance_identity.model_dump(),
            self.directory_identity.model_dump() if self.directory_identity else None,
            self.file_version,
            self.sha256,
        ]
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def read_profile_snapshot(reader: InstanceReadBoundary, version_id: str) -> InstanceProfileSnapshot:
    """
    使用同一受控读取的字节与元数据建立完整版本，不创建缺少的资料目录。

    :param reader: 有批准根身份和有限截止时间的读取边界
    :param version_id: 已由领域入口校验的单个实例目录名
    :return: 完整快照；仅确实缺少文件时document为空
    :raises InstanceReadError: 文件损坏、变化、超限或目录身份不可确认
    """
    relative = Path("versions") / version_id
    instance_identity = reader.directory(relative)
    versions: list[tuple[bytes, os.stat_result]] = []
    document = reader.read_json(
        relative / ".ecl/instance.json",
        65536,
        optional=True,
        capture=lambda content, info: versions.append((content, info)),
    )
    parent_path = reader.root_path / relative / ".ecl"
    directory = reader.directory(relative / ".ecl") if parent_path.exists() or parent_path.is_symlink() else None
    if reader.directory(relative) != instance_identity:
        raise InstanceReadError("实例身份已变化")
    if not versions:
        return InstanceProfileSnapshot({}, instance_identity, directory, None, None)
    content, info = versions[0]
    assert document is not None
    if info.st_nlink != 1:
        raise InstanceReadError("实例资料不是普通单链接文件", "PLUGIN_INSTANCE_METADATA_INVALID")
    validate_profile(document)
    version = (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size, info.st_nlink)
    return InstanceProfileSnapshot(document, instance_identity, directory, version, hashlib.sha256(content).hexdigest())
