# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：模组源策略：Modrinth 与 CurseForge 的官方源与 MCIM 镜像源域名裁决、文件地址重写与双向回退。
#
# 公开接口：
#   - class ModSourcePolicy — 模组源允许值、平台 API 基址与可重写的文件域名。
#   - class ModSourceRequestPolicy — 按当前模组源执行远端请求，远端失败时切换到另一源。
#   - normalize_mod_source(value) -> str — 校验模组源名称，非法值回退官方源。
#   - alternate_mod_source(source) -> str — 返回另一模组源名称。
#   - display_mod_source(source) -> str — 返回面向用户的模组源显示名。
#   - rewrite_mod_file_url(url, source) -> str — 按模组源重写文件与图标地址。
#   - mod_user_agent() -> str — 返回 MCIM 接入登记要求的 User-Agent。
# ============================================================

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from logging import Logger
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx

from ECL.common.version import __version__

result_type = TypeVar("result_type")


class _InvalidModSourceResponseError(ValueError):
    """
    标记远端响应缺少模组源后续读取所需的基本结构。
    """


class ModSourcePolicy:
    """
    模组源的允许值、平台 API 基址与可重写文件域名。

    MCIM 是镜像 Modrinth 与 CurseForge 的第三方服务，接口路径与官方一致，
    仅域名不同。文件与图标地址在官方与镜像之间按主机名精确替换，因此
    不可替换的主机（例如 ``mediafilez.forgecdn.net``）只要不出现在映射表中
    就不会被改写。
    """

    sources = ("official", "mcim")
    default_source = "official"
    display_names = {"official": "官方", "mcim": "MCIM"}
    api_bases = {
        "modrinth": {
            "official": "https://api.modrinth.com/v2",
            "mcim": "https://mod.mcimirror.top/modrinth/v2",
        },
        "curseforge": {
            "official": "https://api.curseforge.com/v1",
            "mcim": "https://mod.mcimirror.top/curseforge/v1",
        },
    }
    # 仅列出 MCIM 已提供镜像的主机；未列出的主机（含 mediafilez.forgecdn.net）保持原样。
    file_host_rewrites = {
        "cdn.modrinth.com": "mod.mcimirror.top",
        "edge.forgecdn.net": "mod.mcimirror.top",
        "media.forgecdn.net": "mod.mcimirror.top",
    }
    user_agent_name = "EuoraCraft-Launcher"


def normalize_mod_source(value: Any) -> str:
    """
    校验模组源名称，非法或缺失时回退官方源。

    读取配置时使用本函数而非抛错，避免用户配置被外部改写后启动器无法工作；
    写入边界仍由请求模型限制为允许值。

    :param value: 配置或调用方传入的模组源名称
    :return: ``official`` 或 ``mcim``
    """
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in ModSourcePolicy.sources else ModSourcePolicy.default_source


def alternate_mod_source(source: str) -> str:
    """
    返回另一模组源名称，用于双向回退。

    :param source: 已校验或未校验的模组源名称
    :return: 与入参不同的模组源名称
    """
    return "mcim" if normalize_mod_source(source) == "official" else "official"


def display_mod_source(source: str) -> str:
    """
    返回面向用户的模组源显示名，供日志使用。

    :param source: 模组源名称
    :return: 中文显示名
    """
    normalized = normalize_mod_source(source)
    return ModSourcePolicy.display_names.get(normalized, normalized)


def mod_api_base(source: str, platform: str) -> str:
    """
    返回指定平台在当前模组源下的 API 基址。

    :param source: 模组源名称
    :param platform: 平台名称，支持 ``modrinth`` 与 ``curseforge``
    :return: 不带尾斜杠的 API 基址
    :raises KeyError: 平台名称不受支持时抛出
    """
    bases = ModSourcePolicy.api_bases[platform]
    return bases[normalize_mod_source(source)]


def rewrite_mod_file_url(url: str, source: str) -> str:
    """
    按模组源重写文件与图标地址的主机名。

    仅当模组源为 MCIM 且主机名出现在可重写映射中时替换主机，路径、查询串
    与片段原样保留；其他主机（含不可替换的 ``mediafilez.forgecdn.net``）与
    非 HTTP 地址一律返回原文。

    :param url: 官方平台返回的文件或图标地址
    :param source: 模组源名称
    :return: 重写后的地址，无需重写时返回原地址
    """
    if not url or normalize_mod_source(source) != "mcim":
        return url
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    replacement = ModSourcePolicy.file_host_rewrites.get((parsed.hostname or "").casefold())
    if replacement is None:
        return url
    return urlunsplit((parsed.scheme, replacement, parsed.path, parsed.query, parsed.fragment))


def mod_user_agent() -> str:
    """
    返回模组平台请求使用的 User-Agent。

    MCIM 要求接入方登记 ``名称/版本号`` 格式的 UA，并使用该 UA 发起请求，
    不得使用空 UA 或 HTTP 库默认 UA。

    :return: 形如 ``EuoraCraft-Launcher/0.0.1-alpha`` 的 UA
    """
    return f"{ModSourcePolicy.user_agent_name}/{__version__}"


class ModSourceRequestPolicy:
    """
    按当前模组源执行远端请求，并在远端失败时切换到另一源。

    首选源由延迟提供器读取，使设置界面切换模组源后无需重建服务即可生效。
    回退只发生在网络请求或远端响应解析失败时，不重复本地安装步骤；
    两源均失败时通过异常链保留首选源的原始错误。
    """

    retryable_errors = (
        httpx.HTTPError,
        json.JSONDecodeError,
        UnicodeError,
        KeyError,
        IndexError,
        TypeError,
        _InvalidModSourceResponseError,
    )

    def __init__(self, source_provider: Callable[[], str], logger: Logger) -> None:
        """
        保存模组源提供器与日志器。

        :param source_provider: 每次请求读取当前模组源的延迟提供器
        :param logger: 记录换源信息的日志器
        """
        self._source_provider = source_provider
        self._logger = logger

    def source(self) -> str:
        """
        返回当前生效的模组源名称。

        :return: ``official`` 或 ``mcim``
        """
        return normalize_mod_source(self._source_provider())

    def request(
        self,
        name: str,
        operation: Callable[[str], result_type],
        *,
        can_fallback: bool = True,
        is_valid: Callable[[result_type], bool] | None = None,
    ) -> result_type:
        """
        以当前模组源执行操作，远端失败时改用另一源重试一次。

        :param name: 面向日志的操作名称
        :param operation: 接收模组源名称并返回结果的远端操作
        :param can_fallback: 是否允许换源重试
        :param is_valid: 可选的响应结构校验，校验失败按远端失败处理
        :return: 首选源或备用源的成功结果
        :raises Exception: 两源均失败时抛出备用源异常，并以首选源异常为原因
        """

        def checked(source: str) -> result_type:
            response = operation(source)
            if is_valid is not None and not is_valid(response):
                raise _InvalidModSourceResponseError(f"{name}响应结构无效")
            return response

        source = self.source()
        try:
            return checked(source)
        except self.retryable_errors as primary_error:
            if not can_fallback:
                raise
            alternate = alternate_mod_source(source)
            self._logger.warning(
                "%s 从 %s 获取失败，尝试 %s",
                name,
                display_mod_source(source),
                display_mod_source(alternate),
            )
            try:
                return checked(alternate)
            except self.retryable_errors as alternate_error:
                raise alternate_error from primary_error


class ModSourceAware:
    """
    为持有模组源状态的服务提供统一的源读取与请求入口。

    独立构造时回退官方源，避免资源协调器为了读取模组源而依赖完整游戏状态。
    """

    _mod_sources: ModSourceRequestPolicy | None = None

    def _mod_source_policy(self) -> ModSourceRequestPolicy:
        """
        返回模组源请求策略，未注入时按官方源惰性创建。

        :return: 本次服务实例共享的模组源请求策略
        """
        policy = self._mod_sources
        if policy is None:
            policy = ModSourceRequestPolicy(lambda: ModSourcePolicy.default_source, logging.getLogger(__name__))
            self._mod_sources = policy
        return policy

    def mod_source(self) -> str:
        """
        返回当前生效的模组源名称。

        模组源独立于游戏本体的下载源，仅作用于 Modrinth 与 CurseForge 的
        搜索、详情、版本、依赖、文件与图标请求。

        :return: ``official`` 或 ``mcim``
        """
        return self._mod_source_policy().source()

    def mod_api_base(self, platform: str) -> str:
        """
        返回指定平台在当前模组源下的 API 基址。

        :param platform: 平台名称，支持 ``modrinth`` 与 ``curseforge``
        :return: 不带尾斜杠的 API 基址
        """
        return mod_api_base(self.mod_source(), platform)

    def rewrite_mod_file_url(self, url: Any) -> str:
        """
        按当前模组源重写平台返回的文件或图标地址。

        仅替换 MCIM 已提供镜像的主机名，不可替换的主机原样返回。

        :param url: 官方平台返回的地址
        :return: 重写后的地址；空值或无需重写时返回原文
        """
        return rewrite_mod_file_url(str(url or ""), self.mod_source())

    def mod_request(
        self,
        name: str,
        operation: Callable[[str], Any],
        *,
        can_fallback: bool = True,
        is_valid: Callable[[Any], bool] | None = None,
    ) -> Any:
        """
        以当前模组源执行远端请求，失败时切换到另一模组源重试一次。

        :param name: 面向日志的操作名称
        :param operation: 接收模组源名称并返回结果的远端操作
        :param can_fallback: 是否允许换源重试
        :param is_valid: 可选的响应结构校验
        :return: 首选源或备用源的成功结果
        """
        return self._mod_source_policy().request(name, operation, can_fallback=can_fallback, is_valid=is_valid)


__all__ = [
    "ModSourceAware",
    "ModSourcePolicy",
    "ModSourceRequestPolicy",
    "alternate_mod_source",
    "display_mod_source",
    "mod_api_base",
    "mod_user_agent",
    "normalize_mod_source",
    "rewrite_mod_file_url",
]
