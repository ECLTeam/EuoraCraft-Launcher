# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：定义插件运行的内容绑定与时效准入快照，不包含权限概念。
#
# 公开接口：
#   - class PluginContentBinding — 绑定代码、完整锁及宿主 ABI 的内容身份。
#   - class PluginLease — 宿主生成的运行时效准入快照。
#   - class PluginRunView — 当前运行的只读身份与失效状态。
# ============================================================

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from ECL.plugins.api.snapshot import CodeSnapshot
from ECL.plugins.package_preparation import PluginPreparedPackage, verify_prepared_code


@dataclass(frozen=True, slots=True)
class PluginContentBinding:
    """
    保存安装时确认的完整内容身份；显示名称与版本号不能代替这些摘要。

    不提供 OS 只读保证；Worker 启动和副作用提交前必须再次复核实际文件。
    """

    snapshot: CodeSnapshot
    target_tag: str
    lock_file: Path | None
    lock_sha256: str

    @classmethod
    def capture(cls, code_path: Path, target_tag: str, lock_file: Path | None = None) -> Self:
        """
        从宿主指定的代码目录和完整锁生成运行内容身份。

        :param code_path: 插件代码快照根目录
        :param target_tag: 宿主提供的系统、架构和 Python ABI 标签
        :param lock_file: 已选择目标的完整依赖锁；无锁时绑定空字节
        :return: 规范化且不执行插件的内容身份
        :raises ValueError: 目标标签不合法或依赖锁越界时抛出
        """
        if not isinstance(target_tag, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", target_tag) is None:
            raise ValueError("插件目标标签无效")
        snapshot = CodeSnapshot.capture(code_path)
        lock_file = Path(lock_file).resolve() if lock_file is not None else None
        if lock_file is not None and not lock_file.is_relative_to(snapshot.root_path):
            raise ValueError("依赖锁不在代码快照内")
        lock_bytes = lock_file.read_bytes() if lock_file is not None else b""
        return cls(snapshot, target_tag, lock_file, hashlib.sha256(lock_bytes).hexdigest())

    @classmethod
    def from_prepared(cls, prepared: PluginPreparedPackage) -> Self:
        """
        先复核归档准备结果，再按当前完整锁建立运行内容身份。

        :param prepared: 已完成归档完整性和依赖准备的结果
        :return: 绑定该预检版本的内容身份
        :raises ValueError: 内容校验失败时抛出
        """
        verify_prepared_code(prepared.code_path, prepared.preflight.package.manifest_sha256)
        lock_file = prepared.code_path / "locks" / f"{prepared.preflight.target_tag}.txt"
        return cls.capture(
            prepared.code_path,
            prepared.preflight.target_tag,
            lock_file if prepared.preflight.has_target_lock else None,
        )

    @property
    def content_digest(self) -> str:
        """
        计算代码、完整锁和目标 ABI 的联合身份。

        :return: 不以插件名或版本号替代的 SHA-256 摘要
        """
        payload = {
            "layout_version": 2,
            "api_version": 2,
            "code_digest": self.snapshot.digest(),
            "lock_sha256": self.lock_sha256,
            "target_tag": self.target_tag,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def verify(self) -> None:
        """
        复核实际代码和锁文件与身份一致，不自动接受新的摘要。

        :raises ValueError: 代码或依赖锁已经变化时抛出
        """
        self.snapshot.verify(self.snapshot.digest())
        lock_bytes = self.lock_file.read_bytes() if self.lock_file is not None else b""
        if hashlib.sha256(lock_bytes).hexdigest() != self.lock_sha256:
            raise ValueError("插件依赖锁已经变化")


@dataclass(frozen=True, slots=True)
class PluginLease:
    """
    保存宿主生成的运行时效准入快照；后续阶段必须再次检查其代次与主体。
    """

    installation_id: str
    content_digest: str
    run_id: str
    run_epoch: int


@dataclass(frozen=True, slots=True)
class PluginRunView:
    """
    返回本安装、本内容和本次运行的身份与失效状态，不泄漏其他插件记录。
    """

    installation_id: str
    content_digest: str
    run_epoch: int
    error_code: str | None
