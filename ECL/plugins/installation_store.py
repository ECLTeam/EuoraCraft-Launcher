# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验第三方 v2 清单并原子维护安装身份，不导入插件或授予权限。
#
# 公开接口：
#   - class WorkerManifest — 第三方插件的受控运行声明。
#   - class PluginInstallationError — 安装身份损坏或来源冲突。
#   - class PluginInstallationStore — 安装登记、更新和卸载资格管理。
# ============================================================

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from ECL.plugins.dependencies import parse_dependency, parse_version
from ECL.plugins.environment_pool import _exclusive_lock
from ECL.plugins.grant_models import PermissionDeclaration
from ECL.utils import atomic_write_text


class PluginInstallationError(ValueError):
    """
    表示安装身份不能可靠读取或与实际来源不符，保留原文件以供恢复。
    """

    error_code: str = "PLUGIN_INSTALLATION_INVALID"


class WorkerManifest(BaseModel):
    """
    校验入口和权限声明，其他展示字段由既有元数据界面消费，不赋予执行能力。
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    api_version: StrictInt = Field(ge=2, le=2)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    entry_point: str = "main:Plugin"
    version: str = Field(default="0.0.0", max_length=64)
    python_dependencies: list[str] = Field(default_factory=list, alias="pythonDependencies", max_length=128)
    dependencies: dict[str, str | dict[str, str | bool]] = Field(default_factory=dict, max_length=128)
    permissions: tuple[PermissionDeclaration, ...] = Field(default=(), max_length=128)
    reserved_names: ClassVar[frozenset[str]] = frozenset(
        {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """
        拒绝 Windows 设备名和尾点别名，确保安装身份对应唯一文件。

        :param value: 声明的插件名称
        :return: 合法名称
        :raises ValueError: 名称会映射到设备或别名时抛出
        """
        if value.endswith(".") or value.split(".", 1)[0].upper() in cls.reserved_names:
            raise ValueError("插件名称不能使用设备名或尾点")
        return value

    @field_validator("entry_point")
    @classmethod
    def validate_entry(cls, value: str) -> str:
        """
        只接受快照内 Python 模块与合法类名，不接受任意导入表达式。

        :param value: 包内模块与类名
        :return: 已校验入口
        :raises ValueError: 入口格式或路径无效时抛出
        """
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*", value) is None:
            raise ValueError("插件入口必须是包内模块和类名")
        return value

    @property
    def entry_file(self) -> str:
        """
        返回代码快照内的相对入口文件。

        :return: Python 相对路径
        """
        return self.entry_point.split(":", 1)[0] + ".py"

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        """
        在进入依赖图之前拒绝无效版本。

        :param value: 清单版本
        :return: 有效版本字符串
        :raises ValueError: 版本不可解析时抛出
        """
        if parse_version(value) is None:
            raise ValueError("插件版本无效")
        return value

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(
        cls, value: dict[str, str | dict[str, str | bool]]
    ) -> dict[str, str | dict[str, str | bool]]:
        """
        在执行依赖排序之前拒绝未知字段、错误类型和无效版本约束。

        :param value: 插件间依赖声明
        :return: 已校验声明
        :raises ValueError: 依赖不符合契约时抛出
        """
        for name, requirement in value.items():
            if isinstance(requirement, dict) and (
                set(requirement) - {"version", "optional"}
                or not isinstance(requirement.get("version", ""), str)
                or type(requirement.get("optional", False)) is not bool
            ):
                raise ValueError("插件间依赖字段无效")
            if parse_dependency(name, requirement) is None:
                raise ValueError("插件间依赖约束无效")
        return value

    @property
    def entry_class(self) -> str:
        """
        返回 Worker 内实例化的 v2 类名。

        :return: 合法 Python 类名
        """
        return self.entry_point.split(":", 1)[1]


class _InstallationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str
    source: Literal["directory", "package"]
    active: bool = True


class PluginInstallationStore:
    """
    保存独立于插件代码的安装资格，卸载重装不继承历史授权。

    文件锁覆盖读取及替换；损坏记录明确失败，不自动重置或删除用户文件。
    """

    def __init__(self, data_path: Path) -> None:
        """
        绑定宿主指定的数据目录。

        :param data_path: 当前应用数据根目录
        """
        self._data_path = data_path.resolve()
        self._root_path = self._data_path / "plugin_installations"

    def ensure(self, name: str, source: Literal["directory", "package"]) -> str:
        """
        登记新安装或读取同来源活动身份，不执行插件或继承旧权限。

        :param name: 已发现的插件名称
        :param source: 宿主确定的安装来源
        :return: 活动安装身份
        :raises PluginInstallationError: 名称、文件或来源不可信时抛出
        """
        record_path = self._record_path(name)
        with _exclusive_lock(record_path.with_suffix(".lock"), wait_seconds=2):
            previous = self._read(record_path)
            if previous is not None and previous.active:
                if previous.source != source or previous.name != name:
                    raise PluginInstallationError("插件安装身份与来源不符")
                return previous.installation_id
            record = _InstallationRecord(installation_id=uuid4().hex, name=name, source=source)
            atomic_write_text(record_path, record.model_dump_json())
            return record.installation_id

    def retire(self, name: str) -> None:
        """
        使卸载安装失去活动资格，保留历史授权和记录供审计。

        :param name: 要撤销活动资格的插件
        :raises PluginInstallationError: 记录损坏时抛出
        """
        record_path = self._record_path(name)
        with _exclusive_lock(record_path.with_suffix(".lock"), wait_seconds=2):
            record = self._read(record_path)
            if record is not None:
                atomic_write_text(record_path, record.model_copy(update={"active": False}).model_dump_json())

    def _record_path(self, name: str) -> Path:
        if self._root_path.is_symlink() or self._root_path.resolve().parent != self._data_path:
            raise PluginInstallationError("插件安装身份目录超出数据边界")
        try:
            WorkerManifest.model_validate({"name": name, "api_version": 2})
        except ValidationError as exc:
            raise PluginInstallationError("插件安装名称无效") from exc
        return self._root_path / f"{name}.json"

    @staticmethod
    def _read(record_path: Path) -> _InstallationRecord | None:
        """
        区分首次登记与损坏文件，不能因读失败悄悄换用新身份。
        """
        if not record_path.exists():
            return None
        try:
            if record_path.is_symlink() or record_path.stat().st_size > 8192:
                raise PluginInstallationError("插件安装身份记录无效")
            return _InstallationRecord.model_validate_json(record_path.read_bytes())
        except (OSError, ValidationError) as exc:
            raise PluginInstallationError("插件安装身份记录无法读取") from exc
