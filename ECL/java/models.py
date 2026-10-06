# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：Java 运行时、来源包与安装计划的稳定领域模型，以及原子登记清单。
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
#   - class JavaRegistry — 管理已发现及已登记 Java 的持久状态。
# ============================================================
from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from types import MappingProxyType
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from ECL.game import JavaRuntime, JavaScanner
from ECL.utils import atomic_write_text
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


class JavaRegistry:
    """
    用可校验的不可变清单保存登记状态，损坏清单不会被静默覆盖。

    公共修改在同一线程锁与 OS 文件锁下重新读取磁盘，以免多个启动器覆盖
    启用状态或安装拥有权；网络和全系统扫描不持有文件锁。
    """

    root_path: Path
    registry_path: Path

    def __init__(self, root_path: Path) -> None:
        """
        创建私有管理目录，登记和临时安装均归属该根目录。

        :param root_path: 后端确定的 Java 管理根目录
        """
        self.root_path = root_path.resolve()
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root_path / "registry.json"
        self._thread_lock = RLock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """
        有界取得线程和文件互斥，退出时始终释放锁和句柄。

        :return: 仅用于短时清单或使用证据变更的互斥作用域
        :raises JavaError: 五秒内无法取得文件锁时抛出
        """
        with self._thread_lock:
            lock_path = self.root_path / ".registry.lock"
            if lock_path.is_symlink():
                raise JavaError("Java 管理锁文件无效", "JAVA_REGISTRY_INVALID")
            with lock_path.open("a+b") as handle:
                handle.seek(0, 2)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                deadline = monotonic() + 5
                while True:
                    try:
                        handle.seek(0)
                        if sys.platform == "win32":
                            import msvcrt

                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if monotonic() >= deadline:
                            raise JavaError("Java 管理正在处理另一项操作，请稍后重试", "JAVA_BUSY") from exc
                        sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if sys.platform == "win32":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> RegistryState:
        """
        读取并校验清单，调用方修改前必须取得 locked 作用域。

        :return: 当前不可变清单
        :raises JavaError: 清单损坏、过大或链接替换时抛出并保留原文件
        """
        if not self.registry_path.exists():
            if self.registry_path.is_symlink():
                raise JavaError("Java 登记清单已被替换", "JAVA_REGISTRY_INVALID")
            return RegistryState()
        try:
            if self.registry_path.is_symlink() or self.registry_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("invalid registry")
            return RegistryState.model_validate_json(self.registry_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise JavaError("Java 登记清单无法读取，原文件已保留", "JAVA_REGISTRY_INVALID") from exc

    def write(self, state: RegistryState) -> None:
        """
        在调用方已持有 locked 时原子替换清单，失败不提交内存状态。

        :param state: 已校验的完整清单
        :raises JavaError: 持久化失败时抛出
        """
        try:
            content = state.model_dump_json(indent=2)
            if len(content.encode()) > 8 * 1024 * 1024:
                raise JavaError("Java 登记清单超过容量限制", "JAVA_REGISTRY_LIMIT")
            atomic_write_text(self.registry_path, content)
        except OSError as exc:
            raise JavaError("Java 登记保存失败", "JAVA_REGISTRY_WRITE_FAILED") from exc

    def register(self, executable_path: Path) -> RuntimeRecord:
        """
        验证手动选择的 Java 并长期登记，不改变其目录或当前启动配置。

        :param executable_path: 用户选择的 Java 或 javaw 可执行文件
        :return: 已确认落盘的运行时记录
        :raises JavaError: 探测失败或保存失败时抛出
        """
        runtime = JavaScanner.probe(executable_path)
        if runtime is None:
            raise JavaError("所选文件不是可用的 Java 运行时", "JAVA_INVALID_RUNTIME")
        candidate = JavaPolicy.record(runtime, "manual")
        if any(candidate.java_home_path.is_relative_to(self.root_path / folder) for folder in (".staging", ".trash")):
            raise JavaError("该 Java 仍在安装或清理中，请等待任务结束", "JAVA_BUSY")
        with self.locked():
            state = self.read()
            existing = next((record for record in state.runtimes if record.runtime_id == candidate.runtime_id), None)
            if existing and existing.origin == "managed":
                candidate = existing.model_copy(update={"validation_status": "valid"})
            else:
                candidate = candidate.model_copy(
                    update={
                        "is_enabled": existing.is_enabled if existing else True,
                        "registered_at": existing.registered_at if existing else datetime.now(UTC).isoformat(),
                    }
                )
            self.replace(state, candidate)
        return candidate

    def replace(self, state: RegistryState, record: RuntimeRecord) -> None:
        """
        在已持锁且已读取最新清单时，替换一个运行时并原子持久化。

        :param state: 最新的已校验登记快照
        :param record: 所属运行时的不可变登记记录
        """
        records = {item.runtime_id: item for item in state.runtimes}
        records[record.runtime_id] = record
        self.write(state.model_copy(update={"runtimes": tuple(records.values())}))

    def set_enabled(self, runtime_id: str, is_enabled: bool) -> RuntimeRecord:
        """
        修改后续启动的可用状态，不终止已经启动的游戏。

        :param runtime_id: 登记的稳定运行时身份
        :param is_enabled: 是否允许后续使用
        :return: 已保存的记录
        :raises JavaError: 条目不存在或启用目标失效时抛出
        """
        with self.locked():
            state = self.read()
            record = self.find(state, runtime_id)
            if is_enabled and JavaScanner.probe(record.executable_path) is None:
                raise JavaError("Java 已失效，请重新定位或安装", "JAVA_INVALID_RUNTIME")
            changed = record.model_copy(update={"is_enabled": is_enabled})
            self.replace(state, changed)
            return changed

    @staticmethod
    def find(state: RegistryState, runtime_id: str) -> RuntimeRecord:
        """
        从已读取的清单查找运行时，不接受目录路径作为删除身份。

        :param state: 最新的已校验登记快照
        :param runtime_id: 已登记的运行时身份
        """
        for record in state.runtimes:
            if record.runtime_id == runtime_id:
                return record
        raise JavaError("Java 登记条目不存在", "JAVA_RUNTIME_NOT_FOUND")

    def validate_owned_directory(self, record: RuntimeRecord) -> Path:
        """
        核对托管路径与原安装文件清单，拒绝外部目录和未知新增文件。

        :param record: 所属运行时的不可变登记记录
        """
        if record.origin != "managed" or record.install_path is None or not record.package_checksum:
            raise JavaError("该 Java 不属于启动器安装", "JAVA_NOT_MANAGED")
        expected = self.root_path / "installed" / f"temurin-{record.major_version}-{record.package_checksum[:16]}"
        path = record.install_path
        if path != expected or path.is_symlink() or path.resolve() != expected.absolute() or not path.is_dir():
            raise JavaError("Java 安装目录的拥有权无法确认", "JAVA_DIRECTORY_CHANGED")
        expected_files = set(record.installed_files)
        for child in path.rglob("*"):
            if not child.resolve().is_relative_to(path) or child.relative_to(path).as_posix() not in expected_files:
                raise JavaError("Java 目录含未登记内容，请保留并手动检查", "JAVA_DIRECTORY_CHANGED")
        return path
