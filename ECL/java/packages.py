# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：查询并校验 Temurin 官方发布包，并有界解压 Java 归档。
#
# 公开接口：
#   - class JavaCatalog — 有界缓存官方版本目录和可信包身份。
#   - class JavaArchive — 将可信安装包解压到专用空临时目录。
# ============================================================
from __future__ import annotations

import hashlib
import stat
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from time import monotonic
from typing import BinaryIO
from urllib.parse import urlparse

import httpx
from pydantic import JsonValue, TypeAdapter

from .models import JavaError, JavaPackage, JavaPolicy


class JavaCatalog:
    """
    将外部元数据转换为可信下载候选，查询失败不会污染已有运行时。

    使用应用注入的元数据客户端；请求和响应大小有上限，缓存最多 64 组。
    """

    json_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

    def __init__(self, client: httpx.Client) -> None:
        """
        注入应用网络客户端，目录服务不关闭由应用拥有的客户端。

        :param client: 应用拥有的 HTTP 客户端
        """
        self._client = client
        self._lock = RLock()
        self._cache: dict[tuple[int, str, str, str], tuple[float, tuple[JavaPackage, ...]]] = {}
        self._releases: tuple[float, tuple[int, ...]] | None = None
        self.recommended_major: int = 0
        self._package_by_id: dict[str, JavaPackage] = {}

    def _get(self, suffix: str, params: dict[str, str] | None = None) -> JsonValue:
        """
        限制请求耗时与 JSON 大小，校验后才将数据送入领域解析。
        """
        try:
            deadline = monotonic() + 30
            with self._client.stream(
                "GET", f"https://api.adoptium.net/v3/{suffix}", params=params, timeout=15
            ) as response:
                if response.status_code == 404:
                    return []
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    if monotonic() >= deadline or len(content) + len(chunk) > 2 * 1024 * 1024:
                        raise ValueError("metadata limit")
                    content.extend(chunk)
                return self.json_adapter.validate_json(bytes(content))
        except (httpx.HTTPError, ValueError) as exc:
            raise JavaError("Java 官方下载目录暂时不可用，请稍后刷新", "JAVA_CATALOG_UNAVAILABLE") from exc

    def releases(self, force: bool = False) -> tuple[int, ...]:
        """
        查询实际存在的正式主版本，不硬编码来源支持能力。

        :param force: 是否绕过五分钟目录缓存
        :return: 已校验的主版本列表
        """
        with self._lock:
            if not force and self._releases and monotonic() - self._releases[0] < 300:
                return self._releases[1]
        payload = self._get("info/available_releases")
        versions = payload.get("available_releases") if isinstance(payload, dict) else None
        if not isinstance(versions, list):
            raise JavaError("Java 官方版本目录格式无效", "JAVA_CATALOG_INVALID")
        releases = tuple(
            sorted(
                {
                    version
                    for version in versions
                    if isinstance(version, int) and not isinstance(version, bool) and 0 < version < 100
                }
            )
        )
        with self._lock:
            self._releases = (monotonic(), releases)
            recommended = payload.get("most_recent_lts")
            self.recommended_major = (
                recommended if isinstance(recommended, int) and recommended in releases else max(releases, default=0)
            )
        return releases

    def packages(self, major_version: int, runtime_kind: str, *, force: bool = False) -> tuple[JavaPackage, ...]:
        """
        查询主机平台的实际 GA 包，空列表明确表示来源暂无适用包。

        :param major_version: 要查询的 Java 主版本
        :param runtime_kind: JRE 或 JDK
        :param force: 是否绕过五分钟缓存
        :return: 经过校验的具体发布包
        """
        platform, architecture = JavaPolicy.host()
        key = (major_version, runtime_kind, platform, architecture)
        with self._lock:
            cached = self._cache.get(key)
            if cached and not force and monotonic() - cached[0] < 300:
                return cached[1]
        payload = self._get(
            f"assets/latest/{major_version}/hotspot",
            {
                "architecture": "aarch64" if architecture == "arm64" else architecture,
                "image_type": runtime_kind.lower(),
                "os": platform,
                "vendor": "eclipse",
                "release_type": "ga",
            },
        )
        if not isinstance(payload, list):
            raise JavaError("Java 官方发布包格式无效", "JAVA_CATALOG_INVALID")
        packages = tuple(self._parse(item, major_version, runtime_kind, platform, architecture) for item in payload)
        with self._lock:
            if len(self._cache) >= 64:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (monotonic(), packages)
            self._package_by_id = {
                package.package_id: package
                for _, cached_packages in self._cache.values()
                for package in cached_packages
            }
        return packages

    @staticmethod
    def _parse(value: JsonValue, major: int, kind: str, platform: str, architecture: str) -> JavaPackage:
        """
        校验官方字段与可信 HTTPS 来源，不接受任意 URL、文件名或校验值。
        """
        try:
            if not isinstance(value, dict):
                raise ValueError("asset object")
            binary = value.get("binary")
            package = binary.get("package") if isinstance(binary, dict) else None
            if not isinstance(package, dict):
                raise ValueError("package object")
            url = package.get("link")
            parsed = urlparse(url) if isinstance(url, str) else None
            if (
                parsed is None
                or parsed.scheme != "https"
                or parsed.hostname != "github.com"
                or not parsed.path.startswith("/adoptium/")
                or parsed.username
                or parsed.password
            ):
                raise ValueError("untrusted source")
            filename = package.get("name")
            if (
                not isinstance(filename, str)
                or "/" in filename
                or "\\" in filename
                or not filename.endswith((".zip", ".tar.gz"))
            ):
                raise ValueError("archive filename")
            checksum = package.get("checksum")
            if not isinstance(checksum, str):
                raise ValueError("checksum")
            return JavaPackage(
                package_id=hashlib.sha256(f"temurin:{checksum}".encode()).hexdigest()[:24],
                release_name=value["release_name"],
                major_version=major,
                runtime_kind=kind,
                platform=platform,
                architecture=architecture,
                filename=filename,
                download_url=url,
                download_bytes=package["size"],
                checksum=checksum.lower(),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise JavaError("Java 下载包元数据校验失败", "JAVA_CATALOG_INVALID") from exc

    def package(self, package_id: str) -> JavaPackage:
        """
        查找后端已验证的包身份，过期缓存需要用户刷新目录。

        :param package_id: 已校验的具体发布包身份
        """
        with self._lock:
            package = self._package_by_id.get(package_id)
            is_fresh = any(
                monotonic() - cached_at < 300 and any(item.package_id == package_id for item in packages)
                for cached_at, packages in self._cache.values()
            )
        if package is None or not is_fresh:
            raise JavaError("Java 下载候选已过期，请刷新目录", "JAVA_PACKAGE_EXPIRED")
        return package


@dataclass(frozen=True, slots=True)
class _Link:
    path: Path
    target: str
    is_hard: bool


class JavaArchive:
    """
    先校验所有成员再写入新目录，链接最后创建且必须指向目录内已有内容。

    不调用库的无条件 extractall，不允许设备、越界、重复成员或提权权限。
    """

    max_files: int = 30000
    max_bytes: int = 2 * 1024 * 1024 * 1024

    @staticmethod
    def _path(root: Path, name: str) -> Path:
        """
        以 POSIX 归档语法校验名称，拒绝驱动器、绝对路径和父目录跳转。
        """
        normalized = name.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if not pure.parts or pure.is_absolute() or ":" in normalized or ".." in pure.parts:
            raise JavaError("Java 安装包包含不安全路径", "JAVA_ARCHIVE_UNSAFE")
        return root.joinpath(*pure.parts)

    @classmethod
    def extract(cls, archive_path: Path, destination: Path, check_cancelled: Callable[[], None]) -> tuple[str, ...]:
        """
        有界解压 ZIP/TAR 并返回安装文件清单，调用方失败时删除专用临时目录。

        :param archive_path: 已校验哈希的 ZIP 或 TAR.GZ
        :param destination: 后端新建的空安装目录
        :param check_cancelled: 在写入前检查取消请求的回调
        :return: 文件与目录的相对路径清单
        :raises JavaError: 归档结构不安全、大小超限或链接无效时抛出
        """
        if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
            raise JavaError("Java 临时安装目录不是空目录", "JAVA_ARCHIVE_UNSAFE")
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        try:
            links = (
                cls._zip(archive_path, root, check_cancelled)
                if archive_path.name.endswith(".zip")
                else cls._tar(archive_path, root, check_cancelled)
            )
            cls._links(root, links, check_cancelled)
            return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            raise JavaError("Java 安装包无法安全解压", "JAVA_ARCHIVE_INVALID") from exc

    @classmethod
    def _validate(cls, root: Path, members: list[tuple[str, int]], compressed_bytes: int) -> None:
        """
        在写入前检查成员数量、总大小、压缩比及规范化后的路径冲突。
        """
        total = sum(size for _, size in members)
        if (
            len(members) > cls.max_files
            or total > cls.max_bytes
            or (total > 64 * 1024 * 1024 and total > max(1, compressed_bytes) * 200)
        ):
            raise JavaError("Java 安装包超过解压限制", "JAVA_ARCHIVE_LIMIT")
        paths = [cls._path(root, name) for name, _ in members]
        if len(paths) != len(set(paths)):
            raise JavaError("Java 安装包包含重复路径", "JAVA_ARCHIVE_UNSAFE")

    @staticmethod
    def _copy(source: BinaryIO, target: Path, size: int, mode: int, check_cancelled: Callable[[], None]) -> None:
        """
        分块写入以响应取消，实际大小不能超过预检声明，执行位不保留提权位。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        copied_bytes = 0
        with target.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                check_cancelled()
                copied_bytes += len(chunk)
                if copied_bytes > size:
                    raise JavaError("Java 安装包条目大小不一致", "JAVA_ARCHIVE_INVALID")
                output.write(chunk)
        if copied_bytes != size:
            raise JavaError("Java 安装包条目未完整读取", "JAVA_ARCHIVE_INVALID")
        if sys.platform != "win32":
            target.chmod(0o755 if mode & 0o111 or target.name in {"java", "javac"} else 0o644)

    @classmethod
    def _zip(cls, archive_path: Path, root: Path, check: Callable[[], None]) -> list[_Link]:
        """
        读取 ZIP 普通文件与有界链接内容，不在解压过程中跟随链接写入。
        """
        links: list[_Link] = []
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            cls._validate(root, [(item.filename, item.file_size) for item in members], archive_path.stat().st_size)
            for member in members:
                check()
                target = cls._path(root, member.filename)
                mode = member.external_attr >> 16
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif stat.S_ISLNK(mode):
                    if member.file_size > 4096:
                        raise JavaError("Java 安装包链接过大", "JAVA_ARCHIVE_UNSAFE")
                    links.append(_Link(target, archive.read(member).decode("utf-8"), False))
                elif stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise JavaError("Java 安装包含特殊设备文件", "JAVA_ARCHIVE_UNSAFE")
                else:
                    with archive.open(member) as source:
                        cls._copy(source, target, member.file_size, mode, check)
        return links

    @classmethod
    def _tar(cls, archive_path: Path, root: Path, check: Callable[[], None]) -> list[_Link]:
        """
        校验 TAR 成员类型，链接延后处理，拒绝 FIFO 与设备文件。
        """
        links: list[_Link] = []
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            cls._validate(root, [(item.name, item.size) for item in members], archive_path.stat().st_size)
            for member in members:
                check()
                target = cls._path(root, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.issym() or member.islnk():
                    links.append(_Link(target, member.linkname, member.islnk()))
                elif member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise JavaError("Java 安装包条目无法读取", "JAVA_ARCHIVE_INVALID")
                    with source:
                        cls._copy(source, target, member.size, member.mode, check)
                else:
                    raise JavaError("Java 安装包含特殊设备文件", "JAVA_ARCHIVE_UNSAFE")
        return links

    @staticmethod
    def _links(root: Path, links: list[_Link], check: Callable[[], None]) -> None:
        """
        拓扑处理内部链接，目标必须已存在并在根目录内，循环和逃逸均拒绝。
        """
        pending = list(links)
        while pending:
            remaining: list[_Link] = []
            for link in pending:
                check()
                raw = link.target.replace("\\", "/")
                if PurePosixPath(raw).is_absolute() or ":" in raw:
                    raise JavaError("Java 安装包含外部链接", "JAVA_ARCHIVE_UNSAFE")
                target = ((root if link.is_hard else link.path.parent) / raw).resolve()
                if not target.is_relative_to(root):
                    raise JavaError("Java 安装包链接越界", "JAVA_ARCHIVE_UNSAFE")
                if not target.exists():
                    remaining.append(link)
                    continue
                link.path.parent.mkdir(parents=True, exist_ok=True)
                if link.is_hard:
                    link.path.hardlink_to(target)
                else:
                    link.path.symlink_to(raw, target_is_directory=target.is_dir())
            if len(remaining) == len(pending):
                raise JavaError("Java 安装包链接循环或目标缺失", "JAVA_ARCHIVE_UNSAFE")
            pending = remaining
