# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义 v2 授权声明、用户决定及内容绑定，不把旧声明转换成许可。
#
# 公开接口：
#   - class PermissionDeclaration — 宿主解析的必要或可选能力及请求范围。
#   - class PermissionDecision — 用户明确的允许、拒绝和授权期限。
#   - class PluginContentBinding — 绑定代码、权限清单、完整锁及宿主 ABI。
#   - class PermissionView / GrantView — 当前运行实际有效的只读授权视图。
#   - class GrantLease — 宿主生成的执行准入快照。
#   - class PermissionManagementCaller — 受信入口分类，不能从请求体取得。
# ============================================================

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from ECL.plugin_api.snapshot import CodeSnapshot
from ECL.plugins.package_preparation import PluginPreparedPackage, verify_prepared_code

capability_name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=96)]
resource_reference = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


class _PermissionScope(BaseModel):
    """
    校验宿主解析的能力和精确资源范围。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    capability: capability_name
    resource_refs: tuple[resource_reference, ...] = Field(default=(), max_length=128)

    @field_validator("resource_refs")
    @classmethod
    def _unique_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("资源标识不能重复")
        return tuple(sorted(value))


class PermissionDeclaration(_PermissionScope):
    """
    保存插件请求的能力及宿主解析的资源范围，不赋予实际权限。

    resource_refs 必须来自宿主资源目录；instances.read 只声明能力，其实际范围
    由用户在宿主界面选择，其他能力仍按已定义的声明范围处理。
    """

    required: StrictBool = False

    @field_validator("resource_refs")
    @classmethod
    def _host_selected_instances(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        if info.data.get("capability") == "instances.read" and value:
            raise ValueError("实例资源由宿主用户选择，清单不能指定本机资源")
        return value


class PermissionDecision(_PermissionScope):
    """
    保存用户明确的决定，会话允许不能写成永久授权。

    必要性由已审阅声明决定，用户决定不能修改它。
    """

    decision: Literal["allow", "deny"]
    lifetime: Literal["session", "permanent"] = "permanent"
    expires_at: StrictFloat | None = Field(default=None, gt=0, allow_inf_nan=False)


class PermissionManagementCaller(Enum):
    """
    区分真实宿主入口；只有可信主 WebView 可以管理授权。

    后续 IPC 适配必须从原生调用上下文生成该值，不从 body 或 Dev Channel 生成 main。
    """

    main_webview = "main-webview"
    dev_channel = "dev-channel"
    worker = "worker"
    unknown = "unknown"


@dataclass(frozen=True, slots=True)
class PluginContentBinding:
    """
    保存已审阅的完整内容身份；显示名称不能代替这些摘要。

    不提供 OS 只读保证；授权更新和加载前必须再次复核实际文件。
    """

    snapshot: CodeSnapshot
    declarations: tuple[PermissionDeclaration, ...]
    target_tag: str
    lock_file: Path | None
    lock_sha256: str

    @classmethod
    def capture(
        cls,
        code_path: Path,
        declarations: tuple[PermissionDeclaration, ...],
        target_tag: str,
        lock_file: Path | None = None,
    ) -> Self:
        """
        从宿主指定的代码目录、资源声明和完整锁生成待审阅身份。

        :param code_path: 插件代码快照根目录
        :param declarations: 已校验声明，不继承旧 PermissionManager 的许可
        :param target_tag: 宿主提供的系统、架构和 Python ABI 标签
        :param lock_file: 已选择目标的完整依赖锁；无锁时绑定空字节
        :return: 规范化且不执行插件的内容身份
        :raises ValueError: 声明重复、资源或目标不合法时抛出
        """
        if not isinstance(target_tag, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", target_tag) is None:
            raise ValueError("插件目标标签无效")
        if not all(isinstance(item, PermissionDeclaration) for item in declarations):
            raise ValueError("权限声明没有经过模型校验")
        declarations = tuple(sorted(declarations, key=lambda item: item.capability))
        if len(declarations) > 128 or len({item.capability for item in declarations}) != len(declarations):
            raise ValueError("权限声明超过上限或能力重复")
        snapshot = CodeSnapshot.capture(code_path)
        lock_file = Path(lock_file).resolve() if lock_file is not None else None
        if lock_file is not None and not lock_file.is_relative_to(snapshot.root_path):
            raise ValueError("依赖锁不在代码快照内")
        lock_bytes = lock_file.read_bytes() if lock_file is not None else b""
        return cls(snapshot, declarations, target_tag, lock_file, hashlib.sha256(lock_bytes).hexdigest())

    @classmethod
    def from_prepared(cls, prepared: PluginPreparedPackage, declarations: tuple[PermissionDeclaration, ...]) -> Self:
        """
        先复核归档准备结果，再按当前完整锁建立授权身份。

        :param prepared: 已完成归档完整性和依赖准备的结果
        :param declarations: 宿主解析的 v2 能力声明
        :return: 绑定该预检版本的授权身份
        :raises ValueError: 内容或权限声明非法时抛出
        """
        verify_prepared_code(prepared.code_path, prepared.preflight.package.manifest_sha256)
        lock_file = prepared.code_path / "locks" / f"{prepared.preflight.target_tag}.txt"
        return cls.capture(
            prepared.code_path,
            declarations,
            prepared.preflight.target_tag,
            lock_file if prepared.preflight.has_target_lock else None,
        )

    @property
    def content_digest(self) -> str:
        """
        计算代码、规范化权限、完整锁和目标 ABI 的联合身份。

        :return: 不以插件名或版本号替代的 SHA-256 摘要
        """
        payload = {
            "layout_version": 1,
            "api_version": 2,
            "code_digest": self.snapshot.digest(),
            "permissions": [item.model_dump(mode="json") for item in self.declarations],
            "lock_sha256": self.lock_sha256,
            "target_tag": self.target_tag,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def verify(self) -> None:
        """
        复核审阅后的实际代码和锁文件，不自动接受新的摘要。

        :raises ValueError: 代码或依赖锁已经变化时抛出
        """
        self.snapshot.verify(self.snapshot.digest())
        lock_bytes = self.lock_file.read_bytes() if self.lock_file is not None else b""
        if hashlib.sha256(lock_bytes).hexdigest() != self.lock_sha256:
            raise ValueError("插件依赖锁已经变化")


@dataclass(frozen=True, slots=True)
class PermissionView:
    """
    描述一个声明在当前运行代次中的实际有效授权。
    """

    capability: str
    required: bool
    decision: Literal["allow", "deny"]
    resource_refs: tuple[str, ...]
    expires_at: float | None
    grant_epoch: int


@dataclass(frozen=True, slots=True)
class GrantView:
    """
    返回本安装、本内容和本次运行的授权状态，不泄漏其他插件记录。
    """

    installation_id: str
    content_digest: str
    revision: int
    grant_epoch: int
    items: tuple[PermissionView, ...]
    required_satisfied: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class GrantLease:
    """
    保存宿主生成的准入快照；后续阶段必须再次检查其 epoch 与主体。
    """

    installation_id: str
    content_digest: str
    run_id: str
    grant_epoch: int
    capability: str
    resource_refs: tuple[str, ...]
