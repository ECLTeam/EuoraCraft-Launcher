# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：统一网络权限的精确站点身份、URL 校验与公网地址准入。
#
# 公开接口：
#   - class NetworkOrigin — 精确协议、DNS 域名和端口组成的不可变站点。
# ============================================================

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Self
from urllib.parse import urlsplit

import httpx
import idna


@dataclass(frozen=True, slots=True)
class NetworkOrigin:
    """
    表示一个精确站点，许可不向父域、子域或其他协议端口传播。
    """

    scheme: str
    host: str
    port: int

    @property
    def reference(self) -> str:
        """
        返回权限声明与授权存储共同使用的规范站点键。

        :return: 显式包含端口的 ASCII 站点键
        """
        return f"{self.scheme}:{self.host}:{self.port}"

    @classmethod
    def parse(cls, reference: str) -> Self:
        """
        校验声明或决定中的规范站点键，不接受 URL 或隐式默认端口。

        :param reference: 清单或决定中的资源引用
        :return: 精确站点身份
        :raises ValueError: 大小写、域名或端口不是规范形式时抛出
        """
        parts = reference.split(":")
        if len(parts) != 3 or len(reference) > 128:
            raise ValueError("网络站点键无效")
        scheme, host, port = parts
        if scheme not in {"http", "https"} or not port.isascii() or not port.isdecimal():
            raise ValueError("网络协议或端口无效")
        cls._validate_host(host)
        number = int(port)
        if not 1 <= number <= 65535 or str(number) != port:
            raise ValueError("网络端口必须使用规范十进制")
        return cls(scheme, host, number)

    @classmethod
    def from_url(cls, value: str) -> tuple[Self, str]:
        """
        先拒绝有歧义的输入，再经公开 URL 解析器规范化实际请求。

        :param value: 插件或重定向提供的 URL
        :return: 精确站点与可传给 HTTPcore 的规范 URL
        :raises ValueError: URL 不是有界 HTTP DNS 站点时抛出
        """
        if len(value.encode("utf-8")) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError("网络 URL 无效")
        if "\\" in value or "#" in value:
            raise ValueError("网络 URL 存在不支持的组成")
        try:
            authority = urlsplit(value).netloc
            if not authority or any(char in authority for char in "@%[]"):
                raise ValueError("网络 URL 站点无效")
            if ":" in authority and re.fullmatch(r"[0-9]+", authority.rsplit(":", 1)[1]) is None:
                raise ValueError("网络 URL 端口无效")
            url = httpx.URL(value)
            host = url.raw_host.decode("ascii")
            port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
            origin = cls.parse(f"{url.scheme}:{host}:{port}")
            normalized = str(url.copy_with(scheme=origin.scheme, host=origin.host, port=origin.port))
        except (httpx.InvalidURL, UnicodeError) as exc:
            raise ValueError("网络 URL 无效") from exc
        if len(normalized.encode("utf-8")) > 4096:
            raise ValueError("网络 URL 超过上限")
        return origin, normalized

    @staticmethod
    def _validate_host(host: str) -> None:
        """
        拒绝 IP、单标签和非规范 DNS 键，punycode 同样必须通过 IDNA 校验。
        """
        labels = host.split(".")
        if len(labels) < 2 or len(host) > 253 or not host.isascii() or host != host.lower():
            raise ValueError("网络站点必须为规范 DNS 域名")
        if not re.search(r"[a-z]", labels[-1]) or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels
        ):
            raise ValueError("网络 DNS 标签无效")
        try:
            ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("网络站点不能是 IP 字面量")
        try:
            if idna.encode(host).decode("ascii") != host:
                raise ValueError("网络 DNS 名称不是规范形式")
        except idna.IDNAError as exc:
            raise ValueError("网络 DNS 名称无效") from exc
