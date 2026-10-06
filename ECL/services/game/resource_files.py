# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：本地资源文件的后缀、包元数据与投影结构校验，不生成预览模型。
#
# 公开接口：
#   - class ResourceFileMetadata — 保存通过校验的包名称和格式。
#   - class ResourceFilePolicy — 统一文件选择、安装及列表扫描的类型规则。
# ============================================================

from __future__ import annotations

import json
import math
import struct
import time
import zipfile
import zlib
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from types import MappingProxyType

from ECL.game import ByteArray, LongArray, load_limited
from ECL.utils import GameServiceError


@dataclass(frozen=True, slots=True)
class ResourceFileMetadata:
    """
    保存通过内容校验的包元数据，不保留归档句柄或完整 NBT 文档。
    """

    name: str | None = None
    pack_format: int | None = None


@dataclass(frozen=True, slots=True)
class _Inspection:
    metadata: ResourceFileMetadata | None
    error_code: str = ""
    error_message: str = ""


class ResourceFilePolicy:
    """
    统一本地资源的后缀和内容规则，并短时缓存列表校验结果。

    缓存由锁保护，只存不可变结论；安装必须关闭缓存并在临时目标上复检。
    """

    extensions: Mapping[str, tuple[str, ...]] = MappingProxyType(
        {
            "resourcepack": ("zip",),
            "shaderpack": ("zip",),
            "datapack": ("zip",),
            "schematic": ("litematic", "schem", "schematic"),
            "mod": ("jar", "disabled"),
        }
    )
    max_metadata_bytes: int = 1024 * 1024
    max_nbt_bytes: int = 64 * 1024 * 1024
    max_cache_entries: int = 512
    cache_seconds: float = 10.0
    _cache: OrderedDict[tuple[object, ...], tuple[float, _Inspection]] = OrderedDict()
    _cache_lock = RLock()

    @classmethod
    def invalidate(cls) -> None:
        """
        安装或删除完成后清除列表校验结论，下一次扫描重新读取内容。
        """
        with cls._cache_lock:
            cls._cache.clear()

    @classmethod
    def validate(
        cls, source_path: Path, resource_type: str, *, allow_directory: bool = False, use_cache: bool = False
    ) -> ResourceFileMetadata:
        """
        校验一个资源的后缀和内部结构，返回可复用的包元数据。

        目录只允许资源包或明确开启目录兼容的数据包；缓存仅供列表使用。
        失败不写入、删除或解压任何文件。

        :param source_path: 待读取的本地资源
        :param resource_type: 受支持的资源类型
        :param allow_directory: 是否允许已有的目录包
        :param use_cache: 是否复用短时列表校验结论
        :return: 已校验的包名称与格式
        :raises GameServiceError: 后缀不支持、结构损坏或超过读取预算时抛出
        """
        inspected_path = cls._checked_path(source_path, resource_type, allow_directory)
        try:
            result = cls._inspect_cached(source_path, inspected_path, resource_type, use_cache)
        except OSError as exc:
            raise GameServiceError("资源内容无法读取", "INVALID_RESOURCE_CONTENT") from exc
        if result.metadata is None:
            raise GameServiceError(result.error_message, result.error_code)
        return result.metadata

    @classmethod
    def _checked_path(cls, source_path: Path, resource_type: str, allow_directory: bool) -> Path:
        """
        在读取内容前约束后缀和目录类型，目录元数据不得链接到包外。
        """
        extensions = cls.extensions.get(resource_type)
        if extensions is None:
            raise GameServiceError("未知资源类型", "INVALID_RESOURCE_TYPE")
        is_directory = source_path.is_dir()
        if is_directory:
            if not (resource_type == "resourcepack" or (resource_type == "datapack" and allow_directory)):
                raise GameServiceError("此类资源只接受文件，不支持文件夹", "UNSUPPORTED_RESOURCE_FILE")
            inspected_path = source_path / "pack.mcmeta"
            if inspected_path.is_symlink():
                raise GameServiceError("包元数据不能是符号链接", "INVALID_RESOURCE_CONTENT")
        elif not source_path.is_file() or source_path.suffix.casefold().lstrip(".") not in extensions:
            raise GameServiceError(
                f"此类资源仅支持：{', '.join('.' + value for value in extensions)}", "UNSUPPORTED_RESOURCE_FILE"
            )
        else:
            inspected_path = source_path
        return inspected_path

    @classmethod
    def _inspect_cached(
        cls, source_path: Path, inspected_path: Path, resource_type: str, use_cache: bool
    ) -> _Inspection:
        """
        用文件状态和短期时限复用校验结论，锁内不执行磁盘内容读取。
        """
        stat = inspected_path.stat()
        key = (
            resource_type,
            str(source_path.resolve()),
            source_path.is_dir(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        now = time.monotonic()
        with cls._cache_lock:
            cached = cls._cache.get(key) if use_cache else None
            if cached is not None and now - cached[0] <= cls.cache_seconds:
                cls._cache.move_to_end(key)
                return cached[1]
        result = cls._inspect(source_path, resource_type)
        if use_cache:
            with cls._cache_lock:
                cls._cache[key] = (now, result)
                cls._cache.move_to_end(key)
                while len(cls._cache) > cls.max_cache_entries:
                    cls._cache.popitem(last=False)
        return result

    @classmethod
    def _inspect(cls, source_path: Path, resource_type: str) -> _Inspection:
        """
        将外部归档和 NBT 解析失败转换为稳定结论，不掩盖其他业务错误。
        """
        try:
            return _Inspection(cls._validate_content(source_path, resource_type, source_path.is_dir()))
        except GameServiceError as exc:
            return _Inspection(None, exc.error_code, str(exc))
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            EOFError,
            struct.error,
            zipfile.BadZipFile,
            zlib.error,
            RuntimeError,
        ):
            return _Inspection(None, "INVALID_RESOURCE_CONTENT", "资源内容损坏或结构不受支持")

    @classmethod
    def _validate_content(cls, source_path: Path, resource_type: str, is_directory: bool) -> ResourceFileMetadata:
        """
        按类型读取结构标识，并将外部 JSON／NBT 限定在校验边界内。
        """
        if resource_type == "mod":
            return ResourceFileMetadata()
        if resource_type == "schematic":
            try:
                document = load_limited(source_path, cls.max_nbt_bytes)
            except ValueError as exc:
                if "安全上限" in str(exc):
                    raise GameServiceError("文件过大，无法校验", "RESOURCE_TOO_LARGE") from exc
                raise
            cls._validate_schematic(document, source_path.suffix.casefold())
            return ResourceFileMetadata()
        if resource_type == "shaderpack":
            cls._validate_shader(source_path)
            return ResourceFileMetadata()
        if is_directory:
            with (source_path / "pack.mcmeta").open("rb") as source:
                content = source.read(cls.max_metadata_bytes + 1)
        else:
            with zipfile.ZipFile(source_path) as archive, archive.open("pack.mcmeta") as source:
                content = source.read(cls.max_metadata_bytes + 1)
        return cls._parse_pack_metadata(content)

    @classmethod
    def _validate_shader(cls, source_path: Path) -> None:
        """
        兼容包装目录内的 shaders，并校验一个成员的载荷与 CRC。
        """
        with zipfile.ZipFile(source_path) as archive:
            shader_entries = [
                entry
                for entry in archive.infolist()
                if "shaders" in PurePosixPath(entry.filename).parts[:-1] or entry.filename.endswith("shaders/")
            ]
            if not shader_entries:
                raise GameServiceError("光影包缺少 shaders 目录", "INVALID_RESOURCE_CONTENT")
            entry = next((entry for entry in shader_entries if not entry.is_dir()), None)
            if entry is not None:
                with archive.open(entry) as source:
                    total = 0
                    while chunk := source.read(65536):
                        total += len(chunk)
                        if total > cls.max_nbt_bytes:
                            raise GameServiceError("光影结构文件过大，无法校验", "RESOURCE_TOO_LARGE")

    @classmethod
    def _parse_pack_metadata(cls, content: bytes) -> ResourceFileMetadata:
        """
        校验传统和新版包元数据，描述允许 Minecraft 文本组件表示。
        """
        if len(content) > cls.max_metadata_bytes:
            raise GameServiceError("包元数据过大，无法校验", "RESOURCE_TOO_LARGE")
        value: object = json.loads(content)
        if not isinstance(value, dict) or not isinstance(pack := value.get("pack"), dict):
            raise GameServiceError("包缺少有效的 pack.mcmeta", "INVALID_RESOURCE_CONTENT")
        description: object = pack.get("description")
        if not isinstance(description, (str, dict, list)):
            raise GameServiceError("包描述格式无效", "INVALID_RESOURCE_CONTENT")
        legacy_format: object = pack.get("pack_format")
        if legacy_format is not None and not cls._is_integer(legacy_format, minimum=0):
            raise GameServiceError("包格式字段无效", "INVALID_RESOURCE_CONTENT")
        if "min_format" in pack or "max_format" in pack:
            minimum = cls._format_version(pack.get("min_format"), is_maximum=False)
            maximum = cls._format_version(pack.get("max_format"), is_maximum=True)
            if minimum > maximum:
                raise GameServiceError("包格式范围无效", "INVALID_RESOURCE_CONTENT")
        elif legacy_format is None:
            raise GameServiceError("包缺少格式字段", "INVALID_RESOURCE_CONTENT")
        name = description if isinstance(description, str) else json.dumps(description, ensure_ascii=False)
        return ResourceFileMetadata(name, int(legacy_format) if isinstance(legacy_format, int) else None)

    @staticmethod
    def _is_integer(value: object, *, minimum: int = 0) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

    @classmethod
    def _format_version(cls, value: object, *, is_maximum: bool) -> tuple[int, int]:
        """
        将主次格式号标准化；最大范围未指定次版本时包含所有次版本。
        """
        if cls._is_integer(value):
            return int(value), 0x7FFFFFFF if is_maximum else 0
        if isinstance(value, list) and len(value) in {1, 2} and all(cls._is_integer(item) for item in value):
            return int(value[0]), int(value[1]) if len(value) == 2 else (0x7FFFFFFF if is_maximum else 0)
        raise GameServiceError("包格式范围字段无效", "INVALID_RESOURCE_CONTENT")

    @classmethod
    def _vector(cls, value: object, *, size: bool) -> tuple[int, int, int]:
        """
        读取区域坐标，尺寸允许负方向，返回实际长度。
        """
        if isinstance(value, Mapping):
            coordinates = [value.get(axis) for axis in ("x", "y", "z")]
        elif isinstance(value, list):
            coordinates = value
        else:
            coordinates = []
        if len(coordinates) != 3 or any(
            not isinstance(item, int) or isinstance(item, bool) or (size and item == 0) for item in coordinates
        ):
            raise GameServiceError("投影坐标或尺寸无效", "INVALID_RESOURCE_CONTENT")
        return tuple(abs(int(item)) if size else int(item) for item in coordinates)

    @classmethod
    def _validate_schematic(cls, root: Mapping[str, object], extension: str) -> None:
        """
        识别三种投影结构并检查存储一致性，不生成按体素展开的列表。
        """
        if extension == ".litematic":
            cls._validate_litematic(root)
            return
        payload = root.get("Schematic", root)
        if not isinstance(payload, Mapping):
            raise GameServiceError("投影根结构无效", "INVALID_RESOURCE_CONTENT")
        if extension == ".schematic":
            cls._validate_alpha(payload, cls._volume(payload))
        else:
            cls._validate_sponge(payload, cls._volume(payload))

    @classmethod
    def _volume(cls, payload: Mapping[str, object]) -> int:
        """
        校验标准尺寸与已有简化尺寸表示，使用整数计算体积。
        """
        dimensions = [payload.get(axis) for axis in ("Width", "Height", "Length")]
        if all(value is None for value in dimensions) and "Size" in payload:
            return math.prod(cls._vector(payload["Size"], size=True))
        if any(not cls._is_integer(value, minimum=1) for value in dimensions):
            raise GameServiceError("投影尺寸无效", "INVALID_RESOURCE_CONTENT")
        return math.prod(int(value) for value in dimensions)

    @classmethod
    def _validate_litematic(cls, root: Mapping[str, object]) -> None:
        """
        检查区域色板和紧密／历史按字长填充的存储长度，保留负方向尺寸。
        """
        regions = root.get("Regions")
        if not isinstance(regions, Mapping) or not regions:
            raise GameServiceError("Litematica 投影缺少区域", "INVALID_RESOURCE_CONTENT")
        for region in regions.values():
            if not isinstance(region, Mapping):
                raise GameServiceError("Litematica 区域格式无效", "INVALID_RESOURCE_CONTENT")
            dimensions = cls._vector(region.get("Size"), size=True)
            cls._vector(region.get("Position"), size=False)
            palette = region.get("BlockStatePalette")
            storage = region.get("BlockStates")
            if (
                not isinstance(palette, list)
                or not palette
                or any(not isinstance(entry, Mapping) or not isinstance(entry.get("Name"), str) for entry in palette)
                or not isinstance(storage, LongArray)
            ):
                raise GameServiceError("Litematica 调色板或存储格式无效", "INVALID_RESOURCE_CONTENT")
            volume = math.prod(dimensions)
            bits = max(2, (len(palette) - 1).bit_length())
            lengths = {(volume * width + 63) // 64 for width in (bits, max(4, bits))}
            lengths.update((volume + 64 // width - 1) // (64 // width) for width in (bits, max(4, bits)) if width <= 64)
            if len(storage) not in lengths:
                raise GameServiceError("Litematica 存储长度与尺寸不符", "INVALID_RESOURCE_CONTENT")

    @classmethod
    def _validate_alpha(cls, payload: Mapping[str, object], volume: int) -> None:
        """
        传统格式的两个字节数组必须匹配体积，扩展 ID 数组按半字节存储。
        """
        blocks, data = payload.get("Blocks"), payload.get("Data")
        extra = payload.get("AddBlocks")
        if (
            payload.get("Materials") != "Alpha"
            or not isinstance(blocks, ByteArray)
            or not isinstance(data, ByteArray)
            or len(blocks) != volume
            or len(data) != volume
            or (extra is not None and (not isinstance(extra, ByteArray) or len(extra) != (volume + 1) // 2))
        ):
            raise GameServiceError("传统投影结构或数据长度无效", "INVALID_RESOURCE_CONTENT")

    @classmethod
    def _validate_sponge(cls, payload: Mapping[str, object], volume: int) -> None:
        """
        兼容 Sponge 三代容器及项目简化布局，检查索引数量与色板引用。
        """
        version = payload.get("Version")
        if (not isinstance(version, int) or version not in (1, 2, 3) or isinstance(version, bool)) and (
            version is not None or not isinstance(payload.get("Size"), list)
        ):
            raise GameServiceError("Sponge 投影版本无效", "INVALID_RESOURCE_CONTENT")
        if version in (2, 3) and not cls._is_integer(payload.get("DataVersion")):
            raise GameServiceError("Sponge 投影缺少有效的数据版本", "INVALID_RESOURCE_CONTENT")
        container = payload.get("Blocks") if version == 3 else payload
        if version == 3 and container is None and isinstance(payload.get("Entities"), list):
            return
        if not isinstance(container, Mapping):
            raise GameServiceError("Sponge 投影方块容器无效", "INVALID_RESOURCE_CONTENT")
        palette = container.get("Palette")
        blocks = container.get("Data") if version == 3 else container.get("BlockData", container.get("Blocks"))
        if (
            not isinstance(palette, Mapping)
            or not palette
            or not all(isinstance(name, str) and cls._is_integer(index) for name, index in palette.items())
            or not isinstance(blocks, ByteArray)
        ):
            raise GameServiceError("Sponge 投影调色板或存储无效", "INVALID_RESOURCE_CONTENT")
        allowed_indices = set(palette.values())
        if len(allowed_indices) != len(palette):
            raise GameServiceError("Sponge 投影调色板索引重复", "INVALID_RESOURCE_CONTENT")
        cls._validate_indices(blocks, allowed_indices, volume)

    @classmethod
    def _validate_indices(cls, blocks: bytes, allowed_indices: set[int], volume: int) -> None:
        """
        流式计数 varint 索引，不创建与投影体积相同的展开数组。
        """
        count = 0
        value = shift = 0
        for byte in blocks:
            if shift >= 35:
                raise GameServiceError("Sponge 投影索引编码无效", "INVALID_RESOURCE_CONTENT")
            value |= (byte & 0x7F) << shift
            if byte & 0x80:
                shift += 7
            else:
                if value not in allowed_indices:
                    raise GameServiceError("Sponge 投影索引超出调色板", "INVALID_RESOURCE_CONTENT")
                count += 1
                value = shift = 0
        if shift or count != volume:
            raise GameServiceError("Sponge 投影数据长度与尺寸不符", "INVALID_RESOURCE_CONTENT")
