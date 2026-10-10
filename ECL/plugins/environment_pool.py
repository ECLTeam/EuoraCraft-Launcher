# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按宿主 ABI 和完整依赖锁复用数据目录中的 wheel 安装目录，不创建 venv。
#
# 公开接口：
#   - class PluginEnvironmentSpec — 宿主目标及完整锁安装输入。
#   - class InstalledPackage — 已安装包的版本及导入归属。
#   - class PluginDependencyDirectory — 校验通过的共享依赖目录。
#   - class PluginEnvironmentPool — 原子准备和查询依赖目录。
#   - PluginEnvironmentError — 依赖目录安装失败异常的兼容名称。
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from packaging.requirements import Requirement
from packaging.version import Version

from ECL.plugins.dependency_lock import PluginDependencyError, PluginDependencyLock, digest_pattern, plugin_target
from ECL.plugins.wheel_installation import (
    PluginWheel,
    PluginWheelRepository,
    install_wheel,
    validate_wheel_dependencies,
)
from ECL.utils import atomic_write_text

PluginEnvironmentError = PluginDependencyError
safe_component_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
lock_wait_seconds = 120


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """
    对同一安装键使用有界跨进程文件锁，防止多个启动器覆盖目录。

    Windows 和 POSIX 分别使用平台文件锁；进程退出时由系统释放。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise PluginDependencyError("插件安装锁不能是符号链接")
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0)
        deadline = monotonic() + lock_wait_seconds
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if monotonic() >= deadline:
                        raise PluginDependencyError("等待插件安装锁超时") from exc
                    sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if monotonic() >= deadline:
                        raise PluginDependencyError("等待插件安装锁超时") from exc
                    sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class PluginEnvironmentSpec:
    """
    描述固定锁的库安装，不包含 Python 可执行文件或 SDK 运行时身份。

    wheelhouse_path 来自已校验归档；调用方应在后台线程准备依赖。
    """

    target_tag: str
    lock_path: Path
    wheelhouse_path: Path | None = None
    direct_dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    """
    保存安装结果的包版本、wheel 摘要和顶层导入名。

    导入归属用于冲突检查，不赋予插件私有 import 语义。
    """

    name: str
    version: str
    sha256: str
    imports: frozenset[str]
    requirements: tuple[Requirement, ...] = ()
    extras: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PluginDependencyDirectory:
    """
    保存可登记到宿主导入路径的就绪库目录和可观察安装提示。

    文件可以共享，但同一解释器内的模块状态仍共享。
    """

    key: str
    site_path: Path
    packages: tuple[InstalledPackage, ...]
    warnings: tuple[str, ...] = ()


class PluginEnvironmentPool:
    """
    原子准备可复用的依赖安装目录，不下载解释器或启动安装子进程。

    就绪目录不增量覆盖；损坏目录拒绝复用，避免替换已被进程加载的文件。
    """

    def __init__(self, data_path: Path, repository: PluginWheelRepository | None = None) -> None:
        """
        绑定数据目录内的依赖安装池与独立 wheel 缓存。

        :param data_path: 启动器可写的数据根目录
        :param repository: 可供测试注入的 wheel 来源
        """
        self.data_path = Path(data_path)
        self.environments_path = self.data_path / "plugin_deps"
        self.cache_path = self.data_path / "plugin_cache" / "wheels"
        self.repository = repository or PluginWheelRepository(self.cache_path)

    def environment_key(self, spec: PluginEnvironmentSpec) -> str:
        """
        根据安装布局、宿主 ABI、平台与完整锁字节生成共享键。

        :param spec: 当前宿主的完整锁安装输入
        :return: 不含插件名或 SDK 版本的 SHA-256 键
        :raises PluginDependencyError: 目标不是当前宿主或锁无法读取
        """
        if spec.target_tag != plugin_target():
            raise PluginDependencyError("插件依赖目标不兼容当前宿主")
        try:
            raw = spec.lock_path.read_bytes()
        except OSError as exc:
            raise PluginDependencyError("插件依赖锁文件不可读取") from exc
        return self._lock_key(spec.target_tag, raw)

    def _lock_key(self, target_tag: str, raw: bytes) -> str:
        PluginDependencyLock.parse(raw)
        identity = f"wheel-layout-1\0{target_tag}\0{sys.implementation.cache_tag}\0{getattr(sys, 'abiflags', '')}\0"
        return hashlib.sha256(identity.encode("ascii") + raw).hexdigest()

    def _root_path(self, key: str) -> Path:
        """
        约束依赖池和缓存祖先，拒绝符号链接重定向到数据目录外。
        """
        if not isinstance(key, str) or digest_pattern.fullmatch(key) is None:
            raise PluginDependencyError("插件依赖键无效")
        root_path = self.environments_path / key
        for path in (self.environments_path, root_path, self.data_path / "plugin_cache", self.cache_path):
            if path.is_symlink() or not path.resolve().is_relative_to(self.data_path.resolve()):
                raise PluginDependencyError("插件依赖目录超出数据目录")
        return root_path

    def ready_directory(self, key: str) -> PluginDependencyDirectory | None:
        """
        复核已提交目录的清单和文件，不创建环境或进行网络请求。

        :param key: 完整依赖键
        :return: 校验通过的库目录；缺失或损坏时返回 None
        :raises PluginDependencyError: 依赖键无效或目录不在插件安装范围内
        """
        root_path = self._root_path(key)
        try:
            ready = json.loads((root_path / "ready.json").read_bytes())
            inventory_bytes = (root_path / "installed.json").read_bytes()
            if (
                not isinstance(ready, dict)
                or ready.get("key") != key
                or ready.get("inventory_sha256") != hashlib.sha256(inventory_bytes).hexdigest()
            ):
                return None
            inventory = json.loads(inventory_bytes)
            if inventory["target_tag"] != plugin_target():
                return None
            packages: list[InstalledPackage] = []
            for item in inventory["packages"]:
                if (
                    not isinstance(item["name"], str)
                    or not isinstance(item["version"], str)
                    or digest_pattern.fullmatch(item["sha256"]) is None
                    or not isinstance(item["imports"], list)
                    or any(not isinstance(name, str) for name in item["imports"])
                ):
                    return None
                packages.append(
                    InstalledPackage(
                        item["name"],
                        item["version"],
                        item["sha256"],
                        frozenset(item["imports"]),
                        tuple(Requirement(raw) for raw in item["requirements"]),
                        frozenset(item["extras"]),
                    )
                )
            for name, digest in inventory["files"].items():
                file_path = root_path / name
                if (
                    file_path.is_symlink()
                    or not file_path.resolve().is_relative_to(root_path.resolve())
                    or hashlib.sha256(file_path.read_bytes()).hexdigest() != digest
                ):
                    return None
            actual_names = {
                path.relative_to(root_path).as_posix()
                for path in root_path.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.relative_to(root_path).parts
                and path.relative_to(root_path).as_posix() not in {"ready.json", "installed.json"}
            }
            if actual_names != set(inventory["files"]) or any(path.is_symlink() for path in root_path.rglob("*")):
                return None
            warnings = inventory["warnings"]
            if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
                return None
            return PluginDependencyDirectory(key, root_path / "site-packages", tuple(packages), tuple(warnings))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def ensure(self, spec: PluginEnvironmentSpec, *, allow_network: bool = False) -> PluginDependencyDirectory:
        """
        用包内 wheel、缓存或显式联网准备完整锁的库目录。

        :param spec: 当前宿主的完整锁及直接需求
        :param allow_network: 是否允许补齐缺失 wheel
        :return: 可被兼容插件复用的就绪目录
        :raises PluginDependencyError: 校验、闭包、下载或安装失败
        """
        key = self.environment_key(spec)
        root_path = self._root_path(key)
        lock = PluginDependencyLock.parse(spec.lock_path.read_bytes())
        if self._lock_key(spec.target_tag, lock.raw) != key:
            raise PluginDependencyError("插件依赖锁在安装准备期间发生变化")
        direct = lock.validate_direct(list(spec.direct_dependencies))
        with _exclusive_lock(self.environments_path / f"{key}.lock"):
            ready = self.ready_directory(key)
            if ready is not None:
                cached_wheels = tuple(
                    PluginWheel(
                        ready.site_path,
                        package.name,
                        Version(package.version),
                        package.sha256,
                        package.requirements,
                        package.extras,
                        package.imports,
                        (),
                    )
                    for package in ready.packages
                )
                validate_wheel_dependencies(lock, cached_wheels, direct)
                return ready
            if root_path.exists():
                raise PluginDependencyError("已有插件依赖目录损坏，不能覆盖正在使用的文件")
            staging_path = self.environments_path / f".{uuid4().hex[:12]}.staging"
            staging_path.mkdir()
            try:
                wheels = tuple(
                    self.repository.obtain(package, spec.wheelhouse_path, allow_network=allow_network)
                    for package in lock.packages
                )
                validate_wheel_dependencies(lock, wheels, direct)
                (staging_path / "site-packages").mkdir()
                for wheel in wheels:
                    install_wheel(wheel, staging_path)
                    installed_files = tuple(path for path in staging_path.rglob("*") if path.is_file())
                    if len(installed_files) > 100000 or sum(path.stat().st_size for path in installed_files) > 1024**3:
                        raise PluginDependencyError("插件依赖目录超过文件数量或大小上限")
                (staging_path / "requirements.lock").write_bytes(lock.raw)
                inventory = {
                    "target_tag": spec.target_tag,
                    "packages": [
                        {
                            "name": wheel.name,
                            "version": str(wheel.version),
                            "sha256": wheel.sha256,
                            "imports": sorted(wheel.imports),
                            "requirements": [str(requirement) for requirement in wheel.requirements],
                            "extras": sorted(wheel.extras),
                        }
                        for wheel in wheels
                    ],
                    "warnings": [warning for wheel in wheels for warning in wheel.warnings],
                    "files": {
                        path.relative_to(staging_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(staging_path.rglob("*"))
                        if path.is_file()
                    },
                }
                inventory_text = json.dumps(inventory, ensure_ascii=False, sort_keys=True)
                atomic_write_text(staging_path / "installed.json", inventory_text)
                atomic_write_text(
                    staging_path / "ready.json",
                    json.dumps(
                        {"key": key, "inventory_sha256": hashlib.sha256(inventory_text.encode("utf-8")).hexdigest()}
                    ),
                )
                staging_path.replace(root_path)
                ready = self.ready_directory(key)
                if ready is None:
                    raise PluginDependencyError("插件依赖目录提交后校验失败")
                return ready
            except PluginDependencyError:
                raise
            except (OSError, ValueError) as exc:
                raise PluginDependencyError("插件依赖目录安装失败") from exc
            finally:
                if staging_path.exists():
                    shutil.rmtree(staging_path)
