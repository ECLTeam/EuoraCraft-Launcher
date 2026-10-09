# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：取得锁定 wheel、检查安装兼容性并通过 PyPA installer 安装到暂存目录。
#
# 公开接口：
#   - class PluginWheel — 已校验 wheel 的依赖与导入归属。
#   - class PluginWheelRepository — 使用包内 wheel、缓存及固定 PyPI 索引。
#   - inspect_wheel() — 校验 wheel 内容与锁的匹配关系。
#   - validate_wheel_dependencies() — 校验完整锁的传递依赖及 extras。
#   - install_wheel() — 安装 wheel，不生成 Python 命令行包装器。
# ============================================================

from __future__ import annotations

import configparser
import hashlib
import io
import json
import stat
import sys
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.records import RecordEntry
from installer.sources import WheelFile
from installer.utils import Scheme
from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import parse_tag, sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from ECL.plugins.dependency_lock import LockedPackage, PluginDependencyError, PluginDependencyLock

max_wheel_bytes = 256 * 1024**2
max_index_bytes = 8 * 1024**2
max_wheel_files = 50000


@dataclass(frozen=True, slots=True)
class PluginWheel:
    """
    保存经过哈希与元数据校验的 wheel，不在检查时导入其代码。

    warnings 表明脚本包装器未提供，不代表额外执行能力。
    """

    path: Path
    name: str
    version: Version
    sha256: str
    requirements: tuple[Requirement, ...]
    extras: frozenset[str]
    imports: frozenset[str]
    warnings: tuple[str, ...]


def _file_digest(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_wheel_name(name: str) -> PurePosixPath:
    """
    拒绝 wheel 与索引文件名中的路径穿越、Windows 驱动器及特殊分隔符。

    路径检查独立于目标文件系统，不能把 POSIX 名称直接交给 Windows 解释。
    """
    relative = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or relative.is_absolute()
        or any(part in {"", ".", ".."} or part.rstrip(". ") != part for part in name.rstrip("/").split("/"))
        or any(
            part.casefold().split(".")[0]
            in {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)}, *{f"lpt{i}" for i in range(1, 10)}}
            for part in relative.parts
        )
    ):
        raise PluginDependencyError("wheel 文件路径无效")
    return relative


def _wheel_import_name(relative: PurePosixPath) -> str | None:
    """
    从安装布局推导顶层导入名，并拒绝无法正确支持的初始化机制。
    """
    parts = relative.parts
    if parts[0].endswith(".data"):
        if len(parts) < 3 or parts[1] not in {"purelib", "platlib", "data", "headers"}:
            raise PluginDependencyError("wheel 需要不支持的脚本或安装前缀布局")
        if parts[1] not in {"purelib", "platlib"}:
            return None
        parts = parts[2:]
    if parts[0].endswith(".dist-info"):
        return None
    root = parts[0].split(".")[0]
    if relative.suffix == ".pth" or root in {"sitecustomize", "usercustomize"}:
        raise PluginDependencyError("wheel 需要不支持的 .pth 或 site 初始化")
    if len(parts) == 1 and relative.suffix not in {".py", ".pyi", ".pyd", ".so"}:
        return None
    return root if root.isidentifier() else None


def _wheel_layout(archive: zipfile.ZipFile) -> tuple[str, frozenset[str]]:
    """
    验证 wheel 的有界路径布局，不把平台特定路径交给安装库后再检查。
    """
    entries = archive.infolist()
    if len(entries) > max_wheel_files or sum(entry.file_size for entry in entries) > max_wheel_bytes:
        raise PluginDependencyError("wheel 解包超过大小或数量上限")
    seen: set[str] = set()
    dist_infos: set[str] = set()
    imports: set[str] = set()
    for entry in entries:
        relative = _safe_wheel_name(entry.filename)
        folded = entry.filename.rstrip("/").casefold()
        if folded in seen or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise PluginDependencyError("wheel 包含重复路径、符号链接或加密内容")
        seen.add(folded)
        if relative.parts[0].endswith(".dist-info"):
            dist_infos.add(relative.parts[0])
        if not entry.is_dir():
            import_name = _wheel_import_name(relative)
            if import_name:
                imports.add(import_name)
    if len(dist_infos) != 1:
        raise PluginDependencyError("wheel 必须包含唯一的 dist-info")
    return next(iter(dist_infos)), frozenset(imports)


def _wheel_metadata(
    archive: zipfile.ZipFile, dist_info: str, name: str, version: Version
) -> tuple[tuple[Requirement, ...], frozenset[str], tuple[str, ...]]:
    """
    校验内部包身份、Python 需求与传递依赖，显式记录不支持的脚本包装器。
    """
    metadata = BytesParser().parsebytes(archive.read(f"{dist_info}/METADATA"))
    if canonicalize_name(metadata.get("Name", "")) != name or Version(metadata.get("Version", "")) != version:
        raise PluginDependencyError("wheel 内部元数据与文件名不一致")
    python_specifier = SpecifierSet(metadata.get("Requires-Python", ""))
    if Version(".".join(str(part) for part in sys.version_info[:3])) not in python_specifier:
        raise PluginDependencyError("wheel 的 Requires-Python 不兼容当前宿主")
    requirements = tuple(Requirement(item) for item in metadata.get_all("Requires-Dist", []))
    if any(requirement.url for requirement in requirements):
        raise PluginDependencyError("wheel 依赖不能使用任意 URL")
    warnings: list[str] = []
    entry_points_name = f"{dist_info}/entry_points.txt"
    if entry_points_name in archive.namelist():
        entry_points = configparser.ConfigParser(interpolation=None)
        entry_points.read_string(archive.read(entry_points_name).decode("utf-8"))
        if entry_points.has_section("console_scripts") or entry_points.has_section("gui_scripts"):
            warnings.append(f"{name} 的命令行/GUI 脚本包装器未安装，仅支持库导入")
    return (
        requirements,
        frozenset(canonicalize_name(item) for item in metadata.get_all("Provides-Extra", [])),
        tuple(warnings),
    )


def inspect_wheel(wheel_path: Path, package: LockedPackage) -> PluginWheel:
    """
    校验 wheel 的锁内摘要、ABI、安装内容、RECORD 与依赖元数据。

    :param wheel_path: 本地普通 wheel 文件
    :param package: 完整锁中的精确包版本与允许摘要
    :return: 供闭包、冲突检查和安装使用的 wheel 模型
    :raises PluginDependencyError: wheel 不兼容、损坏或包含不支持的安装行为
    """
    try:
        if wheel_path.is_symlink() or not wheel_path.is_file() or wheel_path.stat().st_size > max_wheel_bytes:
            raise PluginDependencyError("wheel 文件无效或超过大小上限")
        name, version, _build, tags = parse_wheel_filename(wheel_path.name)
        digest = _file_digest(wheel_path)
        if name != package.name or version != package.version or digest not in package.hashes:
            raise PluginDependencyError(f"wheel 与依赖锁的名称、版本或哈希不匹配: {package.name}")
        if not tags.intersection(sys_tags()):
            raise PluginDependencyError(f"wheel 不兼容当前宿主 ABI: {wheel_path.name}")
        with zipfile.ZipFile(wheel_path) as archive:
            dist_info, imports = _wheel_layout(archive)
            requirements, extras, warnings = _wheel_metadata(archive, dist_info, name, version)
            wheel_metadata = BytesParser().parsebytes(archive.read(f"{dist_info}/WHEEL"))
            internal_tags = {tag for raw_tag in wheel_metadata.get_all("Tag", []) for tag in parse_tag(raw_tag)}
            if internal_tags != tags or not wheel_metadata.get("Wheel-Version", "").startswith("1."):
                raise PluginDependencyError("wheel 内部 ABI 标签或格式版本无效")
        with WheelFile.open(wheel_path) as source:
            source.validate_record(validate_contents=True)
        return PluginWheel(
            wheel_path,
            name,
            version,
            digest,
            requirements,
            extras,
            imports,
            warnings,
        )
    except PluginDependencyError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        InvalidWheelFilename,
        InvalidVersion,
        InvalidSpecifier,
        InvalidRequirement,
        configparser.Error,
    ) as exc:
        raise PluginDependencyError(f"wheel 校验失败: {wheel_path.name}") from exc


def validate_wheel_dependencies(
    lock: PluginDependencyLock, wheels: tuple[PluginWheel, ...], direct: tuple[Requirement, ...]
) -> None:
    """
    验证完整锁覆盖传递依赖及激活的 extras，不重新选择版本。

    :param lock: 已解析的完整依赖锁
    :param wheels: 每个锁定包对应的已校验 wheel
    :param direct: plugin.json 的直接需求
    :raises PluginDependencyError: 缺少传递依赖、extras 或锁定版本不满足约束
    """
    wheels_by_name = {wheel.name: wheel for wheel in wheels}
    if len(wheels_by_name) != len(wheels) or set(wheels_by_name) != {package.name for package in lock.packages}:
        raise PluginDependencyError("已安装依赖集合与完整锁不匹配")
    for package in lock.packages:
        wheel = wheels_by_name[package.name]
        if wheel.version != package.version or wheel.sha256 not in package.hashes:
            raise PluginDependencyError("已安装依赖版本或 wheel 摘要与完整锁不匹配")
    extras_by_name = {package.name: set() for package in lock.packages}
    for requirement in direct:
        extras_by_name[canonicalize_name(requirement.name)].update(
            canonicalize_name(extra) for extra in requirement.extras
        )
    changed = True
    while changed:
        changed = False
        for wheel in wheels:
            requested_extras = extras_by_name[wheel.name]
            if not requested_extras <= wheel.extras:
                raise PluginDependencyError(f"依赖未提供所需 extras: {wheel.name}")
            for requirement in wheel.requirements:
                if requirement.marker and not any(
                    requirement.marker.evaluate({**default_environment(), "extra": extra})
                    for extra in {"", *requested_extras}
                ):
                    continue
                name = canonicalize_name(requirement.name)
                dependency = wheels_by_name.get(name)
                if dependency is None or dependency.version not in requirement.specifier:
                    raise PluginDependencyError(f"完整依赖锁未满足传递依赖: {wheel.name} -> {requirement}")
                old_extras = len(extras_by_name[name])
                extras_by_name[name].update(canonicalize_name(extra) for extra in requirement.extras)
                changed |= old_extras != len(extras_by_name[name])


class _LibraryDestination(SchemeDictionaryDestination):
    def write_script(self, name: str, module: str, attr: str, section: Literal["console", "gui"]) -> RecordEntry:
        """
        记录脚本不支持状态而不生成指向冻结启动器的伪 Python 包装器。

        安装结果中的 warnings 单独显示给用户；此处只处理库返回的提示。
        """
        return self.write_to_fs(
            Scheme("data"),
            f"unsupported-scripts/{name}.txt",
            io.BytesIO(b"Command-line wrappers are not supported by the plugin library installer.\n"),
            False,
        )

    def write_to_fs(self, scheme: Scheme, path: str, stream: BinaryIO, is_executable: bool) -> RecordEntry:
        """
        约束所有安装路径并拒绝多个 wheels 覆盖同一文件。

        installer 负责写入文件和 RECORD；此处检查目标路径是否在插件目录内。
        """
        relative = _safe_wheel_name(path)
        target_path = Path(self.scheme_dict[scheme]).joinpath(*relative.parts)
        root_path = Path(self.scheme_dict[scheme]).resolve()
        if not target_path.resolve().is_relative_to(root_path) or target_path.exists():
            raise PluginDependencyError("wheel 安装路径越界或文件互相覆盖")
        return super().write_to_fs(scheme, path, stream, is_executable)


def install_wheel(wheel: PluginWheel, target_path: Path) -> None:
    """
    通过 installer 公开接口安装已校验 wheel 到同一暂存根目录。

    :param wheel: 经过完整校验的本地 wheel
    :param target_path: 尚未发布为就绪状态的安装目录
    :raises PluginDependencyError: 安装失败、文件冲突或路径越界
    """
    destination = _LibraryDestination(
        {
            "purelib": str(target_path / "site-packages"),
            "platlib": str(target_path / "site-packages"),
            "data": str(target_path / "data"),
            "headers": str(target_path / "headers"),
            "scripts": str(target_path / "unsupported-scripts"),
        },
        interpreter=sys.executable,
        script_kind="posix",
        bytecode_optimization_levels=(),
    )
    try:
        with WheelFile.open(wheel.path) as source:
            install(source, destination, {"INSTALLER": b"EuoraCraft Launcher"})
    except PluginDependencyError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise PluginDependencyError(f"wheel 安装失败: {wheel.name}") from exc


class PluginWheelRepository:
    """
    先使用包内文件和内容缓存，只在明确允许时访问固定的 PyPI 索引。

    不读取插件指定的 URL 或 pip 配置，不启动安装子进程。
    """

    def __init__(self, cache_path: Path, client: httpx.Client | None = None) -> None:
        """
        设置内容缓存及可供测试注入的同步 HTTP 客户端。

        :param cache_path: 数据目录内专用 wheel 缓存
        :param client: 调用方管理的客户端；缺省时每次联网操作自行关闭客户端
        """
        self.cache_path = cache_path
        self.client = client

    def obtain(self, package: LockedPackage, wheelhouse_path: Path | None, *, allow_network: bool) -> PluginWheel:
        """
        获取锁中允许的宿主兼容 wheel 并验证完整内容。

        :param package: 锁定包及允许摘要
        :param wheelhouse_path: 可选包内 wheels 目录
        :param allow_network: 是否允许缺失时访问 PyPI
        :return: 已校验的本地 wheel
        :raises PluginDependencyError: 无可用 wheel、联网失败或哈希不匹配
        """
        candidates = sorted(wheelhouse_path.glob("*.whl")) if wheelhouse_path else []
        for digest in sorted(package.hashes):
            candidates.extend(sorted(self._cache_folder(digest).glob("*.whl")))
        for wheel_path in candidates:
            try:
                name, version, _build, tags = parse_wheel_filename(wheel_path.name)
            except InvalidWheelFilename:
                continue
            if name == package.name and version == package.version and tags.intersection(sys_tags()):
                try:
                    wheel = inspect_wheel(wheel_path, package)
                except PluginDependencyError:
                    if wheelhouse_path and wheel_path.parent == wheelhouse_path:
                        raise
                    continue
                self._cache(wheel)
                return wheel
        if not allow_network:
            raise PluginDependencyError(f"缺少离线 wheel，未允许联网: {package.name}=={package.version}")
        if self.client is not None:
            return self._download(self.client, package)
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            return self._download(client, package)

    def _cache(self, wheel: PluginWheel) -> None:
        """
        将校验通过的 wheel 原子写入内容缓存，不在导入路径中链接缓存文件。
        """
        target_path = self._cache_folder(wheel.sha256) / wheel.path.name
        if target_path.resolve() == wheel.path.resolve():
            return
        target_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = target_path.with_name(f".{uuid4().hex[:12]}.partial")
        try:
            with wheel.path.open("rb") as source, partial_path.open("xb") as target:
                while chunk := source.read(1024**2):
                    target.write(chunk)
            if _file_digest(partial_path) != wheel.sha256:
                raise PluginDependencyError("wheel 在写入缓存期间发生变化")
            partial_path.replace(target_path)
        finally:
            partial_path.unlink(missing_ok=True)

    def _fetch(self, client: httpx.Client, url: str, limit: int, target: BinaryIO) -> None:
        """
        网络传输失败最多重试一次，丢弃未完成响应后从头获取。
        """
        offset = target.tell()
        for attempt in range(2):
            try:
                self._fetch_once(client, url, limit, target)
                return
            except httpx.TransportError:
                target.seek(offset)
                target.truncate()
                if attempt:
                    raise

    def _cache_folder(self, digest: str) -> Path:
        """
        约束内容缓存祖先和哈希目录，拒绝写入通过链接重定向的路径。
        """
        folder_path = self.cache_path / digest
        if (
            self.cache_path.is_symlink()
            or folder_path.is_symlink()
            or not folder_path.resolve().is_relative_to(self.cache_path.resolve())
        ):
            raise PluginDependencyError("wheel 缓存路径越界或包含符号链接")
        return folder_path

    def _fetch_once(self, client: httpx.Client, url: str, limit: int, target: BinaryIO) -> None:
        """
        限定 HTTPS 来源、重定向次数、超时和响应大小。
        """
        for _redirect in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"pypi.org", "files.pythonhosted.org"}
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
            ):
                raise PluginDependencyError("wheel 下载来源无效")
            with client.stream(
                "GET",
                url,
                headers={"Accept": "application/vnd.pypi.simple.v1+json"},
                timeout=30,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise PluginDependencyError("wheel 下载响应超过大小上限")
                    target.write(chunk)
                return
        raise PluginDependencyError("wheel 下载重定向次数超过上限")

    def _download(self, client: httpx.Client, package: LockedPackage) -> PluginWheel:
        """
        从标准索引选择精确版本及锁内摘要，不自行求解其他依赖。
        """
        try:
            payload_stream = io.BytesIO()
            self._fetch(client, f"https://pypi.org/simple/{package.name}/", max_index_bytes, payload_stream)
            payload = json.loads(payload_stream.getvalue())
            if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
                raise PluginDependencyError("PyPI 索引返回结构无效")
            for item in payload["files"]:
                if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                    raise PluginDependencyError("PyPI 索引文件信息无效")
                filename = item["filename"]
                if not filename.endswith(".whl"):
                    continue
                if len(_safe_wheel_name(filename).parts) != 1:
                    raise PluginDependencyError("PyPI wheel 文件名越界")
                name, version, _build, tags = parse_wheel_filename(filename)
                hashes = item.get("hashes")
                if (
                    name != package.name
                    or version != package.version
                    or not tags.intersection(sys_tags())
                    or not isinstance(hashes, dict)
                    or hashes.get("sha256") not in package.hashes
                ):
                    continue
                download_url = item.get("url")
                if not isinstance(download_url, str):
                    raise PluginDependencyError("PyPI wheel 下载地址无效")
                digest = hashes["sha256"]
                folder_path = self._cache_folder(digest)
                folder_path.mkdir(parents=True, exist_ok=True)
                partial_path = folder_path / f".{uuid4().hex}.partial"
                try:
                    with partial_path.open("xb") as stream:
                        self._fetch(client, urljoin("https://pypi.org/", download_url), max_wheel_bytes, stream)
                    if _file_digest(partial_path) != digest:
                        raise PluginDependencyError("下载 wheel 哈希与依赖锁不匹配")
                    target_path = folder_path / filename
                    partial_path.replace(target_path)
                    return inspect_wheel(target_path, package)
                finally:
                    partial_path.unlink(missing_ok=True)
            raise PluginDependencyError(f"PyPI 缺少锁定且兼容的 wheel: {package.name}=={package.version}")
        except PluginDependencyError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            raise PluginDependencyError(f"联网取得 wheel 失败: {package.name}") from exc
