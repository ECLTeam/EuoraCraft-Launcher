# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为游戏核心的同步元数据请求和直接文件下载提供双源回退。
#
# 公开接口：
#   - class PreferredApiClient — 按首选源调用 Core API，并在可替代请求失败时尝试备用源。
#   - alternate_source() — 返回官方源与 BMCLAPI 之间的备用源名称。
# ============================================================

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from logging import Logger
from pathlib import Path
from typing import TypeVar

import httpx

from ECL.game import BaseApiClient, InstallEvent, InstallObserver

result_type = TypeVar("result_type")


class _InvalidRemoteResponseError(ValueError):
    """
    标记远端响应缺少 Core 后续读取所需的基本结构。
    """


def alternate_source(source: str) -> str:
    """
    返回首选源对应的备用源名称。

    调用方须先通过游戏服务的下载源校验；此函数不会接受第三种来源。

    :param source: 已校验的首选源名称
    :return: 另一个下载源名称
    """
    return "bmclapi" if source == "official" else "official"


class PreferredApiClient(BaseApiClient):
    """
    按首选源请求 Core 元数据，并在可替代的远端失败时尝试备用源。

    `config` 保持首选源配置，供 Core 文件检查器生成首选下载 URL。备用
    客户端负责自己的来源特定 URL 和响应解析，避免把镜像响应按官方格式处理。
    """

    def __init__(self, preferred: BaseApiClient, alternate: BaseApiClient, source: str, logger: Logger) -> None:
        self.config = preferred.config
        self._preferred = preferred
        self._alternate = alternate
        self._source = source
        self._logger = logger

    def _request(
        self,
        name: str,
        operation: Callable[[BaseApiClient], result_type],
        *,
        can_fallback: bool = True,
        is_valid: Callable[[result_type], bool] | None = None,
        on_fallback: Callable[[], None] | None = None,
    ) -> result_type:
        """
        仅在远端请求或远端响应解析失败时换源，不重复本地安装步骤。

        两个客户端抛出的异常通过异常链保留；调用方能看到备用源的最终错误。
        """

        def checked(client: BaseApiClient) -> result_type:
            response = operation(client)
            if is_valid is not None and not is_valid(response):
                raise _InvalidRemoteResponseError(f"{name}响应结构无效")
            return response

        retryable_errors = (
            httpx.HTTPError,
            json.JSONDecodeError,
            UnicodeError,
            KeyError,
            IndexError,
            TypeError,
            _InvalidRemoteResponseError,
        )
        try:
            return checked(self._preferred)
        except retryable_errors as primary_error:
            if not can_fallback:
                raise
            self._logger.warning("%s 从 %s 获取失败，尝试 %s", name, self._source, alternate_source(self._source))
            if on_fallback is not None:
                on_fallback()
            try:
                return checked(self._alternate)
            except retryable_errors as alternate_error:
                raise alternate_error from primary_error

    def _download_file_request(
        self,
        name: str,
        stage: str,
        observer: InstallObserver | None,
        download: Callable[[BaseApiClient, InstallObserver], result_type],
    ) -> result_type:
        """
        为三类直接文件下载保留跨来源的实际传输量，并显式报告切换来源。

        回调、来源偏移和取消检查均只属于本次调用，不写入共享客户端。
        """
        current_observer = observer or InstallObserver()
        transferred_offset = 0
        last_transferred = 0

        def report(event: InstallEvent) -> None:
            nonlocal last_transferred
            last_transferred = event.transferred_bytes
            current_observer.callback(replace(event, transferred_bytes=transferred_offset + last_transferred))

        forwarded_observer = InstallObserver(report, current_observer.check_cancelled)

        def switch_source() -> None:
            nonlocal transferred_offset, last_transferred
            transferred_offset += last_transferred
            last_transferred = 0
            forwarded_observer.emit(stage, action="fallback")

        return self._request(name, lambda client: download(client, forwarded_observer), on_fallback=switch_source)

    def get_minecraft_manifest(self) -> dict:
        """
        获取 Minecraft 版本清单，首选源不可用时改用备用源。

        :return: 版本清单
        """
        return self._request(
            "版本清单",
            lambda client: client.get_minecraft_manifest(),
            is_valid=lambda payload: (
                isinstance(payload, dict)
                and isinstance(payload.get("latest"), dict)
                and isinstance(payload.get("versions"), list)
            ),
        )

    def get_minecraft_json(self, version_id: str, sha1: str) -> dict:
        """
        获取指定版本元数据，首选源不可用时改用备用源。

        :param version_id: Minecraft 版本标识
        :param sha1: 元数据校验值
        :return: 版本元数据
        """
        return self._request(
            "版本元数据",
            lambda client: client.get_minecraft_json(version_id, sha1),
            is_valid=lambda payload: isinstance(payload, dict),
        )

    def get_asset_index(self, asset_id: str, sha1: str) -> dict:
        """
        获取资源索引，首选源不可用时改用备用源。

        :param asset_id: 资源索引标识
        :param sha1: 索引校验值
        :return: 资源索引
        """
        return self._request(
            "资源索引",
            lambda client: client.get_asset_index(asset_id, sha1),
            is_valid=lambda payload: isinstance(payload, dict) and isinstance(payload.get("objects"), dict),
        )

    def get_client_jar_url(self, sha1: str) -> str:
        """
        返回首选源的客户端文件 URL，供批量下载阶段使用。

        :param sha1: 客户端文件校验值
        :return: 首选源 URL
        """
        return self._preferred.get_client_jar_url(sha1)

    def download_client_jar(self, sha1: str, save_path: Path | str, *, observer: InstallObserver | None = None) -> None:
        """
        下载客户端文件，首选源请求失败时改用备用源。

        :param sha1: 客户端文件校验值
        :param save_path: 目标文件路径
        :param observer: 本次调用的进度与取消检查；缺省时仅执行下载
        """
        self._download_file_request(
            "客户端文件",
            "download_client",
            observer,
            lambda client, reporter: client.download_client_jar(sha1, save_path, observer=reporter),
        )

    def get_fabric_versions(self, game_version_id: str) -> list[dict]:
        """
        获取 Fabric 版本列表，首选源不可用时改用备用源。

        :param game_version_id: Minecraft 版本标识
        :return: Fabric 版本信息
        """
        return self._request(
            "Fabric 版本",
            lambda client: client.get_fabric_versions(game_version_id),
            is_valid=lambda payload: isinstance(payload, list),
        )

    def get_fabric_profile(self, game_version_id: str, loader_version: str) -> dict:
        """
        获取 Fabric 配置，首选源不可用时改用备用源。

        :param game_version_id: Minecraft 版本标识
        :param loader_version: 加载器版本
        :return: Fabric 版本配置
        """
        return self._request(
            "Fabric 配置",
            lambda client: client.get_fabric_profile(game_version_id, loader_version),
            is_valid=lambda payload: isinstance(payload, dict),
        )

    def get_neoforged_versions(self, game_version_id: str) -> dict[str, list]:
        """
        获取 NeoForged 版本列表，由相应来源的客户端解析响应。

        :param game_version_id: Minecraft 版本标识
        :return: 分类后的 NeoForged 版本信息
        """
        return self._request(
            "NeoForged 版本",
            lambda client: client.get_neoforged_versions(game_version_id),
            is_valid=lambda payload: isinstance(payload, dict),
        )

    def download_neoforged_installer(
        self,
        game_version_id: str,
        loader_version: str,
        save_path: Path | str,
        *,
        observer: InstallObserver | None = None,
    ) -> Path:
        """
        下载 NeoForged 安装器，首选源请求失败时改用备用源。

        :param game_version_id: Minecraft 版本标识
        :param loader_version: 加载器版本
        :param save_path: 安装器缓存目录
        :param observer: 本次调用的进度与取消检查；缺省时仅执行下载
        :return: 下载后的安装器路径
        """
        return self._download_file_request(
            "NeoForged 安装器",
            "download_loader",
            observer,
            lambda client, reporter: client.download_neoforged_installer(
                game_version_id, loader_version, save_path, observer=reporter
            ),
        )

    def get_forge_versions(self, game_version_id: str) -> list[dict[str, str]]:
        """
        获取 Forge 版本列表，由相应来源的客户端解析响应。

        :param game_version_id: Minecraft 版本标识
        :return: Forge 版本信息
        """
        return self._request(
            "Forge 版本",
            lambda client: client.get_forge_versions(game_version_id),
            is_valid=lambda payload: isinstance(payload, list),
        )

    def download_forge_installer(
        self,
        game_version_id: str,
        loader_version: str,
        save_path: Path | str,
        *,
        observer: InstallObserver | None = None,
    ) -> Path:
        """
        下载 Forge 安装器，首选源请求失败时改用备用源。

        :param game_version_id: Minecraft 版本标识
        :param loader_version: 加载器版本
        :param save_path: 安装器缓存目录
        :param observer: 本次调用的进度与取消检查；缺省时仅执行下载
        :return: 下载后的安装器路径
        """
        return self._download_file_request(
            "Forge 安装器",
            "download_loader",
            observer,
            lambda client, reporter: client.download_forge_installer(
                game_version_id, loader_version, save_path, observer=reporter
            ),
        )

    def get_quilt_support(self) -> list[dict]:
        """
        查询 Quilt 支持的游戏版本；两源共用同一服务时不重复请求。

        :return: Quilt 支持列表
        """
        return self._request(
            "Quilt 支持列表",
            lambda client: client.get_quilt_support(),
            can_fallback=self._preferred.config.QuiltMeta != self._alternate.config.QuiltMeta,
        )

    def get_quilt_versions(self) -> list[dict]:
        """
        查询 Quilt 加载器版本；两源共用同一服务时不重复请求。

        :return: Quilt 加载器版本列表
        """
        return self._request(
            "Quilt 版本",
            lambda client: client.get_quilt_versions(),
            can_fallback=self._preferred.config.QuiltMeta != self._alternate.config.QuiltMeta,
        )

    def get_quilt_profile(self, game_version_id: str, loader_version: str) -> dict:
        """
        获取 Quilt 配置；两源共用同一服务时不重复请求。

        :param game_version_id: Minecraft 版本标识
        :param loader_version: 加载器版本
        :return: Quilt 版本配置
        """
        return self._request(
            "Quilt 配置",
            lambda client: client.get_quilt_profile(game_version_id, loader_version),
            can_fallback=self._preferred.config.QuiltMeta != self._alternate.config.QuiltMeta,
        )

    def close(self) -> None:
        """
        关闭两个 Core HTTP 客户端，释放连接池。
        """
        try:
            self._preferred.close()
        finally:
            self._alternate.close()
