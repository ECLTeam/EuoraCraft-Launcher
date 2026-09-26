# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：预检插件归档的目标依赖，并在激活前准备代码与独立 Python 环境。
#
# 公开接口：
#   - class PluginPreparationError — 插件预检或准备失败。
#   - class PluginPackagePreflight — 不执行代码的归档预检结果。
#   - class PluginPreparedPackage — 已准备但尚未激活的代码和环境。
#   - current_plugin_target() — 返回当前平台的插件锁文件目标标签。
#   - verify_prepared_code(code_path, expected_manifest_hash) — 复核已解包插件代码。
#   - class PluginPackagePreparer — 预检并准备插件归档。
# ============================================================

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ECL.plugins.environment_pool import (
    PluginEnvironmentPool,
    PluginEnvironmentSpec,
    _exclusive_lock,
    safe_component_pattern,
)
from ECL.plugins.package_archive import PluginPackageInfo, extract_plugin_package, inspect_plugin_package
from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimeStore

max_lock_bytes = 2 * 1024**2


class PluginPreparationError(ValueError):
    """
    表示插件包的依赖契约、目标平台或显式确认不满足安装前置条件。

    此异常不会触发旧版插件卸载；调用方不得将失败的准备目录视作已安装。
    """


@dataclass(frozen=True, slots=True)
class PluginPackagePreflight:
    """
    保存不执行插件代码得到的包信息和当前目标依赖摘要。

    来源始终未验证；完整性校验并不能证明作者身份。
    """

    package: PluginPackageInfo
    target_tag: str
    python_dependencies: tuple[str, ...]
    wheel_count: int
    has_target_lock: bool
    unverified_source: bool = True


@dataclass(frozen=True, slots=True)
class PluginPreparedPackage:
    """
    保存尚未激活的插件代码目录与已就绪 Worker Python。

    只有 Worker 验证和原子激活完成后，管理器才能将此目录加入插件发现范围。
    """

    preflight: PluginPackagePreflight
    code_path: Path
    python_path: Path
    environment_key: str


def current_plugin_target() -> str:
    """
    返回与固定 Python 3.12 运行时对应的当前平台锁文件目标。

    标签仅用于选择当前目标的完整锁和 wheels，不替代 uv 的 wheel ABI 校验。

    :return: 形如 `windows-x86_64-cp312` 的稳定目标标签
    :raises PluginPreparationError: 当前系统或架构不在运行时发布矩阵时抛出
    """
    system = platform.system().lower()
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
    if (system, machine) not in {
        ("windows", "x86_64"),
        ("windows", "arm64"),
        ("linux", "x86_64"),
        ("linux", "arm64"),
        ("darwin", "x86_64"),
        ("darwin", "arm64"),
    }:
        raise PluginPreparationError(f"当前平台不支持插件专用运行时: {system}/{machine}")
    return f"{system}-{machine}-cp312"


def _direct_dependencies(raw_dependencies: object) -> tuple[Requirement, ...]:
    if not isinstance(raw_dependencies, list) or any(not isinstance(item, str) for item in raw_dependencies):
        raise PluginPreparationError("pythonDependencies 必须是依赖字符串数组")
    dependencies: list[Requirement] = []
    for item in raw_dependencies:
        try:
            requirement = Requirement(item)
        except InvalidRequirement as exc:
            raise PluginPreparationError(f"Python 直接依赖无效: {item}") from exc
        if requirement.url is not None or requirement.marker is not None:
            raise PluginPreparationError("Python 直接依赖不能使用任意 URL 或环境标记")
        dependencies.append(requirement)
    return tuple(dependencies)


def _parse_lock_line(line: str) -> tuple[str, Version]:
    parts = line.split()
    if len(parts) < 2 or not all(part.startswith("--hash=sha256:") for part in parts[1:]):
        raise PluginPreparationError("Python 依赖锁只允许精确版本和 SHA-256 哈希")
    try:
        requirement = Requirement(parts[0])
    except InvalidRequirement as exc:
        raise PluginPreparationError("Python 依赖锁包含无效包名或版本") from exc
    specifiers = list(requirement.specifier)
    if requirement.url is not None or requirement.extras or requirement.marker or len(specifiers) != 1:
        raise PluginPreparationError("Python 依赖锁必须使用单个精确版本")
    specifier = specifiers[0]
    if specifier.operator != "==" or "*" in specifier.version:
        raise PluginPreparationError("Python 依赖锁必须使用单个精确版本")
    try:
        version = Version(specifier.version)
    except InvalidVersion as exc:
        raise PluginPreparationError("Python 依赖锁版本无效") from exc
    for hash_option in parts[1:]:
        digest = hash_option.removeprefix("--hash=sha256:")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PluginPreparationError("Python 依赖锁哈希无效")
    return canonicalize_name(requirement.name), version


def _validate_lock(lock_bytes: bytes) -> dict[str, Version]:
    """
    v1 锁格式只接受单行精确版本与 SHA-256，避免包内 pip 选项改变索引。
    """
    if len(lock_bytes) > max_lock_bytes:
        raise PluginPreparationError("Python 依赖锁超过大小上限")
    try:
        lines = lock_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PluginPreparationError("Python 依赖锁必须是 UTF-8") from exc
    versions_by_name: dict[str, Version] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        normalized_name, version = _parse_lock_line(stripped)
        if normalized_name in versions_by_name:
            raise PluginPreparationError("Python 依赖锁包含重复包名")
        versions_by_name[normalized_name] = version
    return versions_by_name


def _prepared_file_path(code_path: Path, root_path: Path, relative_path: str) -> Path:
    """
    将已验证清单中的路径约束在代码版本目录内，拒绝路径穿越与符号链接逃逸。
    """
    path_parts = PurePosixPath(relative_path).parts
    if not path_parts or relative_path.startswith("/") or any(part in {"", ".", ".."} for part in path_parts):
        raise PluginPreparationError("已准备插件清单路径越界")
    file_path = code_path.joinpath(*path_parts)
    if not file_path.resolve().is_relative_to(root_path):
        raise PluginPreparationError("已准备插件文件路径越界")
    return file_path


def verify_prepared_code(code_path: Path, expected_manifest_hash: str) -> None:
    """
    逐文件复核已准备代码，避免复用或恢复时加载被修改的目录。

    :param code_path: 数据目录下已解包的插件版本目录
    :param expected_manifest_hash: 预检通过的归档清单 SHA-256
    :raises PluginPreparationError: 目录文件与清单不符或无法读取时抛出
    """
    manifest_path = code_path / "package-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_hash:
            raise PluginPreparationError("已准备插件的归档清单不匹配")
        records = json.loads(manifest_bytes)["files"]
        if not isinstance(records, list):
            raise PluginPreparationError("已准备插件清单文件列表无效")
        expected_paths = {"package-manifest.json"}
        root_path = code_path.resolve()
        for record in records:
            relative_path = record["path"]
            if not isinstance(relative_path, str):
                raise PluginPreparationError("已准备插件清单路径无效")
            file_path = _prepared_file_path(code_path, root_path, relative_path)
            expected_paths.add(relative_path)
            digest = hashlib.sha256()
            size = 0
            with file_path.open("rb") as source:
                while chunk := source.read(1024**2):
                    size += len(chunk)
                    digest.update(chunk)
            if size != record["size"] or digest.hexdigest() != record["sha256"]:
                raise PluginPreparationError(f"已准备插件文件发生变化: {record['path']}")
        entries = tuple(code_path.rglob("*"))
        if any(entry.is_symlink() for entry in entries):
            raise PluginPreparationError("已准备插件目录包含符号链接")
        actual_paths = {entry.relative_to(code_path).as_posix() for entry in entries if entry.is_file()}
        if actual_paths != expected_paths:
            raise PluginPreparationError("已准备插件目录包含额外或缺失文件")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PluginPreparationError):
            raise
        raise PluginPreparationError("已准备插件目录无法复核") from exc


class PluginPackagePreparer:
    """
    将校验通过的归档准备为独立代码目录和可共享的锁定 venv。

    此服务故意不激活或加载插件；Worker 握手、注册项切换和回滚由后续生命周期层负责。
    同步调用可能下载资产，async/IPC 调用方须在线程池中运行。
    """

    def __init__(self, data_path: Path, runtime_asset: PluginRuntimeAsset, target_tag: str | None = None) -> None:
        """
        设置持久化数据目录及由启动器发行时固定哈希的运行时资产。

        :param data_path: 启动器可写的数据目录
        :param runtime_asset: 内嵌清单提供的固定运行时资产
        :param target_tag: 测试或交叉打包时指定的目标；默认取当前平台
        """
        self.data_path = Path(data_path)
        self.runtime_asset = runtime_asset
        self.target_tag = target_tag or current_plugin_target()
        if safe_component_pattern.fullmatch(self.target_tag) is None:
            raise PluginPreparationError("插件目标平台标签无效")
        self.environment_pool = PluginEnvironmentPool(self.data_path)
        self.runtime_store = PluginRuntimeStore(self.data_path)

    def inspect(self, archive_path: Path) -> PluginPackagePreflight:
        """
        校验归档、直接依赖和当前目标锁文件，不执行插件代码。

        :param archive_path: 本地 `.eclplugin` 文件
        :return: 包信息、兼容性、依赖与来源未验证状态
        :raises PluginPackageError: 归档结构或载荷不完整时抛出
        :raises PluginPreparationError: 元数据或目标锁文件无效时抛出
        """
        info = inspect_plugin_package(archive_path)
        lock_name = f"locks/{self.target_tag}.txt"
        wheel_prefix = f"wheels/{self.target_tag}/"
        with zipfile.ZipFile(archive_path) as archive:
            metadata = json.loads(archive.read("plugin.json"))
            if not isinstance(metadata, dict):
                raise PluginPreparationError("plugin.json 必须是 JSON 对象")
            dependencies = _direct_dependencies(metadata.get("pythonDependencies", []))
            names = set(archive.namelist())
            has_target_lock = lock_name in names
            if dependencies and not has_target_lock:
                raise PluginPreparationError(f"插件缺少当前目标依赖锁: {self.target_tag}")
            if has_target_lock:
                lock_bytes = archive.read(lock_name)
                locked_versions = _validate_lock(lock_bytes)
                if dependencies and not any(
                    line.strip() and not line.lstrip().startswith(b"#") for line in lock_bytes.splitlines()
                ):
                    raise PluginPreparationError("插件声明了 Python 依赖，但当前目标锁文件为空")
                for requirement in dependencies:
                    locked_version = locked_versions.get(canonicalize_name(requirement.name))
                    if locked_version is None or locked_version not in requirement.specifier:
                        raise PluginPreparationError(f"直接依赖未被当前目标锁定版本满足: {requirement.name}")
            wheel_names = [name for name in names if name.startswith(wheel_prefix)]
            if any(not name.endswith(".whl") for name in wheel_names):
                raise PluginPreparationError("当前目标离线依赖目录只能包含 wheel")
        return PluginPackagePreflight(
            info,
            self.target_tag,
            tuple(str(requirement) for requirement in dependencies),
            len(wheel_names),
            has_target_lock,
        )

    def prepare(
        self,
        archive_path: Path,
        *,
        confirm_unverified_source: bool,
        allow_network: bool = False,
        offline_runtime_pack: Path | None = None,
    ) -> PluginPreparedPackage:
        """
        显式确认后准备代码、运行时和完整锁定依赖，但不激活插件。

        失败时删除本次创建的代码目录；不会卸载或修改任何活动插件。

        :param archive_path: 再次完整校验的本地插件包
        :param confirm_unverified_source: 用户对无签名来源警告的显式确认
        :param allow_network: 是否允许缺失的锁定 wheel 联网下载
        :param offline_runtime_pack: 可选的匹配哈希运行时离线包
        :return: 可供 Worker 验证与生命周期激活的未激活安装结果
        :raises PluginPreparationError: 未确认来源或依赖契约无效时抛出
        """
        if confirm_unverified_source is not True:
            raise PluginPreparationError("安装无签名插件前必须确认来源未验证")
        preflight = self.inspect(archive_path)
        package = preflight.package
        package_root = self.data_path / "plugin_packages" / package.name
        code_path = package_root / f"pkg-{package.manifest_sha256[:20]}"
        with _exclusive_lock(self.data_path / "plugin_packages" / f"{package.name}.lock"):
            created = False
            if not code_path.exists():
                extracted = extract_plugin_package(archive_path, code_path)
                created = True
                if extracted != package:
                    shutil.rmtree(code_path)
                    raise PluginPreparationError("预检后插件归档发生变化")
            else:
                verify_prepared_code(code_path, package.manifest_sha256)
            try:
                runtime = self.runtime_store.ensure(self.runtime_asset, offline_pack=offline_runtime_pack)
                lock_path = code_path / "locks" / f"{self.target_tag}.txt"
                if not preflight.has_target_lock:
                    empty_lock = self.data_path / "plugin_cache" / "empty.lock"
                    empty_lock.parent.mkdir(parents=True, exist_ok=True)
                    if not empty_lock.exists():
                        partial = empty_lock.with_name(f"empty.{uuid4().hex}.partial")
                        partial.write_bytes(b"")
                        partial.replace(empty_lock)
                    elif empty_lock.stat().st_size != 0:
                        raise PluginPreparationError("共享空依赖锁发生变化")
                    lock_path = empty_lock
                wheelhouse = code_path / "wheels" / self.target_tag
                spec = PluginEnvironmentSpec(
                    runtime_id=self.runtime_asset.runtime_id,
                    target_tag=self.target_tag,
                    sdk_version="1",
                    python_path=runtime.python_path,
                    uv_path=runtime.uv_path,
                    lock_path=lock_path,
                    wheelhouse_path=wheelhouse if wheelhouse.is_dir() else None,
                )
                environment_key = self.environment_pool.environment_key(spec)
                python_path = self.environment_pool.ensure(spec, allow_network=allow_network)
                return PluginPreparedPackage(preflight, code_path, python_path, environment_key)
            except Exception:
                if created:
                    shutil.rmtree(code_path)
                raise
