# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：识别世界种子存储位置，校验精确整数并协调多文件原子更新。
#
# 公开接口：
#   - class WorldSeedSource — 保存已识别种子及其 NBT 写入位置。
#   - class WorldSeedStore — 读取种子、校验输入并串行提交存档修改。
# ============================================================

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from ECL.game import Compound, File, Long, load_limited

from .base import GameServiceError


@dataclass(frozen=True, slots=True)
class WorldSeedSource:
    """
    保存单次请求解析得到的种子位置与读取状态。

    NBT 节点仅归当前请求所有；外部文件由提交协调器统一落盘。
    """

    value: int | None = None
    node: Compound | None = None
    key: str = ""
    file_path: Path | None = None
    document: File | None = None
    error: str = ""


class WorldSeedStore:
    """
    按实际 NBT 结构读取种子并保持精确的有符号 64 位值。

    有界分片锁供读写共同使用，同一存档的并发请求不会交叉替换文件。
    """

    min_seed: int = -(2**63)
    max_seed: int = 2**63 - 1
    max_safe_number: int = 2**53 - 1
    max_nbt_bytes: int = 16 * 1024 * 1024
    _world_locks: tuple[RLock, ...] = tuple(RLock() for _ in range(32))

    @classmethod
    def lock_for(cls, world_path: Path) -> RLock:
        """
        返回存档的有界分片锁，供整个读取或备份写入事务持有。

        :param world_path: 已经通过目录边界校验的世界目录
        :return: 可重入锁；哈希冲突只会串行化额外的世界请求
        """
        return cls._world_locks[hash(str(world_path.resolve()).casefold()) % len(cls._world_locks)]

    @classmethod
    def parse_input(cls, value: object) -> int:
        """
        校验文本种子与旧安全整数输入，不通过浮点数转换。

        :param value: 来自 IPC 或内部服务调用的种子值
        :return: 范围内的精确整数
        :raises GameServiceError: 输入非法、数值不安全或越界
        """
        if type(value) is int and abs(value) <= cls.max_safe_number:
            seed = value
        elif isinstance(value, str) and len(value) <= 20 and re.fullmatch(r"-?[0-9]+", value):
            seed = int(value)
        else:
            raise GameServiceError("种子必须使用完整的十进制整数文本", "INVALID_WORLD_SEED")
        if not cls.min_seed <= seed <= cls.max_seed:
            raise GameServiceError("种子超出有符号 64 位整数范围", "INVALID_WORLD_SEED")
        return seed

    @classmethod
    def read(cls, world_path: Path, level_data: Compound) -> WorldSeedSource:
        """
        从独立、现代内嵌或旧式字段识别种子，异常时返回不可编辑状态。

        不回退到损坏现代来源旁的旧字段，不修改任何原始文档。

        :param world_path: 当前世界目录
        :param level_data: 当前请求已经读取的 level.dat 数据节点
        :return: 精确种子和原节点，或带原因的不可用来源
        """
        settings_file = world_path / "data" / "minecraft" / "world_gen_settings.dat"
        if not settings_file.resolve().is_relative_to(world_path.resolve()):
            return WorldSeedSource(error="世界种子文件超出存档目录")
        if settings_file.exists() or settings_file.is_symlink():
            try:
                document = load_limited(settings_file, cls.max_nbt_bytes)
                node = document.get("data")
            except (OSError, ValueError, EOFError, RecursionError, struct.error):
                return WorldSeedSource(error="世界种子文件损坏或不可读取")
            return cls._source(node, "seed", settings_file, document)
        if "WorldGenSettings" in level_data:
            return cls._source(level_data["WorldGenSettings"], "seed")
        return cls._source(level_data, "RandomSeed")

    @classmethod
    def _source(
        cls, node: object, key: str, file_path: Path | None = None, document: File | None = None
    ) -> WorldSeedSource:
        if not isinstance(node, Compound):
            return WorldSeedSource(error="未识别世界种子结构")
        value = node.get(key)
        if not isinstance(value, Long) or not cls.min_seed <= value <= cls.max_seed:
            return WorldSeedSource(error="世界种子缺失或类型无效")
        return WorldSeedSource(int(value), node, key, file_path, document)

    @classmethod
    def set_value(cls, source: WorldSeedSource, value: object) -> None:
        """
        在当前请求的文档中修改已识别种子，落盘交给事务提交。

        :param source: 从当前世界解析的有效来源
        :param value: 完整十进制种子文本或安全整数
        :raises GameServiceError: 输入非法或原始种子不可用
        """
        seed = cls.parse_input(value)
        if source.node is None or source.value is None:
            raise GameServiceError(source.error or "无法读取世界种子", "WORLD_SEED_UNAVAILABLE")
        if source.key == "seed":
            cls._update_vanilla_seed_references(source.node, source.value, seed)
        source.node[source.key] = Long(seed)

    @staticmethod
    def _update_vanilla_seed_references(settings: Compound, old_seed: int, new_seed: int) -> None:
        """
        同步 1.16 原版生成器保存的种子引用，不改写自定义维度配置。

        只处理原版噪声生成器和已知生物群系源中与世界旧种子相同的 Long
        标签，保留有意设置的不同种子及模组私有字段。
        """
        dimensions = settings.get("dimensions")
        if not isinstance(dimensions, Compound):
            return
        for dimension_id in ("minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"):
            dimension = dimensions.get(dimension_id)
            if not isinstance(dimension, Compound):
                continue
            generator = dimension.get("generator")
            if not isinstance(generator, Compound) or generator.get("type") != "minecraft:noise":
                continue
            if isinstance(generator.get("seed"), Long) and generator["seed"] == old_seed:
                generator["seed"] = Long(new_seed)
            biome_source = generator.get("biome_source")
            if (
                isinstance(biome_source, Compound)
                and biome_source.get("type")
                in ("minecraft:vanilla_layered", "minecraft:multi_noise", "minecraft:the_end")
                and isinstance(biome_source.get("seed"), Long)
                and biome_source["seed"] == old_seed
            ):
                biome_source["seed"] = Long(new_seed)

    @staticmethod
    def commit(documents: list[tuple[Path, File]]) -> None:
        """
        预写所有 NBT 文件，再替换目标；中途失败回滚已经替换的原内容。

        调用者必须持有存档锁并完成路径、占用和备份检查。失败时保留
        自动备份；回滚也失败时保留回滚文件以供恢复。

        :param documents: 当前事务中已校验的目标文件与请求私有文档
        :raises OSError: 写入或回滚失败
        """
        prepared: list[tuple[Path, Path, Path]] = []
        replaced: list[tuple[Path, Path]] = []
        try:
            for destination, document in documents:
                temporary_file = destination.with_name(f".{destination.name}.ecl-tmp")
                rollback_file = destination.with_name(f".{destination.name}.ecl-rollback")
                if rollback_file.exists() or rollback_file.is_symlink():
                    raise OSError("存在待恢复的存档事务文件，请先恢复备份")
                if temporary_file.exists() or temporary_file.is_symlink():
                    raise OSError("存在未完成的存档临时文件，请先检查存档状态")
                prepared.append((destination, temporary_file, rollback_file))
                rollback_file.write_bytes(destination.read_bytes())
                document.save(temporary_file, gzipped=True)
            for destination, temporary_file, rollback_file in prepared:
                temporary_file.replace(destination)
                replaced.append((destination, rollback_file))
        except Exception as error:
            rollback_failed = False
            for destination, rollback_file in reversed(replaced):
                try:
                    rollback_file.replace(destination)
                except OSError as rollback_error:
                    rollback_failed = True
                    error.add_note(f"回滚失败，保留恢复文件：{type(rollback_error).__name__}")
            if not rollback_failed:
                for _, _, rollback_file in prepared:
                    rollback_file.unlink(missing_ok=True)
            raise
        finally:
            for _, temporary_file, _ in prepared:
                temporary_file.unlink(missing_ok=True)
        for _, _, rollback_file in prepared:
            rollback_file.unlink(missing_ok=True)
