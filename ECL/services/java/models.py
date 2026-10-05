# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：Java 运行时、来源包、安装计划及登记清单的稳定领域模型。
#
# 公开接口：
#   - class JavaError — 表示可返回 IPC 的 Java 管理失败。
#   - class JavaModel — 定义 Java IPC 的字段命名与校验边界。
#   - class RuntimeRecord — 保存一个运行时的身份、拥有权与有效状态。
#   - class RuntimeView — 为界面提供引用和占用快照。
#   - class JavaInventory — 描述当前平台的完整运行时清单。
#   - class JavaReferenceConfig — 校验配置中的 Java 引用。
#   - class CleanupRecord — 保存遗留目录的可信文件清单。
#   - class RegistryState — 保存原子登记清单。
#   - class JavaPackage — 保存经过来源元数据校验的下载包。
#   - class JavaInstallPlan — 保存有有效期的服务端安装计划。
#   - class RuntimeUsage — 保存启动或游戏进程的使用证据。
#   - class JavaPolicy — 归属平台、版本与路径规则。
# ============================================================
from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from ECL.game import JavaRuntime
from ECL.utils.errors import GameServiceError


class JavaError(GameServiceError):
    """
    Java 管理的稳定业务失败，不包含个人路径或不可信原始响应。
    """


class JavaModel(BaseModel):
    """
    用不可变模型校验 Java JSON 边界，并为 IPC 输出驼峰字段。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, alias_generator=to_camel)


class RuntimeRecord(JavaModel):
    """
    区分运行时本身、发现来源和安装拥有权，失效条目仍可呈现。
    """

    runtime_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    executable_path: Path
    java_home_path: Path
    vendor: str = ""
    runtime_kind: Literal["JDK", "JRE"] = "JRE"
    major_version: int = 0
    full_version: str = ""
    architecture: str = "unknown"
    origin: Literal["system", "manual", "managed"] = "system"
    is_enabled: bool = True
    validation_status: Literal["valid", "missing", "invalid"] = "valid"
    discovery_sources: tuple[str, ...] = ()
    distribution_source: str | None = None
    release_name: str | None = None
    package_checksum: str | None = None
    package_platform: str | None = None
    install_path: Path | None = None
    installed_files: tuple[str, ...] = ()
    registered_at: str = ""


class CleanupRecord(JavaModel):
    """
    保存可重试目录与其原有成员，清理不会删除未知新增文件。
    """

    relative_path: str
    owned_files: tuple[str, ...]


class RegistryState(JavaModel):
    """
    保存登记版本、运行时和可重试的安装目录清理记录。
    """

    schema_version: Literal[1] = 1
    runtimes: tuple[RuntimeRecord, ...] = ()
    cleanup_paths: tuple[CleanupRecord, ...] = ()


class RuntimeView(RuntimeRecord):
    """
    给管理界面附加当前引用与占用状态，不将这些快照长期写回登记清单。
    """

    is_in_use: bool = False
    usage_unknown: bool = False
    references: tuple[str, ...] = ()


class JavaPackage(JavaModel):
    """
    保存可安装的具体发布包，字段来自经过校验的官方元数据。
    """

    package_id: str
    distribution_source: Literal["temurin"] = "temurin"
    release_name: str
    major_version: int = Field(ge=1, le=99)
    runtime_kind: Literal["JDK", "JRE"]
    platform: Literal["windows", "linux", "mac", "alpine-linux"]
    architecture: str
    filename: str
    download_url: str
    download_bytes: int = Field(gt=0, le=512 * 1024 * 1024)
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")


class JavaInstallPlan(JavaModel):
    """
    保存后端确定的包和正式目录，前端不能修改 URL 或目标路径。
    """

    plan_id: str
    package: JavaPackage
    install_path: Path
    expires_at: float
    estimated_free_bytes: int


class RuntimeUsage(JavaModel):
    """
    持久记录进程身份，启动器退出不意味着游戏已经停止。
    """

    lease_id: str
    runtime_id: str
    process_id: int
    process_started_at: float
    is_game: bool = False


class JavaInventory(JavaModel):
    """
    输出完整管理清单与平台信息，保留无效登记而不提供给选择器。
    """

    runtimes: tuple[RuntimeView, ...]
    platform: str
    architecture: str
    managed_root_path: Path
    cleanup_pending_count: int = 0


class JavaReferenceConfig(BaseModel):
    """
    在配置边界只读取 Java 引用相关字段，未知路径结构不能被静默忽略。
    """

    model_config = ConfigDict(extra="ignore")
    java_auto: bool = True
    java_path: str = ""
    minecraft_paths: tuple[Path, ...] = ()

    @field_validator("minecraft_paths", mode="before")
    @classmethod
    def parse_roots(cls, value: object) -> tuple[Path, ...]:
        """
        把当前配置支持的路径条目转换为明确的根目录集合。

        :param value: 需要归一化的架构声明
        """
        if not isinstance(value, (list, tuple)):
            raise ValueError("invalid minecraft paths")
        roots: list[Path] = []
        for item in value:
            path = item.get("path") if isinstance(item, dict) else item
            if not isinstance(path, (str, Path)) or not str(path).strip():
                raise ValueError("invalid minecraft path")
            roots.append(Path(path).expanduser())
        return tuple(roots)


class JavaPolicy:
    """
    归属 Java 的架构别名、版本排序、真实路径身份和托管存储规则。
    """

    architecture_by_alias: ClassVar[MappingProxyType[str, str]] = MappingProxyType(
        {
            "amd64": "x64",
            "x86_64": "x64",
            "aarch64": "arm64",
            "arm64": "arm64",
            "i386": "x86",
            "i686": "x86",
            "x86": "x86",
        }
    )

    @classmethod
    def architecture(cls, value: str) -> str:
        """
        归一化声明的运行架构，未知值保留原值。

        :param value: 需要归一化的架构声明
        """
        return cls.architecture_by_alias.get(value.lower(), value.lower())

    @classmethod
    def host(cls) -> tuple[str, str]:
        """
        在运行时读取操作系统和主机架构，兼顾 Windows 仿真进程环境。
        """
        system = "windows" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "linux"
        if system == "linux" and Path("/etc/alpine-release").is_file():
            system = "alpine-linux"
        machine = os.getenv("PROCESSOR_ARCHITEW6432") or platform.machine()
        return system, cls.architecture(machine)

    @staticmethod
    def version_key(version: str) -> tuple[int, ...]:
        """
        比较 Java 数字补丁版本，保留 1.8.0_ 更新号的旧格式语义。

        :param version: 实际探测的完整 Java 版本
        """
        numbers = tuple(int(part) for part in re.findall(r"\d+", version))
        return numbers[1:] if numbers[:1] == (1,) and len(numbers) > 1 else numbers

    @classmethod
    def record(cls, runtime: JavaRuntime, origin: Literal["system", "manual", "managed"] = "system") -> RuntimeRecord:
        """
        将 Core 探测结果转换为稳定模型，java/javaw 共用真实 Home 身份。

        :param runtime: Core 已验证的运行时信息
        :param origin: 运行时发现及拥有权分类
        """
        executable = runtime.path.resolve(strict=False)
        home = executable.parent.parent.resolve(strict=False)
        identity = str(home)
        if sys.platform == "win32":
            identity = identity.casefold()
        return RuntimeRecord(
            runtime_id=hashlib.sha256(identity.encode()).hexdigest()[:24],
            executable_path=executable,
            java_home_path=home,
            vendor=runtime.vendor or "",
            runtime_kind="JDK" if runtime.is_jdk else "JRE",
            major_version=cls.version_key(runtime.version)[0],
            full_version=runtime.version,
            architecture=cls.architecture(runtime.architecture),
            origin=origin,
        )

    @staticmethod
    def storage_root(data_path: Path) -> Path:
        """
        优先使用启动器数据目录，应用包或不可写位置回退到用户持久目录。

        :param data_path: 当前启动器持久化数据目录
        """
        root = data_path / "runtimes" / "java"
        is_bundle = any(part.lower().endswith(".app") for part in data_path.parts)
        if not is_bundle:
            try:
                root.mkdir(parents=True, exist_ok=True)
                probe = root / f".write-probe-{os.getpid()}"
                with probe.open("xb"):
                    pass
                probe.unlink()
                return root.resolve()
            except OSError:
                pass
        if sys.platform == "win32":
            base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        root = base / "EuoraCraft-Launcher" / "runtimes" / "java"
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()
