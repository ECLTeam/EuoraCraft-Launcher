# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：解析插件完整依赖锁并保留每个 wheel 的允许摘要，不解析新版本。
#
# 公开接口：
#   - class PluginDependencyError — 依赖契约或安装失败。
#   - class LockedPackage — 一个精确包版本及允许的 wheel 摘要。
#   - class PluginDependencyLock — 完整锁与直接依赖校验。
#   - plugin_target() — 取得实际宿主的平台及 Python 目标标签。
# ============================================================

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

max_lock_bytes = 2 * 1024**2
digest_pattern = re.compile(r"[0-9a-f]{64}\Z")


class PluginDependencyError(ValueError):
    """
    表示依赖锁、wheel、下载或安装目录不满足插件安装契约。

    失败不允许写入就绪标记或修改宿主依赖。
    """


@dataclass(frozen=True, slots=True)
class LockedPackage:
    """
    保存规范化包名、精确版本及允许下载的完整 wheel 摘要。

    摘要来自归档锁，不以索引返回的摘要替代。
    """

    name: str
    version: Version
    hashes: frozenset[str]


@dataclass(frozen=True, slots=True)
class PluginDependencyLock:
    """
    保存可共享安装目录所需的完整锁，不在用户设备上重新求解版本。

    空锁只用于无依赖插件；所有非空行必须为精确版本及 SHA-256。
    """

    raw: bytes
    packages: tuple[LockedPackage, ...]

    @classmethod
    def parse(cls, raw: bytes) -> PluginDependencyLock:
        """
        解析有界 UTF-8 锁并拒绝索引选项、URL、重复包和非精确版本。

        :param raw: 归档中的原始完整锁字节
        :return: 保留原始字节与 wheel 摘要的锁模型
        :raises PluginDependencyError: 锁包含不支持的语法或无效摘要
        """
        if len(raw) > max_lock_bytes:
            raise PluginDependencyError("Python 依赖锁超过大小上限")
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise PluginDependencyError("Python 依赖锁必须是 UTF-8") from exc
        packages: list[LockedPackage] = []
        names: set[str] = set()
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split()
            try:
                requirement = Requirement(parts[0])
                specifiers = tuple(requirement.specifier)
                if (
                    requirement.url
                    or requirement.extras
                    or requirement.marker
                    or len(specifiers) != 1
                    or specifiers[0].operator != "=="
                    or "*" in specifiers[0].version
                ):
                    raise PluginDependencyError("Python 依赖锁必须使用单个精确版本")
                version = Version(specifiers[0].version)
            except (InvalidRequirement, InvalidVersion) as exc:
                raise PluginDependencyError("Python 依赖锁包含无效包名或版本") from exc
            hashes = frozenset(part.removeprefix("--hash=sha256:") for part in parts[1:])
            if (
                not hashes
                or any(not part.startswith("--hash=sha256:") for part in parts[1:])
                or any(digest_pattern.fullmatch(digest) is None for digest in hashes)
            ):
                raise PluginDependencyError("Python 依赖锁只允许精确版本和 SHA-256 哈希")
            name = canonicalize_name(requirement.name)
            if name in names:
                raise PluginDependencyError("Python 依赖锁包含重复包名")
            names.add(name)
            packages.append(LockedPackage(name, version, hashes))
            if len(packages) > 512:
                raise PluginDependencyError("Python 依赖锁包数量超过上限")
        return cls(raw, tuple(packages))

    def validate_direct(self, raw_dependencies: object) -> tuple[Requirement, ...]:
        """
        校验直接依赖由锁覆盖，不允许插件配置下载源或环境标记。

        :param raw_dependencies: plugin.json 的 pythonDependencies 边界值
        :return: 用于闭包与 extras 校验的直接需求
        :raises PluginDependencyError: 直接依赖格式或锁定版本不满足契约
        """
        if not isinstance(raw_dependencies, list) or any(not isinstance(item, str) for item in raw_dependencies):
            raise PluginDependencyError("pythonDependencies 必须是依赖字符串数组")
        versions = {package.name: package.version for package in self.packages}
        requirements: list[Requirement] = []
        for item in raw_dependencies:
            try:
                requirement = Requirement(item)
            except InvalidRequirement as exc:
                raise PluginDependencyError(f"Python 直接依赖无效: {item}") from exc
            if requirement.url is not None or requirement.marker is not None:
                raise PluginDependencyError("Python 直接依赖不能使用任意 URL 或环境标记")
            version = versions.get(canonicalize_name(requirement.name))
            if version is None or version not in requirement.specifier:
                raise PluginDependencyError(f"直接依赖未被当前目标锁定版本满足: {requirement.name}")
            requirements.append(requirement)
        return tuple(requirements)


def plugin_target() -> str:
    """
    根据实际宿主而非专用运行时生成平台及 Python 目标标签。

    :return: 形如 windows-x86_64-cp312 的当前目标标签
    :raises PluginDependencyError: 宿主不是受支持的 CPython 平台
    """
    system = platform.system().lower()
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    if (
        sys.implementation.name != "cpython"
        or system not in {"windows", "linux", "darwin"}
        or machine not in {"x86_64", "arm64"}
    ):
        raise PluginDependencyError("当前宿主不支持插件 wheel 安装")
    return f"{system}-{machine}-cp{sys.version_info.major}{sys.version_info.minor}"
