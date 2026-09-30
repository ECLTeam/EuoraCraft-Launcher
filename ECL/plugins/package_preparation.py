# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：预检插件归档并准备独立安装目录，不下载解释器或执行插件代码。
#
# 公开接口：
#   - class PluginPreparationError — 插件预检或准备失败。
#   - class PluginPackagePreflight — 不执行代码得到的安装预检信息。
#   - class PluginPreparedPackage — 未加载的代码及依赖目录。
#   - current_plugin_target() — 返回实际宿主目标标签。
#   - verify_prepared_code() — 逐文件复核已解包插件代码。
#   - class PluginPackagePreparer — 准备归档及固定锁依赖。
# ============================================================

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ECL.plugins.dependency_lock import PluginDependencyError, PluginDependencyLock, plugin_target
from ECL.plugins.environment_pool import (
    PluginDependencyDirectory,
    PluginEnvironmentPool,
    PluginEnvironmentSpec,
    _exclusive_lock,
)
from ECL.plugins.package_archive import PluginPackageInfo, extract_plugin_package, inspect_plugin_package


class PluginPreparationError(PluginDependencyError):
    """
    表示插件包、依赖或显式确认不满足安装前置条件。

    此异常不修改已提交的旧版插件，也不执行新插件入口。
    """


@dataclass(frozen=True, slots=True)
class PluginPackagePreflight:
    """
    保存无代码执行的归档预检信息，不把完整性误报为作者身份认证。

    依赖就绪仅表示文件已准备，不代表插件已加载或依赖运行时隔离。
    """

    package: PluginPackageInfo
    target_tag: str
    python_dependencies: tuple[str, ...]
    wheel_count: int
    has_target_lock: bool
    unverified_source: bool = True
    dependencies_ready: bool = False


@dataclass(frozen=True, slots=True)
class PluginPreparedPackage:
    """
    保存尚未提交安装指针的代码与可共享依赖目录。

    安装完成后默认重启加载，不在准备阶段创建插件实例。
    """

    preflight: PluginPackagePreflight
    code_path: Path
    dependency_directory: PluginDependencyDirectory | None

    @property
    def environment_key(self) -> str:
        """
        返回完整依赖键；无依赖插件不创建共享空环境。

        :return: 依赖键或空字符串
        """
        return self.dependency_directory.key if self.dependency_directory else ""


def current_plugin_target() -> str:
    """
    根据实际宿主 Python 返回插件锁文件目标，不使用固定专用运行时。

    :return: 当前系统、架构及 CPython 主次版本标签
    """
    return plugin_target()


def verify_prepared_code(code_path: Path, expected_manifest_hash: str) -> None:
    """
    逐文件复核已准备代码，拒绝越界路径、符号链接及额外载荷。

    :param code_path: 数据目录中的代码版本目录
    :param expected_manifest_hash: 原归档清单摘要
    :raises PluginPreparationError: 载荷与清单不符或路径越界
    """
    try:
        if code_path.is_symlink():
            raise PluginPreparationError("已准备插件目录包含符号链接")
        manifest_bytes = (code_path / "package-manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_hash:
            raise PluginPreparationError("已准备插件的归档清单不匹配")
        records = json.loads(manifest_bytes)["files"]
        if not isinstance(records, list):
            raise PluginPreparationError("已准备插件清单文件列表无效")
        expected_paths = {"package-manifest.json"}
        for record in records:
            name = record["path"]
            if (
                not isinstance(name, str)
                or name.startswith("/")
                or "\\" in name
                or ":" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
            ):
                raise PluginPreparationError("已准备插件清单路径越界")
            file_path = code_path.joinpath(*PurePosixPath(name).parts)
            if not file_path.resolve().is_relative_to(code_path.resolve()):
                raise PluginPreparationError("已准备插件文件路径越界")
            expected_paths.add(name)
            size, digest = _file_digest(file_path)
            if size != record["size"] or digest != record["sha256"]:
                raise PluginPreparationError(f"已准备插件文件发生变化: {name}")
        entries = tuple(code_path.rglob("*"))
        if any(entry.is_symlink() for entry in entries):
            raise PluginPreparationError("已准备插件目录包含符号链接")
        actual_paths = {entry.relative_to(code_path).as_posix() for entry in entries if entry.is_file()}
        if actual_paths != expected_paths:
            raise PluginPreparationError("已准备插件目录包含额外或缺失文件")
    except PluginPreparationError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PluginPreparationError("已准备插件目录无法复核") from exc


def _file_digest(file_path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with file_path.open("rb") as source:
        while chunk := source.read(1024**2):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


class PluginPackagePreparer:
    """
    将归档准备为独立代码版本及固定锁依赖目录，不激活插件。

    网络和磁盘操作为同步服务，async 调用方须通过后台线程使用。
    """

    def __init__(self, data_path: Path, target_tag: str | None = None) -> None:
        """
        绑定持久化目录与宿主依赖安装池。

        :param data_path: 启动器数据目录
        :param target_tag: 测试用目标；实际安装必须与宿主一致
        """
        self.data_path = Path(data_path)
        self.target_tag = target_tag or current_plugin_target()
        if self.target_tag != current_plugin_target():
            raise PluginPreparationError("插件依赖目标不兼容当前宿主")
        self.environment_pool = PluginEnvironmentPool(self.data_path)

    def inspect(self, archive_path: Path) -> PluginPackagePreflight:
        """
        校验归档与完整锁，不联网、不解包到宿主环境、不执行插件代码。

        :param archive_path: 本地 .eclplugin 文件
        :return: 当前目标的预检结果
        :raises PluginDependencyError: 锁缺失或直接依赖不满足
        """
        package = inspect_plugin_package(archive_path)
        lock_name = f"locks/{self.target_tag}.txt"
        with zipfile.ZipFile(archive_path) as archive:
            metadata = json.loads(archive.read("plugin.json"))
            names = set(archive.namelist())
            has_lock = lock_name in names
            raw_dependencies = metadata.get("pythonDependencies", [])
            if raw_dependencies and not has_lock:
                raise PluginPreparationError(f"插件缺少当前目标依赖锁: {self.target_tag}")
            lock = PluginDependencyLock.parse(archive.read(lock_name) if has_lock else b"")
            direct = lock.validate_direct(raw_dependencies)
            wheel_names = [name for name in names if name.startswith(f"wheels/{self.target_tag}/")]
            if any(not name.endswith(".whl") for name in wheel_names):
                raise PluginPreparationError("当前目标离线依赖目录只能包含 wheel")
        code_path = self.data_path / "plugin_packages" / package.name / f"pkg-{package.manifest_sha256[:20]}"
        ready = not lock.packages
        lock_path = code_path / lock_name
        if lock.packages and lock_path.is_file():
            spec = PluginEnvironmentSpec(self.target_tag, lock_path)
            ready = self.environment_pool.ready_directory(self.environment_pool.environment_key(spec)) is not None
        return PluginPackagePreflight(
            package,
            self.target_tag,
            tuple(str(item) for item in direct),
            len(wheel_names),
            has_lock,
            dependencies_ready=ready,
        )

    def prepare(
        self,
        archive_path: Path,
        *,
        confirm_unverified_source: bool,
        allow_network: bool = False,
        expected_package: PluginPackageInfo | None = None,
    ) -> PluginPreparedPackage:
        """
        显式确认后准备代码与 wheel 目录，失败时保留旧安装版本。

        :param archive_path: 再次校验的归档
        :param confirm_unverified_source: 是否明确确认无签名来源
        :param allow_network: 是否允许下载缺失 wheel
        :param expected_package: 可选预检归档身份，用于防止确认后替换
        :return: 未加载的代码及依赖安装结果
        :raises PluginDependencyError: 归档、确认、依赖或目录无效
        """
        if confirm_unverified_source is not True:
            raise PluginPreparationError("安装无签名插件前必须确认来源未验证")
        preflight = self.inspect(archive_path)
        package = preflight.package
        if expected_package is not None and package != expected_package:
            raise PluginPreparationError("插件包在预检后发生变化")
        package_root = self.data_path / "plugin_packages" / package.name
        if (
            package_root.parent.is_symlink()
            or package_root.is_symlink()
            or not package_root.resolve().is_relative_to(self.data_path.resolve())
        ):
            raise PluginPreparationError("插件版本目录超出数据目录")
        code_path = package_root / f"pkg-{package.manifest_sha256[:20]}"
        with _exclusive_lock(package_root.parent / f"{package.name}.lock"):
            created = not code_path.exists()
            if created:
                extracted = extract_plugin_package(archive_path, code_path)
                if extracted != package:
                    shutil.rmtree(code_path)
                    raise PluginPreparationError("预检后插件归档发生变化")
            try:
                verify_prepared_code(code_path, package.manifest_sha256)
                dependency_directory = self.prepare_dependencies(
                    code_path, preflight.python_dependencies, allow_network=allow_network
                )
                return PluginPreparedPackage(preflight, code_path, dependency_directory)
            except Exception:
                if created:
                    shutil.rmtree(code_path)
                raise

    def prepare_dependencies(
        self, code_path: Path, direct: tuple[str, ...], *, allow_network: bool = False
    ) -> PluginDependencyDirectory | None:
        """
        从已复核代码目录准备依赖，供旧安装记录迁移复用。

        :param code_path: 调用方已验证的归档代码目录
        :param direct: 原 plugin.json 的直接需求
        :param allow_network: 是否明确允许联网
        :return: 有依赖时返回安装目录，无依赖返回 None
        :raises PluginDependencyError: 缺少锁、wheel 或完整依赖不满足
        """
        lock_path = code_path / "locks" / f"{self.target_tag}.txt"
        lock = PluginDependencyLock.parse(lock_path.read_bytes() if lock_path.is_file() else b"")
        lock.validate_direct(list(direct))
        if not lock.packages:
            return None
        wheelhouse_path = code_path / "wheels" / self.target_tag
        return self.environment_pool.ensure(
            PluginEnvironmentSpec(
                self.target_tag, lock_path, wheelhouse_path if wheelhouse_path.is_dir() else None, direct
            ),
            allow_network=allow_network,
        )
