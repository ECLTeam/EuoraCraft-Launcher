# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：实例兼容性扩展点：插件提供外部启动器的实例元数据识别。
#
# 公开接口：
#   - class InstanceCompatibilityContext — 保存插件读取单个 Minecraft 实例信息所需的只读数据。
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
    保存插件读取单个 Minecraft 实例信息所需的只读数据。

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


# 第三方来源返回的元数据上限：字段数量与文本长度都要有界。
limit_metadata_fields = 32
limit_metadata_text = 256
limit_metadata_warnings = 8


def sanitize_external_metadata(payload: object, source: str) -> ExternalInstanceMetadata | None:
    """
    把第三方来源的返回值收敛为受控元数据，越界项丢弃而不是整体失败。

    :param payload: 插件回调返回值
    :param source: 来源标识，由宿主填入而不是采信插件
    :return: 受控元数据，无有效内容时为空
    """
    if not isinstance(payload, dict):
        return None
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return None
    cleaned: dict[str, object] = {}
    for key, value in list(fields.items())[:limit_metadata_fields]:
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            cleaned[key[:64]] = value[:limit_metadata_text]
        elif key in {"hidden", "pinned"} and type(value) is bool:
            cleaned[key] = value
        elif key == "icon":
            icon = _safe_external_icon(value)
            if icon is not None:
                icon["source"] = source
                cleaned[key] = icon
    raw_warnings = payload.get("warnings")
    warnings = (
        [item[:limit_metadata_text] for item in raw_warnings[:limit_metadata_warnings] if isinstance(item, str)]
        if isinstance(raw_warnings, list)
        else []
    )
    stats = _external_stats(payload.get("stats"))
    modified_ns = payload.get("modified_ns", 0)
    if type(modified_ns) is not int or not 0 <= modified_ns <= 2**53 - 1:
        modified_ns = 0
    if not cleaned and not warnings and not stats:
        return None
    return ExternalInstanceMetadata(
        source=source, modified_ns=modified_ns, fields=cleaned, stats=stats, warnings=warnings
    )


def _external_stats(raw_stats: object) -> dict[str, int | str]:
    """
    保留有限非负运行时间与时间字符串，不接受任意统计对象。
    """
    stats: dict[str, int | str] = {}
    if isinstance(raw_stats, dict):
        duration = raw_stats.get("totalRunDurationSeconds")
        launched = raw_stats.get("lastLaunchedAt")
        if type(duration) is int and 0 <= duration <= 2**53 - 1:
            stats["totalRunDurationSeconds"] = duration
        if isinstance(launched, str) and len(launched) <= 64:
            stats["lastLaunchedAt"] = launched
    return stats


def _safe_external_icon(value: object) -> dict[str, str] | None:
    """
    只接受受控图标名称和有界 PNG，防止外部元数据注入 URL 或脚本图像。
    """
    if not isinstance(value, dict) or set(value) != {"type", "value", "source"}:
        return None
    kind, content = value.get("type"), value.get("value")
    if kind in {"loader", "builtin"} and isinstance(content, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", content):
        return {"type": kind, "value": content}
    if (
        kind != "data"
        or not isinstance(content, str)
        or not content.startswith("data:image/png;base64,")
        or len(content) > 700000
    ):
        return None
    import base64
    import binascii
    from io import BytesIO

    from PIL import Image

    try:
        decoded = base64.b64decode(content.split(",", 1)[1], validate=True)
        if len(decoded) > 512 * 1024:
            return None
        with Image.open(BytesIO(decoded)) as picture:
            if picture.format != "PNG" or max(picture.size) > 2048:
                return None
            picture.verify()
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError):
        return None
    return {"type": "data", "value": content}


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

        :param context: 当前实例的只读扫描信息
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
