# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：查询并校验 Temurin 官方发布包，仅返回当前平台实际存在的候选。
#
# 公开接口：
#   - class JavaCatalog — 有界缓存官方版本目录和可信包身份。
# ============================================================
from __future__ import annotations

import hashlib
from threading import RLock
from time import monotonic
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
