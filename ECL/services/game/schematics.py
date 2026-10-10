# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：原理图服务：litematic/schem 解析为体素预览数据。
#
# 公开接口：
#   - class LitematicaRegion
#   - class SchematicCoordinator — 解析原理图文件为适合 3D 预览的体素模型数据。
#       - schematic_preview(game_path, version_id, resource_id, version_isolation=…) -> dict[str, Any] — 读取并解析指定原理图，返回体素模型数据供前端渲染。
#       - schematic_assets(game_path, version_id, blocks, version_isolation=…, locale=…) -> dict[str, Any] — 读取方块资源与当前启动器语言名称。
#       - export_schematic_material_manifest(...) -> dict[str, str] — 导出当前原理图会话的材料审计清单。
# ============================================================

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import re
import secrets
import sys
import time
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from ECL.game import Compound, IntArray, List, load, load_limited
from ECL.utils import atomic_write_text

from .base import GameServiceError
from .resources import ResourceCatalogPolicy
from .workspace import ResolvedInstanceTarget, resolve_relative_id


class SchematicPalette:
    """
    原理图预览的内置方块调色板。

    颜色为可辨认主要结构的近似值，不追求与原版材质逐像素一致。
    """

    unknown_color = (140, 140, 140)
    block_colors: dict[str, tuple[int, int, int]] = {
        "air": (0, 0, 0),
        "cave_air": (0, 0, 0),
        "void_air": (0, 0, 0),
        "stone": (124, 124, 124),
        "cobblestone": (128, 128, 136),
        "gravel": (136, 124, 118),
        "dirt": (124, 94, 70),
        "grass_block": (86, 148, 74),
        "sand": (219, 211, 164),
        "sandstone": (212, 199, 146),
        "red_sand": (192, 130, 74),
        "clay": (156, 168, 182),
        "granite": (148, 118, 108),
        "diorite": (178, 178, 178),
        "andesite": (140, 140, 140),
        "deepslate": (75, 73, 78),
        "bedrock": (62, 60, 60),
        "water": (54, 101, 210),
        "lava": (214, 108, 36),
        "obsidian": (24, 16, 32),
        "snow_block": (238, 240, 242),
        "ice": (150, 194, 230),
        "packed_ice": (139, 168, 190),
        "coal_ore": (110, 110, 110),
        "iron_ore": (142, 120, 104),
        "gold_ore": (150, 140, 70),
        "diamond_ore": (128, 200, 196),
        "emerald_ore": (90, 190, 128),
        "redstone_ore": (150, 70, 70),
        "lapis_ore": (70, 100, 190),
        "iron_block": (212, 212, 216),
        "gold_block": (246, 198, 62),
        "diamond_block": (104, 224, 214),
        "emerald_block": (64, 202, 118),
        "redstone_block": (168, 42, 34),
        "lapis_block": (50, 84, 200),
        "coal_block": (46, 46, 48),
        "oak_log": (94, 74, 44),
        "spruce_log": (62, 46, 32),
        "birch_log": (208, 196, 164),
        "jungle_log": (94, 76, 40),
        "acacia_log": (106, 66, 44),
        "dark_oak_log": (54, 42, 30),
        "oak_planks": (178, 148, 96),
        "spruce_planks": (140, 112, 76),
        "birch_planks": (206, 192, 156),
        "jungle_planks": (150, 114, 74),
        "acacia_planks": (176, 132, 76),
        "dark_oak_planks": (104, 82, 54),
        "oak_leaves": (74, 128, 62),
        "spruce_leaves": (54, 104, 54),
        "birch_leaves": (118, 158, 80),
        "jungle_leaves": (78, 122, 66),
        "acacia_leaves": (120, 148, 86),
        "dark_oak_leaves": (58, 94, 52),
        "glass": (190, 216, 218),
        "stained_glass_white": (190, 216, 218),
        "glass_pane": (190, 216, 218),
        "smooth_stone": (150, 150, 152),
        "bricks": (152, 92, 82),
        "stone_bricks": (130, 130, 134),
        "mossy_stone_bricks": (116, 130, 106),
        "cracked_stone_bricks": (116, 114, 118),
        "netherrack": (96, 46, 44),
        "nether_bricks": (54, 32, 36),
        "netherite_block": (70, 66, 74),
        "soul_sand": (78, 62, 54),
        "end_stone": (214, 216, 160),
        "purpur_block": (168, 128, 168),
        "magma_block": (140, 84, 40),
        "sea_lantern": (206, 220, 214),
        "wool_white": (216, 216, 216),
        "wool_orange": (226, 126, 38),
        "wool_magenta": (178, 76, 216),
        "wool_light_blue": (102, 153, 216),
        "wool_yellow": (226, 214, 40),
        "wool_lime": (94, 180, 42),
        "wool_pink": (216, 130, 152),
        "wool_gray": (76, 76, 76),
        "wool_light_gray": (158, 156, 158),
        "wool_cyan": (40, 128, 152),
        "wool_purple": (126, 60, 180),
        "wool_blue": (52, 66, 172),
        "wool_brown": (90, 62, 40),
        "wool_green": (66, 124, 52),
        "wool_red": (162, 44, 42),
        "wool_black": (26, 22, 22),
        "terracotta": (150, 90, 56),
        "white_terracotta": (208, 174, 133),
        "orange_terracotta": (156, 88, 44),
        "red_terracotta": (146, 60, 42),
        "cyan_terracotta": (82, 104, 106),
        "light_blue_terracotta": (106, 128, 146),
        "lime_terracotta": (104, 118, 66),
        "pink_terracotta": (144, 96, 106),
        "gray_terracotta": (76, 68, 64),
        "light_gray_terracotta": (132, 122, 110),
        "magenta_terracotta": (142, 70, 108),
        "yellow_terracotta": (164, 126, 62),
        "blue_terracotta": (72, 82, 132),
        "brown_terracotta": (86, 60, 42),
        "green_terracotta": (78, 96, 62),
        "purple_terracotta": (112, 66, 108),
        "black_terracotta": (58, 36, 34),
        "mud": (134, 112, 84),
        "mud_bricks": (136, 112, 78),
        "cut_copper": (178, 100, 106),
        "exposed_cut_copper": (150, 122, 112),
        "weathered_cut_copper": (108, 136, 118),
        "oxidized_cut_copper": (80, 132, 118),
        "waxed_cut_copper": (178, 100, 106),
    }


def _block_color(name: str) -> tuple[int, int, int]:
    # 去掉命名空间、属性和方块状态后缀，逐级回退匹配基础方块名。
    base = name.split(":", 1)[-1]
    for candidate in (base, base.split("[", 1)[0]):
        color = SchematicPalette.block_colors.get(candidate)
        if color is not None:
            return color
        for key, value in SchematicPalette.block_colors.items():
            if candidate.startswith(key):
                return value
    return SchematicPalette.unknown_color


def _is_air_block(name: str) -> bool:
    return name.rsplit(":", 1)[-1] in {"air", "cave_air", "void_air"}


def _normalize(index: int, size: int, axis: int) -> int:
    # 按采样步长计算目标位置，并确保位置不超过目标轴长。
    if size <= 1:
        return 0
    return min(index * axis // size, axis - 1)


def _read_litematica_vector(value: Any, field_name: str) -> list[int]:
    """
    读取 Litematica 的三维 NBT 坐标，并兼容旧的三元素数组表示。

    标准 Litematica 把 ``Size`` 与 ``Position`` 编码为含 ``x/y/z`` 的复合标签；
    历史测试数据和少数第三方导出则可能使用整数数组。解析失败统一转换为领域错误，
    避免把底层 ``ValueError`` 直接返回给前端。

    :param value: NBT 标签中的坐标值
    :param field_name: 用于错误信息的字段名
    :return: 按 x、y、z 顺序排列的三个整数
    :raises GameServiceError: 坐标字段缺失、维度不完整或包含非整数值时抛出
    """
    if isinstance(value, Mapping):
        try:
            coordinates = [value[axis] for axis in ("x", "y", "z")]
        except KeyError as exc:
            raise GameServiceError(f"原理图 {field_name} 坐标不完整", "SCHEMATIC_INVALID") from exc
    elif isinstance(value, (list, tuple)):
        coordinates = list(value)
    else:
        raise GameServiceError(f"原理图 {field_name} 坐标格式无效", "SCHEMATIC_INVALID")
    if len(coordinates) != 3 or any(isinstance(item, bool) or not isinstance(item, int) for item in coordinates):
        raise GameServiceError(f"原理图 {field_name} 坐标格式无效", "SCHEMATIC_INVALID")
    return [int(item) for item in coordinates]


def _block_palette_entry(value: Any) -> dict[str, Any]:
    """
    将 NBT 方块状态转换为前端纹理渲染所需的稳定色板条目。

    :param value: ``BlockStatePalette`` 中的单个复合标签
    :return: 包含方块标识、状态属性和颜色回退值的字典
    """
    entry = value if isinstance(value, Mapping) else {}
    raw_name = str(entry.get("Name") or "minecraft:air")
    name, separator, serialized_properties = raw_name.partition("[")
    properties_tag = entry.get("Properties")
    properties = (
        {str(key): str(item) for key, item in properties_tag.items()} if isinstance(properties_tag, Mapping) else {}
    )
    if separator and serialized_properties.endswith("]"):
        for pair in serialized_properties[:-1].split(","):
            key, equals, item = pair.partition("=")
            if key and equals:
                properties[key] = item
    return {"name": name, "properties": properties, "color": list(_block_color(name))}


class LitematicaRegion:
    __slots__ = ("block_palette", "indices", "mask_bits", "name", "position", "size")

    def __init__(self, name: str, region: Compound) -> None:
        self.name = name
        raw_size = _read_litematica_vector(region.get("Size"), "Size")
        raw_position = _read_litematica_vector(region.get("Position"), "Position")
        if any(axis == 0 for axis in raw_size):
            raise GameServiceError("原理图区域尺寸不能为零", "SCHEMATIC_INVALID")
        self.size = [abs(axis) for axis in raw_size]
        # 负尺寸表示区域从 Position 向负方向延伸；预览从区域的最小坐标开始。
        self.position = [
            position if size > 0 else position + size + 1 for position, size in zip(raw_position, raw_size, strict=True)
        ]
        palette_tag = region.get("BlockStatePalette") or List()
        self.block_palette = [_block_palette_entry(item) for item in palette_tag]
        # Minecraft 调色板位宽最小为 4，超过位宽后再按调色板大小进位，保证与 BlockStates 编码一致。
        self.mask_bits = max(4, (max(1, len(self.block_palette)) - 1).bit_length())
        self.indices = self._decode_indices(region.get("BlockStates") or IntArray())

    def _decode_indices(self, storage: IntArray) -> list[int]:
        # 依据 litematic BlockStates 的紧凑位数组解码每个方块对应的调色板索引。
        bits = self.mask_bits
        mask = (1 << bits) - 1
        total = self.size[0] * self.size[1] * self.size[2]
        result: list[int] = []
        for position_index in range(total):
            bit_index = position_index * bits
            word_index, offset = divmod(bit_index, 64)
            word = (storage[word_index] & ((1 << 64) - 1)) if word_index < len(storage) else 0
            value = word >> offset
            if offset + bits > 64:
                next_word = (storage[word_index + 1] & ((1 << 64) - 1)) if word_index + 1 < len(storage) else 0
                value |= next_word << (64 - offset)
            result.append(value & mask)
        return result


def _decode_litematic(root: Compound) -> list[dict[str, Any]]:
    # 遍历 litematic Regions 下的全部区域，解析为统一的结构化体素块。
    regions = root.get("Regions") or Compound()
    result: list[dict[str, Any]] = []
    for name, region in regions.items():
        if not isinstance(region, dict):
            continue
        region_obj = LitematicaRegion(str(name or f"region{len(result)}"), region)
        if region_obj.size[0] <= 0 or region_obj.size[1] <= 0 or region_obj.size[2] <= 0:
            continue
        result.append(
            {
                "name": region_obj.name,
                "size": region_obj.size,
                "position": region_obj.position,
                "indices": region_obj.indices,
                "maskBits": region_obj.mask_bits,
                "palette": region_obj.block_palette,
            }
        )
    return result


def _decode_schematic(root: Compound, size_tag, blocks_tag) -> dict[str, Any]:
    # 解析 Sponge 格式 .schem：Palette 映射块名->索引，Blocks 为 z 优先坐标的联合数组。
    size = [int(v) for v in size_tag] if size_tag else []
    blocks = list(blocks_tag) if blocks_tag is not None else []
    if not size or len(size) != 3 or not blocks:
        raise GameServiceError("原理图缺少尺寸或方块数据", "SCHEMATIC_INVALID")
    palette = root.get("Palette") or Compound()
    color_map: list[dict[str, Any]] = [_block_palette_entry({"Name": "minecraft:air"})] * (len(palette) + 1)
    for key, value in palette.items():
        color_map[int(value)] = _block_palette_entry({"Name": str(key)})
    indices = [0] * len(blocks)
    for index, value in enumerate(blocks):
        indices[index] = int(value)
    return {"size": size, "indices": indices, "palette": color_map}


@dataclass(frozen=True, slots=True)
class _PackedRegion:
    name: str
    size: tuple[int, int, int]
    position: tuple[int, int, int]
    palette: list[dict[str, Any]]
    storage: Sequence[int]
    mask_bits: int | None = None

    def palette_index(self, offset: int) -> int:
        if self.mask_bits is None:
            return int(self.storage[offset]) if offset < len(self.storage) else 0
        bit_index = offset * self.mask_bits
        word_index, shift = divmod(bit_index, 64)
        if word_index >= len(self.storage):
            return 0
        word = int(self.storage[word_index]) & ((1 << 64) - 1)
        value = word >> shift
        if shift + self.mask_bits > 64 and word_index + 1 < len(self.storage):
            value |= (int(self.storage[word_index + 1]) & ((1 << 64) - 1)) << (64 - shift)
        return value & ((1 << self.mask_bits) - 1)


@dataclass(slots=True)
class SchematicSession:
    """
    保存一次原理图预览的只读分块快照。

    原始 NBT 在快照生成后立即释放；会话字典由游戏服务的共享锁保护，
    关闭弹窗、文件变化、过期或服务关闭时释放分块字节。
    """

    source_path: Path
    source_mtime_ns: int
    source_size: int
    created_at: float
    schematic_type: str
    size: tuple[int, int, int]
    palette: list[dict[str, Any]]
    material_counts: dict[str, int]
    chunks: dict[tuple[int, int, int], bytes]


class SchematicCoordinator:
    """
    解析原理图文件为适合 3D 预览的体素模型数据。

    支持 Litematica（.litematic）与 Sponge（.schem）两种主流格式，
    输出尺寸、调色板颜色与沿坐标轴排列的方块索引数组。
    """

    max_voxels = 262144
    max_axis = 1024
    stream_chunk_size = 16
    stream_max_voxels = 16_000_000
    stream_max_nbt_bytes = 64 * 1024 * 1024
    stream_max_chunks = 8192
    stream_session_seconds = 600
    max_asset_blocks = 512
    max_asset_bytes = 24 * 1024 * 1024
    asset_cache_version = 4

    def _schematic_root(self, game_path: Any, version_id: Any, resource_id: Any, version_isolation: Any) -> Path:
        target = self.resolve_instance(game_path, version_id, version_isolation)
        root = target.game_data_path / ResourceCatalogPolicy.directories["schematic"]
        return resolve_relative_id(root, resource_id)

    def schematic_preview(
        self,
        game_path: Any,
        version_id: Any,
        resource_id: Any,
        version_isolation: Any = False,
    ) -> dict[str, Any]:
        """
        读取并解析指定原理图，返回体素模型数据供前端渲染。

        :return: 包含 type/size/regions（名称、尺寸、位置、色板）的字典
        """
        path = self._schematic_root(game_path, version_id, resource_id, version_isolation)
        if not path.is_file():
            raise GameServiceError("原理图文件不存在", "SCHEMATIC_NOT_FOUND")
        try:
            root = load(path)
        except (OSError, ValueError, gzip.BadGzipFile) as exc:
            raise GameServiceError("原理图解析失败或格式不受支持", "SCHEMATIC_INVALID") from exc
        if "Regions" in root:
            regions = _decode_litematic(root)
            if not regions:
                raise GameServiceError("原理图中没有可解析的区域", "SCHEMATIC_EMPTY")
            return {
                "type": "litematic",
                "size": self._bounding_size(regions),
                "regions": [self._downsample_region(region) for region in regions],
            }
        if "Palette" in root or "Blocks" in root:
            payload = _decode_schematic(root, root.get("Size"), root.get("Blocks"))
            return {
                "type": "schem",
                "size": payload["size"],
                "regions": [
                    {
                        "name": "schem",
                        "size": payload["size"],
                        "position": [0, 0, 0],
                        "palette": payload["palette"],
                        "indices": payload["indices"],
                    }
                ],
            }
        raise GameServiceError("无法识别的原理图格式", "SCHEMATIC_INVALID")

    def schematic_session_open(
        self,
        game_path: Any,
        version_id: Any,
        resource_id: Any,
        version_isolation: Any = False,
    ) -> dict[str, Any]:
        """
        创建不降采样的原理图分块预览会话。

        解析和分块仅执行一次；返回轻量元数据，体素数据由后续分块调用读取。
        文件超出安全预算时明确失败，不返回不完整建筑。

        :param game_path: Minecraft 根目录
        :param version_id: 当前实例版本标识
        :param resource_id: 实例原理图目录中的相对文件名
        :param version_isolation: 实例隔离状态
        :return: 会话标识、尺寸、色板、方块统计及非空区块坐标
        :raises GameServiceError: 文件不存在、格式无效或超过安全预算时抛出
        """
        source_path = self._schematic_root(game_path, version_id, resource_id, version_isolation)
        if not source_path.is_file():
            raise GameServiceError("原理图文件不存在", "SCHEMATIC_NOT_FOUND")
        try:
            root = load_limited(source_path, self.stream_max_nbt_bytes)
        except ValueError as exc:
            if "安全上限" in str(exc):
                raise GameServiceError("原理图解压后过大，无法安全预览", "SCHEMATIC_TOO_LARGE") from exc
            raise GameServiceError("原理图解析失败或格式不受支持", "SCHEMATIC_INVALID") from exc
        except (OSError, gzip.BadGzipFile, EOFError) as exc:
            raise GameServiceError("原理图解析失败或格式不受支持", "SCHEMATIC_INVALID") from exc
        source_stat = source_path.stat()
        session = self._build_schematic_session(source_path, source_stat.st_mtime_ns, source_stat.st_size, root)
        session_id = secrets.token_hex(16)
        with self._lock:
            cutoff = time.monotonic() - self.stream_session_seconds
            expired = [key for key, value in self._schematic_sessions.items() if value.created_at < cutoff]
            for key in expired:
                self._schematic_sessions.pop(key, None)
            while len(self._schematic_sessions) >= 2:
                oldest = min(self._schematic_sessions, key=lambda key: self._schematic_sessions[key].created_at)
                self._schematic_sessions.pop(oldest)
            self._schematic_sessions[session_id] = session
        return {
            "sessionId": session_id,
            "type": session.schematic_type,
            "size": list(session.size),
            "chunkSize": self.stream_chunk_size,
            "palette": session.palette,
            "materialCounts": session.material_counts,
            "chunks": [list(coord) for coord in sorted(session.chunks)],
        }

    def schematic_session_chunks(self, session_id: str, coords: list[tuple[int, int, int]]) -> dict[str, Any]:
        """
        批量读取预览会话中的非空体素区块。

        返回固定 16³ 个小端无符号色板索引的 Base64 字节，避免巨大 JSON 整数数组。

        :param session_id: 打开预览时得到的会话标识
        :param coords: 本次请求的区块坐标，最多 24 个
        :return: 与请求顺序一致的区块字节集合
        :raises GameServiceError: 会话失效、文件变化或请求越界时抛出
        """
        if len(coords) > 24 or any(len(coord) != 3 or any(axis < 0 for axis in coord) for coord in coords):
            raise GameServiceError("原理图区块请求无效", "SCHEMATIC_CHUNK_INVALID")
        with self._lock:
            session = self._schematic_sessions.get(session_id)
            if session is None:
                raise GameServiceError("原理图预览会话已过期，请重新打开", "SCHEMATIC_SESSION_EXPIRED")
            if time.monotonic() - session.created_at > self.stream_session_seconds:
                self._schematic_sessions.pop(session_id, None)
                raise GameServiceError("原理图预览会话已过期，请重新打开", "SCHEMATIC_SESSION_EXPIRED")
            try:
                source_stat = session.source_path.stat()
            except OSError as exc:
                self._schematic_sessions.pop(session_id, None)
                raise GameServiceError("原理图文件已变化，请重新打开", "SCHEMATIC_SESSION_EXPIRED") from exc
            if source_stat.st_mtime_ns != session.source_mtime_ns or source_stat.st_size != session.source_size:
                self._schematic_sessions.pop(session_id, None)
                raise GameServiceError("原理图文件已变化，请重新打开", "SCHEMATIC_SESSION_EXPIRED")
            session.created_at = time.monotonic()
            chunks = session.chunks
            if any(coord not in chunks for coord in coords):
                raise GameServiceError("原理图区块坐标不存在", "SCHEMATIC_CHUNK_INVALID")
            return {
                "chunks": [
                    {"coord": list(coord), "indices": base64.b64encode(chunks[coord]).decode("ascii")}
                    for coord in coords
                ]
            }

    def schematic_session_close(self, session_id: str) -> dict[str, bool]:
        """
        释放一次原理图预览的分块缓存。

        重复关闭安全返回 false，不影响其他弹窗或游戏资源。

        :param session_id: 打开预览时得到的会话标识
        :return: 是否实际释放了会话
        """
        with self._lock:
            return {"closed": self._schematic_sessions.pop(session_id, None) is not None}

    def _packed_regions(self, root: Compound) -> tuple[str, list[_PackedRegion]]:
        """
        保留原始 NBT 的紧凑索引存储，避免把所有体素展开成 Python 整数列表。

        只解析区域尺寸和色板；实际索引在分块扫描时按位置读取。
        """
        regions: list[_PackedRegion] = []
        if "Regions" in root:
            for name, region in (root.get("Regions") or Compound()).items():
                if not isinstance(region, Mapping):
                    continue
                raw_size = _read_litematica_vector(region.get("Size"), "Size")
                raw_position = _read_litematica_vector(region.get("Position"), "Position")
                if any(axis == 0 for axis in raw_size):
                    raise GameServiceError("原理图区域尺寸不能为零", "SCHEMATIC_INVALID")
                size = tuple(abs(axis) for axis in raw_size)
                position = tuple(
                    origin if axis > 0 else origin + axis + 1
                    for origin, axis in zip(raw_position, raw_size, strict=True)
                )
                palette = [_block_palette_entry(item) for item in (region.get("BlockStatePalette") or List())]
                mask_bits = max(4, (max(1, len(palette)) - 1).bit_length())
                storage = region.get("BlockStates") or IntArray()
                regions.append(_PackedRegion(str(name), size, position, palette, storage, mask_bits))
            return "litematic", regions
        if "Palette" in root or "Blocks" in root:
            size_tag = root.get("Size")
            blocks_tag = root.get("Blocks")
            if not isinstance(size_tag, Sequence) or len(size_tag) != 3 or not isinstance(blocks_tag, Sequence):
                raise GameServiceError("原理图缺少尺寸或方块数据", "SCHEMATIC_INVALID")
            size = tuple(int(axis) for axis in size_tag)
            palette_tag = root.get("Palette") or Compound()
            if not isinstance(palette_tag, Mapping) or len(palette_tag) > 65535:
                raise GameServiceError("原理图方块色板无效", "SCHEMATIC_INVALID")
            palette = [_block_palette_entry({"Name": "minecraft:air"}) for _ in range(len(palette_tag) + 1)]
            for name, value in palette_tag.items():
                palette_index = int(value)
                if palette_index < 0 or palette_index >= len(palette):
                    raise GameServiceError("原理图方块色板无效", "SCHEMATIC_INVALID")
                palette[palette_index] = _block_palette_entry({"Name": str(name)})
            regions.append(_PackedRegion("schem", size, (0, 0, 0), palette, blocks_tag))
            return "schem", regions
        raise GameServiceError("无法识别的原理图格式", "SCHEMATIC_INVALID")

    def _build_schematic_session(
        self, source_path: Path, source_mtime_ns: int, source_size: int, root: Compound
    ) -> SchematicSession:
        """
        将紧凑 NBT 索引写入固定大小的非空区块，并保留准确材料统计。

        全部区域以真实坐标合并；后出现的非空气块覆盖同坐标旧块。
        """
        schematic_type, regions = self._packed_regions(root)
        if not regions:
            raise GameServiceError("原理图中没有可解析的区域", "SCHEMATIC_EMPTY")
        min_x = min(region.position[0] for region in regions)
        min_y = min(region.position[1] for region in regions)
        min_z = min(region.position[2] for region in regions)
        max_x = max(region.position[0] + region.size[0] for region in regions)
        max_y = max(region.position[1] + region.size[1] for region in regions)
        max_z = max(region.position[2] + region.size[2] for region in regions)
        size = (max_x - min_x, max_y - min_y, max_z - min_z)
        self._validate_stream_regions(regions, size)
        palette: list[dict[str, Any]] = [_block_palette_entry({"Name": "minecraft:air"})]
        palette_ids: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        counts: list[int] = [0]
        chunks: dict[tuple[int, int, int], array] = {}
        for region in regions:
            local_ids = self._session_palette_ids(region, palette, palette_ids, counts)
            self._fill_session_region(region, local_ids, (min_x, min_y, min_z), chunks, counts)
        material_counts: dict[str, int] = {}
        for entry, count in zip(palette, counts, strict=True):
            if count:
                name = str(entry["name"])
                material_counts[name] = material_counts.get(name, 0) + count
        if sys.byteorder != "little":
            for chunk in chunks.values():
                chunk.byteswap()
        return SchematicSession(
            source_path,
            source_mtime_ns,
            source_size,
            time.monotonic(),
            schematic_type,
            size,
            palette,
            material_counts,
            {coord: chunk.tobytes() for coord, chunk in chunks.items()},
        )

    def _validate_stream_regions(self, regions: list[_PackedRegion], size: tuple[int, int, int]) -> None:
        """
        在扫描体素前验证尺寸、存储完整性和整体安全预算。

        数据不完整与超预算区分错误码，避免伪装为部分成功。
        """
        total_cells = 0
        if any(axis > 8192 for axis in size):
            raise GameServiceError("原理图区域跨度过大", "SCHEMATIC_TOO_LARGE")
        for region in regions:
            width, height, depth = region.size
            if min(width, height, depth) <= 0:
                raise GameServiceError("原理图区域尺寸非法", "SCHEMATIC_INVALID")
            cells = width * height * depth
            total_cells += cells
            if total_cells > self.stream_max_voxels:
                raise GameServiceError("原理图超过 1600 万体素预览上限", "SCHEMATIC_TOO_LARGE")
            required = (cells * region.mask_bits + 63) // 64 if region.mask_bits else cells
            if len(region.storage) < required:
                raise GameServiceError("原理图方块数据不完整", "SCHEMATIC_INVALID")

    @staticmethod
    def _session_palette_ids(
        region: _PackedRegion,
        palette: list[dict[str, Any]],
        palette_ids: dict[tuple[str, tuple[tuple[str, str], ...]], int],
        counts: list[int],
    ) -> list[int]:
        """
        将区域局部色板映射到跨区域共享的色板索引。

        相同方块状态在多个区域复用一个索引，保留原始块 ID 和属性。
        """
        local_ids = [0] * len(region.palette)
        for local_index, entry in enumerate(region.palette):
            name = str(entry["name"])
            if _is_air_block(name):
                continue
            properties = entry["properties"]
            key = (name, tuple(sorted(properties.items())))
            palette_id = palette_ids.get(key)
            if palette_id is None:
                palette_id = len(palette)
                if palette_id > 65535:
                    raise GameServiceError("原理图方块状态过多", "SCHEMATIC_TOO_LARGE")
                palette_ids[key] = palette_id
                palette.append(entry)
                counts.append(0)
            local_ids[local_index] = palette_id
        return local_ids

    def _fill_session_region(
        self,
        region: _PackedRegion,
        local_ids: list[int],
        world_minimum: tuple[int, int, int],
        chunks: dict[tuple[int, int, int], array],
        counts: list[int],
    ) -> None:
        """
        将区域非空气块写入固定大小区块。

        后写区域覆盖同坐标时扣除旧块统计，保证材料数量准确。
        """
        width, height, depth = region.size
        origin_x, origin_y, origin_z = region.position
        min_x, min_y, min_z = world_minimum
        side = self.stream_chunk_size
        for offset in range(width * height * depth):
            local_index = region.palette_index(offset)
            if local_index >= len(local_ids):
                continue
            palette_id = local_ids[local_index]
            if palette_id == 0:
                continue
            x = origin_x + offset % width - min_x
            row = offset // width
            z = origin_z + row % depth - min_z
            y = origin_y + row // depth - min_y
            coord = (x // side, y // side, z // side)
            chunk = chunks.get(coord)
            if chunk is None:
                if len(chunks) >= self.stream_max_chunks:
                    raise GameServiceError("原理图区块过多，无法安全预览", "SCHEMATIC_TOO_LARGE")
                chunk = array("H", [0]) * (side**3)
                chunks[coord] = chunk
            chunk_index = ((y % side) * side + z % side) * side + x % side
            previous = chunk[chunk_index]
            if previous:
                counts[previous] -= 1
            chunk[chunk_index] = palette_id
            counts[palette_id] += 1

    def schematic_assets(
        self, game_path: Any, version_id: Any, blocks: list[str], version_isolation: Any = False, locale: str = "zh-CN"
    ) -> dict[str, Any]:
        """
        从用户本机游戏 Jar 按需读取原理图涉及的原版方块资源。

        提取结果仅包含请求色板使用的 blockstate、模型闭包和 PNG 纹理，并缓存到
        启动器数据目录；不会随程序分发或写入游戏目录。无法解析的模组方块会列入
        ``missingBlocks``，由前端使用回退渲染。

        :param game_path: Minecraft 根目录
        :param version_id: 当前实例版本标识
        :param blocks: 原理图调色板中的方块命名空间标识
        :param version_isolation: 实例隔离状态
        :return: 可直接传给 WebGL 渲染器的资源包
        :raises GameServiceError: 版本资源不存在、请求非法或 Jar 损坏时抛出
        """
        requested = self._normalize_asset_blocks(blocks)
        target = self.resolve_instance(game_path, version_id, version_isolation)
        jar_path = self._resolve_asset_jar(target.minecraft_root_path, target.instance_directory_name)
        cache_path = self._asset_cache_path(jar_path, requested)
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict):
            bundle = cached
        else:
            bundle = self._extract_assets(jar_path, requested)
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(cache_path, json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))
            except OSError:
                self.logger.warning("原理图资源缓存写入失败，将在下次预览重新提取")
        return {**bundle, "blockNames": self._read_block_names(target, jar_path, requested, locale)}

    def _read_block_names(
        self, target: ResolvedInstanceTarget, jar_path: Path, blocks: list[str], launcher_locale: str
    ) -> dict[str, str]:
        """
        按启动器界面语言读取方块名称，缺项回退客户端英文语言文件。

        现代 Minecraft 通常只在 Jar 内保留英文语言文件，其它语言由资产索引提供；
        因此先读取 Jar 英文，再用目标语言资产覆盖。仅接受索引给出的 SHA-1 对象路径，
        不读取资源包或自定义覆盖文件。
        """
        locale = self._minecraft_locale(launcher_locale)
        translations: dict[str, str] = {}
        try:
            with ZipFile(jar_path) as archive:
                english = self._read_json_asset(archive, "minecraft", "lang/en_us.json")
                if english:
                    translations.update({key: value for key, value in english.items() if isinstance(value, str)})
        except (BadZipFile, OSError):
            pass
        if locale != "en_us":
            translations.update(
                self._indexed_language(target.minecraft_root_path, target.instance_directory_name, locale)
            )
        names: dict[str, str] = {}
        for block in blocks:
            namespace, name = block.split(":", 1)
            label = translations.get(f"block.{namespace}.{name}")
            if isinstance(label, str) and 0 < len(label) <= 256:
                names[block] = label
        return names

    @staticmethod
    def _minecraft_locale(launcher_locale: str) -> str:
        """
        将受支持的启动器语言映射为 Minecraft 语言资源标识。

        IPC 模型已限制输入范围；此处仍以英文作为未知值回退，避免将未验证的文本
        拼接到资产索引路径中。
        """
        locale_by_launcher = {
            "zh-CN": "zh_cn",
            "zh-TW": "zh_tw",
            "en-US": "en_us",
            "ja-JP": "ja_jp",
            "ru-RU": "ru_ru",
            "de-DE": "de_de",
        }
        return locale_by_launcher.get(launcher_locale, "en_us")

    def export_schematic_material_manifest(
        self,
        game_path: Any,
        version_id: Any,
        session_id: str,
        output_path: Any,
        output_format: str,
        locale: str = "zh-CN",
        missing_blocks: list[str] | None = None,
        version_isolation: Any = False,
    ) -> dict[str, str]:
        """
        导出当前原理图预览会话的材料审计清单。

        导出前再次校验会话和源文件状态，避免文件变动后写出过期统计。方块名称
        始终跟随启动器语言；纹理状态由刚完成的资源提取结果传入，仅用于审计标记。

        :param game_path: Minecraft 根目录
        :param version_id: 当前实例版本标识
        :param session_id: 已打开的原理图会话标识
        :param output_path: 用户在系统对话框中选定的输出文件
        :param output_format: json 或 csv
        :param locale: 启动器当前界面语言
        :param missing_blocks: 没有可用纹理的方块标识
        :param version_isolation: 实例隔离状态
        :return: 实际写入的绝对路径
        :raises GameServiceError: 会话失效、资源缺失或格式不支持时抛出
        """
        with self._lock:
            session = self._schematic_sessions.get(session_id)
            if session is None:
                raise GameServiceError("原理图预览会话已过期，请重新打开", "SCHEMATIC_SESSION_EXPIRED")
            if time.monotonic() - session.created_at > self.stream_session_seconds:
                self._schematic_sessions.pop(session_id, None)
                raise GameServiceError("原理图预览会话已过期，请重新打开", "SCHEMATIC_SESSION_EXPIRED")
            try:
                source_stat = session.source_path.stat()
            except OSError as exc:
                self._schematic_sessions.pop(session_id, None)
                raise GameServiceError("原理图文件已变化，请重新打开", "SCHEMATIC_SESSION_EXPIRED") from exc
            if source_stat.st_mtime_ns != session.source_mtime_ns or source_stat.st_size != session.source_size:
                self._schematic_sessions.pop(session_id, None)
                raise GameServiceError("原理图文件已变化，请重新打开", "SCHEMATIC_SESSION_EXPIRED")
            session.created_at = time.monotonic()
            material_counts = dict(session.material_counts)
        blocks = sorted(material_counts)
        target = self.resolve_instance(game_path, version_id, version_isolation)
        jar_path = self._resolve_asset_jar(target.minecraft_root_path, target.instance_directory_name)
        names = self._read_block_names(target, jar_path, blocks, locale)
        missing = set(self._normalize_asset_blocks(missing_blocks)) if missing_blocks else set()
        materials = [
            {
                "id": block,
                "name": names.get(block, block),
                "count": material_counts[block],
                "hasTexture": block not in missing,
                "hasTranslation": block in names,
            }
            for block in blocks
        ]
        output = Path(str(output_path)).expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output_format == "json":
            content = json.dumps({"schemaVersion": 1, "materials": materials}, ensure_ascii=False, indent=2)
        elif output_format == "csv":
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=["id", "name", "count", "hasTexture", "hasTranslation"])
            writer.writeheader()
            writer.writerows(materials)
            content = stream.getvalue()
        else:
            raise GameServiceError("材料清单仅支持 JSON 或 CSV", "INVALID_MANIFEST_FORMAT")
        atomic_write_text(output, content)
        return {"path": str(output)}

    def _indexed_language(self, game_path: Path, version_id: str, locale: str) -> dict[str, str]:
        """
        按当前游戏资源索引读取指定语言的 Minecraft 资产对象。

        仅信任格式正确的 SHA-1 哈希；损坏或缺失的本地资源回退英文。
        """
        asset_index_id = self._asset_index_id(game_path, version_id)
        if not asset_index_id:
            return {}
        index_path = game_path / "assets" / "indexes" / f"{asset_index_id}.json"
        try:
            if index_path.stat().st_size > 8 * 1024 * 1024:
                return {}
            index = json.loads(index_path.read_text(encoding="utf-8"))
            asset_hash = index["objects"][f"minecraft/lang/{locale}.json"]["hash"]
            if not isinstance(asset_hash, str) or not re.fullmatch(r"[0-9a-f]{40}", asset_hash):
                return {}
            language_path = game_path / "assets" / "objects" / asset_hash[:2] / asset_hash
            if language_path.stat().st_size > 4 * 1024 * 1024:
                return {}
            language = json.loads(language_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        return (
            {key: value for key, value in language.items() if isinstance(value, str)}
            if isinstance(language, dict)
            else {}
        )

    @staticmethod
    def _asset_index_id(game_path: Path, version_id: str) -> str | None:
        """
        沿版本继承链查找受限格式的游戏资源索引标识。

        版本名与索引 ID 均限制为无路径分隔符的片段。
        """
        current = version_id
        visited: set[str] = set()
        for _ in range(16):
            if current in visited or not re.fullmatch(r"[a-zA-Z0-9._-]{1,255}", current):
                break
            visited.add(current)
            version_path = game_path / "versions" / current / f"{current}.json"
            try:
                payload = json.loads(version_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            if not isinstance(payload, dict):
                break
            asset_index = payload.get("assetIndex")
            if isinstance(asset_index, dict):
                index_id = asset_index.get("id")
                if isinstance(index_id, str) and re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", index_id):
                    return index_id
            parent = payload.get("inheritsFrom")
            if not isinstance(parent, str):
                break
            current = parent
        return None

    def _normalize_asset_blocks(self, blocks: list[str]) -> list[str]:
        """
        校验方块资源标识，阻止调用方构造 Jar 内路径穿越。

        :param blocks: 前端提供的方块标识列表
        :return: 去重、排序后的合法标识列表
        :raises GameServiceError: 标识数量或格式不符合约束时抛出
        """
        if len(blocks) > self.max_asset_blocks:
            raise GameServiceError("原理图方块类型过多", "SCHEMATIC_ASSETS_TOO_LARGE")
        normalized: set[str] = set()
        for block in blocks:
            if not isinstance(block, str) or ":" not in block:
                raise GameServiceError("原理图方块标识无效", "SCHEMATIC_ASSETS_INVALID")
            namespace, name = block.split(":", 1)
            if (
                not namespace.replace("_", "").replace("-", "").isalnum()
                or not name
                or any(part in {"", ".", ".."} for part in name.split("/"))
            ):
                raise GameServiceError("原理图方块标识无效", "SCHEMATIC_ASSETS_INVALID")
            normalized.add(f"{namespace}:{name}")
        if not normalized:
            raise GameServiceError("原理图没有可渲染方块", "SCHEMATIC_ASSETS_INVALID")
        return sorted(normalized)

    def _resolve_asset_jar(self, game_path: Path, version_id: str) -> Path:
        """
        沿版本继承链定位携带原版资源的客户端 Jar。

        :param game_path: Minecraft 根目录
        :param version_id: 起始版本标识
        :return: 可读取 ``assets/minecraft`` 的 Jar 路径
        :raises GameServiceError: 找不到 Jar 或继承链损坏时抛出
        """
        current = version_id
        visited: set[str] = set()
        for _ in range(16):
            if current in visited:
                break
            visited.add(current)
            version_dir = game_path / "versions" / current
            jar_path = version_dir / f"{current}.jar"
            if jar_path.is_file():
                return jar_path
            try:
                version_json = json.loads((version_dir / f"{current}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            parent = version_json.get("inheritsFrom") if isinstance(version_json, dict) else None
            if not isinstance(parent, str) or not parent:
                break
            current = parent
        raise GameServiceError("未找到当前版本的游戏资源 Jar，请先完成游戏安装", "SCHEMATIC_ASSETS_NOT_FOUND")

    def _asset_cache_path(self, jar_path: Path, blocks: list[str]) -> Path:
        stat = jar_path.stat()
        digest = hashlib.sha256(
            f"{self.asset_cache_version}:{jar_path}:{stat.st_mtime_ns}:{stat.st_size}:{','.join(blocks)}".encode()
        ).hexdigest()
        return self._data_path / "schematic-assets" / f"{digest}.json"

    def _extract_assets(self, jar_path: Path, blocks: list[str]) -> dict[str, Any]:
        """
        读取 Jar 中的 JSON 与 PNG 资源，并限制总输出体积。

        仅跟随请求方块引用的模型、父模型和纹理闭包；新版纹理描述对象会读取其
        ``sprite`` 字段，资源总量超过限制时中止，避免 IPC 返回过大的 Base64 数据。
        """
        blockstates: dict[str, Any] = {}
        models: dict[str, Any] = {}
        textures: dict[str, str] = {}
        animated: list[str] = []
        missing: set[str] = set()
        pending_models: set[str] = set()
        pending_textures: set[str] = set()
        total_bytes = 0
        try:
            with ZipFile(jar_path) as archive:
                for block in blocks:
                    namespace, name = block.split(":", 1)
                    payload = self._read_json_asset(archive, namespace, f"blockstates/{name}.json")
                    if payload is None:
                        missing.add(block)
                        continue
                    blockstates[block] = payload
                    pending_models.update(self._json_references(payload, "model", namespace))
                while pending_models:
                    model = pending_models.pop()
                    if model in models:
                        continue
                    namespace, name = model.split(":", 1)
                    payload = self._read_json_asset(archive, namespace, f"models/{name}.json")
                    if payload is None:
                        continue
                    models[model] = payload
                    pending_models.update(self._json_references(payload, "parent", namespace))
                    pending_textures.update(self._json_references(payload, "texture", namespace))
                for block in blocks:
                    pending_textures.update(self._special_texture_ids(block))
                for texture in pending_textures:
                    namespace, name = texture.split(":", 1)
                    try:
                        data = archive.read(f"assets/{namespace}/textures/{name}.png")
                    except KeyError:
                        continue
                    total_bytes += len(data)
                    if total_bytes > self.max_asset_bytes:
                        raise GameServiceError("原理图纹理资源过大", "SCHEMATIC_ASSETS_TOO_LARGE")
                    textures[texture] = base64.b64encode(data).decode("ascii")
                    metadata = self._read_json_asset(archive, namespace, f"textures/{name}.png.mcmeta")
                    if metadata is not None and isinstance(metadata.get("animation"), dict):
                        animated.append(texture)
        except (BadZipFile, OSError) as exc:
            raise GameServiceError("游戏资源 Jar 无法读取", "SCHEMATIC_ASSETS_INVALID") from exc
        return {
            "blockstates": blockstates,
            "models": models,
            "textures": textures,
            "animated": sorted(animated),
            "missingBlocks": sorted(missing),
        }

    @staticmethod
    def _special_texture_ids(block: str) -> set[str]:
        """
        补齐特殊方块渲染器直接引用、无法从模型继承链发现的纹理。

        仅生成固定的原版资源路径；资源不存在时由常规缺失纹理回退。
        """
        namespace, name = block.split(":", 1)
        if namespace != "minecraft":
            return set()
        if name in {"water", "lava"}:
            return {f"minecraft:block/{name}_still", f"minecraft:block/{name}_flow"}
        chest_types = {
            "chest": "normal",
            "trapped_chest": "trapped",
            "ender_chest": "ender",
            "copper_chest": "copper",
            "exposed_copper_chest": "copper_exposed",
            "weathered_copper_chest": "copper_weathered",
            "oxidized_copper_chest": "copper_oxidized",
        }
        normalized_chest = name.removeprefix("waxed_")
        if normalized_chest in chest_types:
            return {f"minecraft:entity/chest/{chest_types[normalized_chest]}"}
        for suffix in ("_wall_hanging_sign", "_hanging_sign", "_wall_sign", "_sign"):
            if name.endswith(suffix):
                wood = name.removesuffix(suffix)
                return {f"minecraft:entity/signs/{wood}", f"minecraft:entity/signs/hanging/{wood}"}
        if name.endswith("_shulker_box"):
            color = name.removesuffix("_shulker_box")
            return {f"minecraft:entity/shulker/shulker_{color}"}
        if name.endswith("_bed"):
            return {f"minecraft:entity/bed/{name.removesuffix('_bed')}"}
        if name.endswith("_banner"):
            color = name.removesuffix("_wall_banner").removesuffix("_banner")
            return {f"minecraft:entity/banner/{color}"}
        return set()

    @staticmethod
    def _read_json_asset(archive: ZipFile, namespace: str, path: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(archive.read(f"assets/{namespace}/{path}").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _json_references(value: Any, key: str, namespace: str) -> set[str]:
        references: set[str] = set()
        if isinstance(value, dict):
            for item_key, item in value.items():
                if item_key == key and isinstance(item, str) and not item.startswith("#"):
                    references.add(item if ":" in item else f"{namespace}:{item}")
                elif key == "texture" and item_key == "textures" and isinstance(item, dict):
                    for texture in item.values():
                        texture_ref = texture.get("sprite") if isinstance(texture, Mapping) else texture
                        if isinstance(texture_ref, str) and not texture_ref.startswith("#"):
                            references.add(texture_ref if ":" in texture_ref else f"{namespace}:{texture_ref}")
                else:
                    references.update(SchematicCoordinator._json_references(item, key, namespace))
        elif isinstance(value, list):
            for item in value:
                references.update(SchematicCoordinator._json_references(item, key, namespace))
        return references

    @staticmethod
    def _bounding_size(regions: list[dict[str, Any]]) -> list[int]:
        # 计算包含全部区域的位置关系的整体包围尺寸。
        max_x = max_y = max_z = 0
        for region in regions:
            position = region.get("position") or [0, 0, 0]
            size = region.get("size") or [0, 0, 0]
            max_x = max(max_x, position[0] + size[0])
            max_y = max(max_y, position[1] + size[1])
            max_z = max(max_z, position[2] + size[2])
        return [max(1, max_x), max(1, max_y), max(1, max_z)]

    def _downsample_region(self, region: dict[str, Any]) -> dict[str, Any]:
        # 体素总量超过上限时沿各轴均匀降采样，仍需保证每轴不长于 max_axis。
        size = region["size"]
        sx, sy, sz = size[0], size[1], size[2]
        if sx <= 0 or sy <= 0 or sz <= 0:
            raise GameServiceError("原理图区域尺寸非法", "SCHEMATIC_INVALID")
        scale_x = max(1, (sx + self.max_axis - 1) // self.max_axis)
        scale_y = max(1, (sy + self.max_axis - 1) // self.max_axis)
        scale_z = max(1, (sz + self.max_axis - 1) // self.max_axis)
        while sx * sy * sz // (scale_x * scale_y * scale_z) > self.max_voxels:
            if scale_x >= scale_y and scale_x >= scale_z:
                scale_x += 1
            elif scale_y >= scale_z:
                scale_y += 1
            else:
                scale_z += 1
        # 当降采样因子超过 1 时，线性取步进位置的索引，避免连接临近体素产生非线性形变。
        step_x, step_y, step_z = scale_x, scale_y, scale_z
        ax = (sx + scale_x - 1) // scale_x
        ay = (sy + scale_y - 1) // scale_y
        az = (sz + scale_z - 1) // scale_z
        indices = region["indices"]
        sampled: list[int] = []
        for z in range(0, sz, step_z):
            for y in range(0, sy, step_y):
                for x in range(0, sx, step_x):
                    index = (y * sz + z) * sx + x
                    sampled.append(indices[index])
        result_region = {
            "name": region["name"],
            "size": [ax, ay, az],
            "position": region["position"],
            "palette": region["palette"],
            "indices": sampled,
        }
        return result_region
