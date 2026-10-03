# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例兼容性扩展点：插件提供外部启动器的实例元数据识别。
#
# 公开接口：
#   - class InstanceCompatibilityContext — 描述插件读取单个 Minecraft 实例元数据时可用的只读上下文。
#   - class ExternalInstanceMetadata — 保存一个兼容来源针对单个实例提供的只读元数据。
#   - class InstanceCompatibilityRegistry — 保存插件注册的实例兼容读取器，并隔离单个提供者的读取失败。
#       - register(owner, source, title, reader, watch_paths) -> None — 注册或更新一个由插件拥有的实例元数据来源。
#       - unregister_owner(owner) -> None — 移除指定插件拥有的全部实例兼容来源。
#       - revision() -> int — 返回注册表变更序号，供扫描缓存将插件启停视为输入变化。
#       - describe_sources() -> list[dict[str, str]] — 返回当前已注册来源的稳定标识、标题和所属插件。
#       - read(context) -> list[ExternalInstanceMetadata] — 调用全部提供者并汇总元数据，单个插件异常会转换为来源警告。
#       - resolve_watch_paths(options=…) -> list[tuple[str, Path]] — 汇总插件声明的外部监听文件，供版本扫描缓存自动失效。
# ============================================================

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from ECL.utils import get_logger


class InstanceCompatibilityPolicy:
    """
    保存实例兼容来源的标识校验规则。
    """

    source_pattern = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


@dataclass(frozen=True, slots=True)
class InstanceCompatibilityContext:
    """
    描述插件读取单个 Minecraft 实例元数据时可用的只读上下文。

    ``options`` 由宿主按插件来源名分组传入，例如
    ``{"qomicex": {"instances_path": "..."}}``。
    """

    game_path: Path
    instance_path: Path
    version_id: str
    vanilla_name: str
    primary_loader: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExternalInstanceMetadata:
    """
    保存一个兼容来源针对单个实例提供的只读元数据。
    """

    source: str
    modified_ns: int
    fields: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


InstanceMetadataReader = Callable[[InstanceCompatibilityContext], ExternalInstanceMetadata | None]
InstanceWatchPathResolver = Callable[[Mapping[str, Any]], Iterable[str | Path]]


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    owner: str
    source: str
    title: str
    reader: InstanceMetadataReader
    watch_paths: InstanceWatchPathResolver | None = None


class InstanceCompatibilityRegistry:
    """
    保存插件注册的实例兼容读取器，并隔离单个提供者的读取失败。
    """

    def __init__(self) -> None:
        """
        创建线程安全的实例兼容提供者注册表。
        """
        self._providers: dict[str, _RegisteredProvider] = {}
        self._revision = 0
        self._lock = RLock()
        self._logger = get_logger("InstanceCompatibilityRegistry")

    def register(
        self,
        *,
        owner: str,
        source: str,
        title: str,
        reader: InstanceMetadataReader,
        watch_paths: InstanceWatchPathResolver | None = None,
    ) -> None:
        """
        注册或更新一个由插件拥有的实例元数据来源。

        :param owner: 注册该来源的插件名
        :param source: 稳定的来源标识
        :param title: 面向用户显示的来源名称
        :param reader: 单实例只读元数据读取器
        :param watch_paths: 返回需要参与扫描缓存失效判断的外部文件
        """
        normalized = str(source).strip().casefold()
        if not InstanceCompatibilityPolicy.source_pattern.fullmatch(normalized):
            raise ValueError(f"实例兼容来源标识无效: {source}")
        if not callable(reader):
            raise TypeError("实例兼容读取器必须可调用")
        provider = _RegisteredProvider(
            owner=owner,
            source=normalized,
            title=str(title).strip() or normalized,
            reader=reader,
            watch_paths=watch_paths,
        )
        with self._lock:
            current = self._providers.get(normalized)
            if current is not None and current.owner != owner:
                raise ValueError(f"实例兼容来源已由插件 {current.owner} 注册: {normalized}")
            self._providers[normalized] = provider
            self._revision += 1

    def unregister_owner(self, owner: str) -> None:
        """
        移除指定插件拥有的全部实例兼容来源。

        :param owner: 插件名称
        """
        with self._lock:
            previous_count = len(self._providers)
            self._providers = {
                source: provider for source, provider in self._providers.items() if provider.owner != owner
            }
            if len(self._providers) != previous_count:
                self._revision += 1

    @property
    def revision(self) -> int:
        """
        返回注册表变更序号，供扫描缓存将插件启停视为输入变化。
        """
        with self._lock:
            return self._revision

    def describe_sources(self) -> list[dict[str, str]]:
        """
        返回当前已注册来源的稳定标识、标题和所属插件。
        """
        with self._lock:
            providers = tuple(self._providers.values())
        return [
            {"source": provider.source, "title": provider.title, "plugin": provider.owner}
            for provider in sorted(providers, key=lambda item: item.source)
        ]

    def read(self, context: InstanceCompatibilityContext) -> list[ExternalInstanceMetadata]:
        """
        调用全部提供者并汇总元数据，单个插件异常会转换为来源警告。

        :param context: 当前实例的只读扫描上下文
        :return: 各插件来源返回的元数据
        """
        with self._lock:
            providers = tuple(self._providers.values())
        results: list[ExternalInstanceMetadata] = []
        for provider in providers:
            try:
                result = provider.reader(context)
                if result is None:
                    continue
                if not isinstance(result, ExternalInstanceMetadata):
                    raise TypeError("读取器必须返回 ExternalInstanceMetadata 或 None")
                result.source = provider.source
                results.append(result)
            except Exception as exc:
                self._logger.exception("插件实例兼容读取失败：来源：%s", provider.source)
                results.append(
                    ExternalInstanceMetadata(
                        source=provider.source,
                        modified_ns=0,
                        warnings=[f"{provider.title} 兼容读取失败: {exc}"],
                    )
                )
        return results

    def resolve_watch_paths(self, options: Mapping[str, Any] | None = None) -> list[tuple[str, Path]]:
        """
        汇总插件声明的外部监听文件，供版本扫描缓存自动失效。

        :param options: 按来源分组的宿主配置
        :return: ``(来源标识, 文件路径)`` 列表
        """
        with self._lock:
            providers = tuple(self._providers.values())
        resolved: list[tuple[str, Path]] = []
        for provider in providers:
            if provider.watch_paths is None:
                continue
            try:
                paths = provider.watch_paths(options or {})
                for path in paths:
                    resolved.append((provider.source, Path(path).expanduser().resolve(strict=False)))
            except Exception:
                self._logger.exception("插件兼容监听路径解析失败：来源：%s", provider.source)
        return resolved


__all__ = [
    "ExternalInstanceMetadata",
    "InstanceCompatibilityContext",
    "InstanceCompatibilityRegistry",
    "InstanceMetadataReader",
    "InstanceWatchPathResolver",
]
