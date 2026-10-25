# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：整合包导出文件扫描、分类及执行前身份校验，不拥有后台任务或修改源文件。
#
# 公开接口：
#   - class ExportFile — 保存可导出文件的相对身份和扫描时属性。
#   - class ExportSnapshot — 保存一次导出预览的目录身份与可选清单。
#   - class ExportContentPolicy — 统一导出分类、默认选择和禁止内容规则。
#   - class ExportMod — 参与在线反查的已选模组数据。
#   - class OnlineExportFile — 标准整合包的在线文件条目。
#   - scan_export_files — 在实际游戏目录内收集有界且不跟随链接的导出预览。
#   - export_source_revision — 读取目录与启动描述身份，确认依赖解析期间来源未改变。
# ============================================================

from __future__ import annotations

import hashlib
import json
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from os import fstat, stat_result
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, TypedDict

from pydantic import JsonValue

from ECL.services.operations import OperationContext

from .base import GameServiceError
from .workspace import ResolvedInstanceTarget


class ExportMod(TypedDict):
    """
    保存所选模组的反查指纹，不接收前端提供的文件路径。
    """

    relative: str
    path: Path
    sha1: str
    sha512: str
    size: int
    fingerprint: int


class OnlineExportFile(TypedDict):
    """
    保存写入 mrpack 清单的已确认在线文件。
    """

    path: str
    hashes: dict[str, str]
    env: dict[str, str]
    downloads: list[str]
    fileSize: int


ExportCategory = Literal[
    "mods", "disabledMods", "config", "settings", "resources", "shaders", "saves", "screenshots", "servers", "other"
]


class ExportContentPolicy:
    """
    集中定义导出文件边界、分类和默认勾选，不由前端推测允许读取的目录。
    """

    max_files = 50000
    max_entries = 150000
    max_depth = 32
    max_manifest_bytes = 8 * 1024 * 1024
    blocked_directories = frozenset(
        {
            "assets",
            "libraries",
            "versions",
            "logs",
            "crash-reports",
            "natives",
            "runtime",
            "runtimes",
            ".git",
            ".fabric",
            ".connector",
            ".cache",
            "cache",
            "caches",
            "webcache",
            "ecl_data",
            "pcl",
            "hmcl",
            "accounts",
            "accountsx",
            "credentials",
            "avatar-cache",
            "cosmetic-cache",
            "mcef-cache",
            "mcef-libraries",
            "structurecachev1",
        }
    )
    blocked_files = frozenset(
        {
            "ecl.json",
            "setting.json",
            "launcher_accounts.json",
            "launcher_profiles.json",
            "accounts.json",
            "microsoft_accounts.json",
            "credentials.json",
            "authentication.json",
            "session.lock",
            "instance.cfg",
            "mmc-pack.json",
            "modrinth.index.json",
            "dev_channel.json",
        }
    )
    config_directories = frozenset({"config", "defaultconfigs", "kubejs", "scripts", "configureddefaults"})
    resource_directories = frozenset({"resourcepacks", "texturepacks", "datapacks", "resources", "global_packs"})
    recommended_categories = frozenset({"mods", "config", "settings", "resources", "shaders"})

    @staticmethod
    def is_link(attributes: stat_result) -> bool:
        """
        在 Windows 检查重解析点，在 POSIX 检查符号链接；调用方传入 lstat 结果。

        :param attributes: 未跟随链接的文件状态
        :return: 是否为不可遍历的链接或重解析点
        """
        # stat_result 的 Windows 属性只在对应平台读取，避免导入时依赖平台字段。
        return stat.S_ISLNK(attributes.st_mode) or (
            sys.platform == "win32" and bool(attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        )

    @classmethod
    def allows(cls, relative: PurePosixPath, instance_name: str) -> bool:
        """
        拒绝运行依赖、凭据和实例自身启动描述；目录勾选不能绕过此规则。

        :param relative: 已限定在实际游戏目录内的相对路径
        :param instance_name: 实例磁盘名称
        :return: 是否允许出现在导出候选清单
        """
        parts = tuple(part.casefold() for part in relative.parts)
        if any(part in cls.blocked_directories for part in parts[:-1]):
            return False
        name = parts[-1]
        if name in cls.blocked_files or name in {f"{instance_name.casefold()}.json", f"{instance_name.casefold()}.jar"}:
            return False
        return not (
            name.endswith((".mrpack", ".ecl-tmp", ".key", ".pem", ".p12", ".pfx", ".log", ".log.gz"))
            or name.startswith(".env")
        )

    @classmethod
    def category(cls, relative: PurePosixPath) -> ExportCategory:
        """
        按真实相对路径对允许文件分类，未知目录默认需要用户显式选择。

        :param relative: 已通过导出边界检查的相对路径
        :return: 统一的文件分类
        """
        first = relative.parts[0].casefold()
        if first == "mods":
            if relative.name.casefold().endswith((".disabled", ".old")):
                return "disabledMods"
            return "mods"
        if first in cls.config_directories:
            return "config"
        if first in cls.resource_directories:
            return "resources"
        if first == "shaderpacks":
            return "shaders"
        if first in {"saves", "screenshots"}:
            return "saves" if first == "saves" else "screenshots"
        if first == "servers.dat":
            return "servers"
        if len(relative.parts) == 1 and first in {"options.txt", "optionsof.txt", "optionsshaders.txt"}:
            return "settings"
        return "other"


@dataclass(frozen=True, slots=True)
class ExportFile:
    """
    绑定扫描时文件身份，读取时重新检查目录边界与打开的实际文件。
    """

    file_id: str
    source_path: Path
    relative: str
    size: int
    modified_ns: int
    device: int
    inode: int
    category: ExportCategory

    def validate(self, root: Path) -> None:
        """
        逐级拒绝新出现的链接，并拒绝已修改、消失或越界的文件。

        :param root: 预览绑定的实际游戏目录
        :raises GameServiceError: 文件与预览不一致时要求刷新
        """
        try:
            current = root
            if ExportContentPolicy.is_link(current.lstat()):
                raise OSError("来源目录已变为链接")
            for part in PurePosixPath(self.relative).parts:
                current /= part
                if ExportContentPolicy.is_link(current.lstat()):
                    raise OSError("文件路径已变为链接")
            if current.resolve(strict=True) != self.source_path or root not in current.resolve().parents:
                raise OSError("文件不在原来源内")
            attributes = current.stat()
            if not self.matches(attributes):
                raise OSError("文件属性已变化")
        except OSError as exc:
            raise GameServiceError("导出文件已变化，请刷新文件列表后重试", "EXPORT_PREVIEW_STALE") from exc

    def matches(self, attributes: stat_result) -> bool:
        """
        比较打开文件或路径的身份及内容修改属性。

        :param attributes: 路径状态或已打开文件描述符的状态
        :return: 是否仍为扫描时的普通文件
        """
        return (
            stat.S_ISREG(attributes.st_mode)
            and attributes.st_size == self.size
            and attributes.st_mtime_ns == self.modified_ns
            and attributes.st_dev == self.device
            and attributes.st_ino == self.inode
        )


@dataclass(frozen=True, slots=True)
class ExportSnapshot:
    """
    保存服务器重新构建的导出清单，不依赖客户端提交的绝对路径或分类。
    """

    root: Path
    source_revision: str
    target: ResolvedInstanceTarget
    dependencies: Mapping[str, str]
    files: tuple[ExportFile, ...]
    excluded_count: int

    def validate_source(self) -> None:
        """
        提交输出前确认目录及实例启动描述仍与已确认来源一致。

        :raises GameServiceError: 来源目录或启动元数据变化时抛出
        """
        if export_source_revision(self.target, self.dependencies) != self.source_revision:
            raise GameServiceError("实例导出信息已变化，请刷新后重试", "EXPORT_PREVIEW_STALE")

    def payload(self) -> dict[str, JsonValue]:
        """
        返回只含相对文件身份的预览，不暴露可被客户端改写的源路径。

        :return: 后台预览操作的可序列化结果
        """
        files: list[JsonValue] = [
            {
                "fileId": entry.file_id,
                "path": entry.relative,
                "size": entry.size,
                "category": entry.category,
                "recommended": entry.category in ExportContentPolicy.recommended_categories,
            }
            for entry in self.files
        ]
        dependencies: dict[str, JsonValue] = dict(self.dependencies)
        return {
            "sourceRevision": self.source_revision,
            "versionId": self.target.instance_directory_name,
            "isolated": self.target.version_isolation,
            "dependencies": dependencies,
            "files": files,
            "excludedCount": self.excluded_count,
        }

    def selection(self, revision: str, file_ids: list[str]) -> tuple[ExportFile, ...]:
        """
        只接受当前清单中的唯一文件 ID；空选择表示仅导出游戏依赖。

        :param revision: 用户确认的来源版本
        :param file_ids: 用户勾选的有界服务器文件 ID
        :return: 按路径稳定排序的当前可导出文件
        :raises GameServiceError: 来源或所选文件身份变化时抛出
        """
        selected_ids = frozenset(file_ids)
        if len(selected_ids) != len(file_ids) or len(file_ids) > ExportContentPolicy.max_files:
            raise GameServiceError("导出文件选择无效", "INVALID_EXPORT_SELECTION")
        files_by_id = {entry.file_id: entry for entry in self.files}
        if revision != self.source_revision or not selected_ids.issubset(files_by_id):
            raise GameServiceError("导出文件已变化，请刷新文件列表后重试", "EXPORT_PREVIEW_STALE")
        return tuple(entry for entry in self.files if entry.file_id in selected_ids)


def export_source_revision(target: ResolvedInstanceTarget, dependencies: Mapping[str, str]) -> str:
    """
    绑定实际目录和有界的启动描述，在读取元数据时拒绝链接与并发修改。

    :param target: 已解析且不允许链接的实例来源
    :param dependencies: 本次身份绑定的游戏和加载器依赖
    :return: 可复核的来源身份摘要
    :raises GameServiceError: 来源消失、变化或启动描述超限时抛出
    """
    root = target.game_data_path
    manifest = target.instance_path / f"{target.instance_directory_name}.json"
    try:
        if ExportContentPolicy.is_link(root.lstat()) or root.resolve(strict=True) != root:
            raise OSError("来源目录发生变化")
        attributes = manifest.lstat()
        if ExportContentPolicy.is_link(attributes) or not stat.S_ISREG(attributes.st_mode):
            raise OSError("启动描述不是普通文件")
        if attributes.st_size > ExportContentPolicy.max_manifest_bytes:
            raise GameServiceError("实例启动描述过大", "EXPORT_MANIFEST_TOO_LARGE")
        with manifest.open("rb") as stream:
            opened = fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (attributes.st_dev, attributes.st_ino):
                raise OSError("启动描述已被替换")
            content = stream.read(ExportContentPolicy.max_manifest_bytes + 1)
            finished = fstat(stream.fileno())
        current = manifest.lstat()
        if (
            ExportContentPolicy.is_link(current)
            or (current.st_dev, current.st_ino, current.st_mtime_ns)
            != (attributes.st_dev, attributes.st_ino, attributes.st_mtime_ns)
            or finished.st_mtime_ns != attributes.st_mtime_ns
            or len(content) != attributes.st_size
        ):
            raise OSError("启动描述发生变化")
    except OSError as exc:
        raise GameServiceError("实例启动描述或来源目录已变化，请刷新后重试", "EXPORT_PREVIEW_STALE") from exc
    identity = [
        str(root),
        str(target.instance_path),
        target.version_isolation,
        dict(dependencies),
        hashlib.sha256(content).hexdigest(),
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _is_sibling_instance(directory: Path, target: ResolvedInstanceTarget, depth: int) -> bool:
    """
    判断游戏根下的一级子目录是否属于同一游戏根的另一个实例。

    非隔离实例共用游戏根，其他实例目录及其启动描述不能进入导出清单，
    否则会把其他实例的存档与配置打进整合包。只在游戏根的直接子目录上判断，
    隔离实例各自拥有独立数据目录，不存在同级实例。

    :param directory: 当前遍历到的子目录
    :param target: 按有效隔离策略解析的实例目标
    :param depth: 该目录相对游戏根的层级，0 表示游戏根的直接子目录
    :return: 属于其他实例目录时为 True
    """
    if target.version_isolation or depth != 0:
        return False
    return directory == target.instance_path or (directory / f"{directory.name}.json").is_file()


def _is_exportable_file(attributes: stat_result, relative: PurePosixPath, instance_directory_name: str) -> bool:
    """
    判断扫描到的普通文件是否属于允许导出的内容。

    链接与目录在此之前已被排除；这里只放行普通文件，并交给统一的分类规则
    判定路径是否落在禁止导出的范围外。

    :param attributes: 该文件不跟随链接的 lstat 结果
    :param relative: 相对游戏数据目录的 POSIX 路径
    :param instance_directory_name: 所属实例目录名，用于排除实例自身元信息
    :return: 可以进入导出清单时为 True
    """
    if not stat.S_ISREG(attributes.st_mode):
        return False
    return ExportContentPolicy.allows(relative, instance_directory_name)


def _build_export_file(
    child: Path,
    relative: PurePosixPath,
    attributes: stat_result,
    revision: str,
    existing_count: int,
) -> ExportFile:
    """
    为允许导出的文件生成条目，并校验清单数量上限。

    文件身份由来源修订与扫描属性共同决定，执行阶段据此确认文件未被替换。

    :param child: 文件的绝对路径
    :param relative: 相对游戏数据目录的 POSIX 路径
    :param attributes: 该文件不跟随链接的 lstat 结果
    :param revision: 本次预览的来源修订
    :param existing_count: 已收集的条目数量
    :return: 带服务器文件身份的导出条目
    :raises GameServiceError: 条目数量超过上限时抛出
    """
    if existing_count >= ExportContentPolicy.max_files:
        raise GameServiceError("可导出文件超过 50000 个，请缩小内容范围", "EXPORT_SCAN_LIMIT")
    file_identity = [
        revision,
        str(relative),
        attributes.st_size,
        attributes.st_mtime_ns,
        attributes.st_dev,
        attributes.st_ino,
    ]
    file_id = hashlib.sha256(json.dumps(file_identity).encode()).hexdigest()
    return ExportFile(
        file_id,
        child,
        str(relative),
        attributes.st_size,
        attributes.st_mtime_ns,
        attributes.st_dev,
        attributes.st_ino,
        ExportContentPolicy.category(relative),
    )


def scan_export_files(
    target: ResolvedInstanceTarget, dependencies: Mapping[str, str], context: OperationContext
) -> ExportSnapshot:
    """
    在实际游戏数据目录中扫描候选文件，预览与执行共用相同分类和边界。

    :param target: 按有效隔离策略解析的实例目标
    :param dependencies: 来自该实例启动描述的游戏与加载器依赖
    :param context: 所属后台操作的取消与进度上下文
    :return: 不跟随链接且文件数量有界的导出快照
    :raises GameServiceError: 来源不存在、读取失败或扫描超限时抛出
    """
    root = target.game_data_path
    if not root.is_dir():
        raise GameServiceError("实例游戏数据目录不存在", "INSTANCE_DATA_NOT_FOUND")
    revision = export_source_revision(target, dependencies)
    entries: list[ExportFile] = []
    excluded = 0
    visited = 0
    pending: list[tuple[Path, int]] = [(root, 0)]
    try:
        while pending:
            context.check_cancelled()
            directory, depth = pending.pop()
            if depth > ExportContentPolicy.max_depth:
                raise GameServiceError("导出目录层级过深，请缩小内容范围", "EXPORT_SCAN_LIMIT")
            for child in directory.iterdir():
                context.check_cancelled()
                visited += 1
                if visited > ExportContentPolicy.max_entries:
                    raise GameServiceError("导出内容过多，请缩小内容范围", "EXPORT_SCAN_LIMIT")
                attributes = child.lstat()
                if ExportContentPolicy.is_link(attributes):
                    excluded += 1
                    continue
                relative = PurePosixPath(child.relative_to(root).as_posix())
                if stat.S_ISDIR(attributes.st_mode):
                    if child.name.casefold() in ExportContentPolicy.blocked_directories or _is_sibling_instance(
                        child, target, depth
                    ):
                        excluded += 1
                        continue
                    pending.append((child, depth + 1))
                    continue
                if not _is_exportable_file(attributes, relative, target.instance_directory_name):
                    excluded += 1
                    continue
                entries.append(_build_export_file(child, relative, attributes, revision, len(entries)))
                if visited % 200 == 0:
                    context.progress(10, "正在扫描可导出文件", done=len(entries), progressType="files")
    except OSError as exc:
        raise GameServiceError("读取导出文件失败，请检查文件是否仍存在或被占用", "EXPORT_SCAN_FAILED") from exc
    return ExportSnapshot(
        root,
        revision,
        target,
        MappingProxyType(dict(dependencies)),
        tuple(sorted(entries, key=lambda item: item.relative)),
        excluded,
    )
