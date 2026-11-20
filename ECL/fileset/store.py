# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：用户媒体文件集，按内容寻址保存图片与视频并维护元数据索引。
#
# 公开接口：
#   - class FileSetError — 文件集操作失败时的领域异常。
#   - class FileSetItem — 文件集条目的不可变快照。
#   - class FileSetStore — 导入、重命名、删除与定位媒体条目。
# ============================================================

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from ECL.utils.files import atomic_write_bytes, atomic_write_text

logger = logging.getLogger("EuoraCraft-Launcher.FileSet")


class FileSetError(Exception):
    """
    文件集操作失败，携带稳定的错误码供 IPC 层转换。

    :param message: 面向用户的中文说明
    :param code: 稳定错误码，形如 ``FILESET_FILE_TOO_LARGE``
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True, slots=True)
class FileSetItem:
    """
    描述一个已入库的媒体条目，不包含本地绝对路径。

    条目以内容摘要标识文件，重命名只改显示名，不影响磁盘文件。
    """

    id: str
    kind: str
    name: str
    sha256: str
    extension: str
    byte_size: int
    width: int | None
    height: int | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        """
        转换为可直接交给前端的字典，键名沿用持久化格式。

        :return: 带驼峰风格键名的条目字典
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "sha256": self.sha256,
            "extension": self.extension,
            "byteSize": self.byte_size,
            "width": self.width,
            "height": self.height,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSetItem:
        """
        从持久化字典还原条目，字段缺失即视为数据损坏。

        :param data: 元数据文件中的单个条目
        :return: 还原后的不可变条目
        :raises ValueError: 字段缺失或类型不符时抛出
        """
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            name=str(data["name"]),
            sha256=str(data["sha256"]),
            extension=str(data["extension"]),
            byte_size=int(data["byteSize"]),
            width=int(data["width"]) if data.get("width") is not None else None,
            height=int(data["height"]) if data.get("height") is not None else None,
            created_at=str(data["createdAt"]),
            updated_at=str(data["updatedAt"]),
        )


class FileSetStore:
    """
    管理用户媒体文件集，把导入的图片与视频复制到数据目录并按内容去重。

    文件落在 ``userdata/filesets/files/<sha256>.<ext>``，元数据索引为同目录的
    ``filesets.json``；缩略图属于可重建缓存，落在 ``cache/fileset-thumbnails``。
    导入后源文件移动或删除都不影响已入库内容。

    :param data_path: 启动器持久化数据目录
    """

    image_extensions: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})
    video_extensions: frozenset[str] = frozenset({".mp4", ".webm"})
    max_image_bytes = 10 * 1024 * 1024
    max_image_dimension = 4096
    max_video_bytes = 1024 * 1024 * 1024
    thumbnail_size = 360
    max_name_length = 80
    metadata_version = 1

    def __init__(self, data_path: Path) -> None:
        self.root = Path(data_path) / "userdata" / "filesets"
        self.metadata_path = self.root / "filesets.json"
        self.files_path = self.root / "files"
        self.thumbnail_root = Path(data_path) / "cache" / "fileset-thumbnails"
        # 保护元数据读改写；导入与删除都在这把锁内完成索引更新。
        self._lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_path.mkdir(parents=True, exist_ok=True)
        self._items = self._load_items()
        self._cleanup_files()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()

    def _load_items(self) -> list[FileSetItem]:
        """
        读取元数据索引；损坏时备份原文件并重建空索引。

        :return: 校验通过的条目列表
        """
        if not self.metadata_path.is_file():
            return []
        try:
            document = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if document.get("version") != self.metadata_version or not isinstance(document.get("items"), list):
                raise ValueError("不支持的文件集元数据格式")
            items = [FileSetItem.from_dict(entry) for entry in document["items"]]
            for item in items:
                if item.kind not in {"image", "video"}:
                    raise ValueError("文件集条目类型无效")
                if len(item.sha256) != 64 or any(character not in "0123456789abcdef" for character in item.sha256):
                    raise ValueError("文件集条目摘要无效")
            return items
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            backup = self.metadata_path.with_name(f"filesets.corrupt-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.json")
            try:
                self.metadata_path.replace(backup)
            except OSError as backup_error:
                logger.exception("备份损坏的文件集元数据失败")
                raise FileSetError("文件集数据损坏且无法备份", "FILESET_METADATA_INVALID") from backup_error
            logger.warning("文件集元数据损坏，已备份后重建：%s；原因：%s", backup.name, exc)
            return []

    def _save_items(self) -> None:
        # 整份索引原子替换，避免中途失败留下半份元数据。
        document = {"version": self.metadata_version, "items": [item.to_dict() for item in self._items]}
        atomic_write_text(self.metadata_path, json.dumps(document, ensure_ascii=False, indent=2))

    def _file_path(self, item: FileSetItem) -> Path:
        # 用摘要与扩展名拼出文件路径，任何越界结果都视为索引损坏。
        target = (self.files_path / f"{item.sha256}{item.extension}").resolve()
        if not target.is_relative_to(self.files_path.resolve()):
            raise FileSetError("文件集路径越界", "FILESET_PATH_ESCAPE")
        return target

    def _thumbnail_path(self, item: FileSetItem) -> Path:
        return self.thumbnail_root / f"{item.sha256}.webp"

    def _find_item(self, item_id: str) -> FileSetItem:
        # 索引条目数有限，线性查找比维护额外字典更不易失配。
        for item in self._items:
            if item.id == item_id:
                return item
        raise FileSetError("文件集条目不存在", "FILESET_ITEM_NOT_FOUND")

    def _cleanup_files(self) -> None:
        # 清理无索引引用的残留文件与中断导入留下的临时文件。
        referenced = {self._file_path(item).name for item in self._items}
        for path in self.files_path.iterdir():
            if not path.is_file():
                continue
            if path.name.endswith(".tmp") or path.name not in referenced:
                try:
                    path.unlink()
                except OSError:
                    logger.warning("清理文件集孤立文件失败：%s", path.name)

    def list_items(self) -> list[FileSetItem]:
        """
        返回按最近更新时间倒序的条目副本。

        :return: 不含本地绝对路径的条目列表
        """
        with self._lock:
            return sorted(self._items, key=lambda item: item.updated_at, reverse=True)

    def import_files(self, sources: Iterable[str | Path]) -> dict[str, Any]:
        """
        批量导入媒体文件，单个文件的失败不影响其余文件。

        :param sources: 文件选择器返回的本地源路径
        :return: 含 ``imported``、``duplicated`` 与 ``failed`` 三段的结果字典
        """
        imported: list[dict[str, Any]] = []
        duplicated: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for source in sources:
            try:
                item, is_duplicate = self.import_file(source)
            except (FileSetError, OSError) as exc:
                message = exc.message if isinstance(exc, FileSetError) else "读取文件失败"
                logger.warning("导入文件集条目失败：%s；原因：%s", source, message)
                failed.append({"path": str(source), "message": message})
                continue
            (duplicated if is_duplicate else imported).append(item.to_dict())
        return {"imported": imported, "duplicated": duplicated, "failed": failed}

    def import_file(self, source: str | Path) -> tuple[FileSetItem, bool]:
        """
        校验并复制单个媒体文件；相同内容直接返回已有条目。

        :param source: 本地源文件路径
        :return: 条目以及是否命中重复内容
        :raises FileSetError: 文件缺失、类型不支持、超出大小或尺寸限制时抛出
        """
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileSetError("选择的文件不存在", "FILESET_FILE_NOT_FOUND")
        if path.is_symlink():
            raise FileSetError("文件集不支持符号链接", "FILESET_FILE_UNSUPPORTED")
        extension = path.suffix.lower()
        kind = self._kind_by_extension(extension)
        byte_size = path.stat().st_size
        if byte_size <= 0:
            raise FileSetError("不能导入空文件", "FILESET_FILE_EMPTY")
        if kind == "image" and byte_size > self.max_image_bytes:
            raise FileSetError("图片不能超过 10 MiB", "FILESET_FILE_TOO_LARGE")
        if kind == "video" and byte_size > self.max_video_bytes:
            raise FileSetError("视频不能超过 1 GiB", "FILESET_FILE_TOO_LARGE")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FileSetError("读取文件失败", "FILESET_FILE_READ_FAILED") from exc
        width: int | None = None
        height: int | None = None
        if kind == "image":
            width, height = self._probe_image(data)
        digest = hashlib.sha256(data).hexdigest()

        with self._lock:
            existing = next((item for item in self._items if item.sha256 == digest), None)
            if existing is not None:
                logger.debug("文件集导入命中重复内容：摘要：%s", digest[:12])
                return existing, True

            timestamp = self._timestamp()
            item = FileSetItem(
                id=uuid4().hex,
                kind=kind,
                name=self._normalized_name(path.stem),
                sha256=digest,
                extension=extension,
                byte_size=byte_size,
                width=width,
                height=height,
                created_at=timestamp,
                updated_at=timestamp,
            )
            target = self._file_path(item)
            if not target.is_file():
                atomic_write_bytes(target, data)
            self._items.append(item)
            try:
                self._save_items()
            except OSError:
                # 索引写入失败时回滚内存条目，文件保留给下次导入复用。
                self._items.pop()
                raise
            if kind == "image":
                self._ensure_thumbnail(item)
            logger.info(
                "已导入文件集条目：类型：%s；大小：%s 字节；摘要：%s",
                kind,
                byte_size,
                digest[:12],
            )
            return item, False

    def rename_item(self, item_id: str, name: str) -> FileSetItem:
        """
        修改条目显示名，不触碰磁盘文件。

        :param item_id: 条目稳定标识
        :param name: 新显示名，去空白后截断到 80 字符
        :return: 更新后的条目
        :raises FileSetError: 条目不存在或名称为空时抛出
        """
        normalized = name.strip()[: self.max_name_length]
        if not normalized:
            raise FileSetError("名称不能为空", "FILESET_INVALID_NAME")
        with self._lock:
            index = self._index_of(item_id)
            current = self._items[index]
            updated = FileSetItem(
                id=current.id,
                kind=current.kind,
                name=normalized,
                sha256=current.sha256,
                extension=current.extension,
                byte_size=current.byte_size,
                width=current.width,
                height=current.height,
                created_at=current.created_at,
                updated_at=self._timestamp(),
            )
            self._items[index] = updated
            self._save_items()
            return updated

    def delete_items(self, item_ids: Iterable[str]) -> list[str]:
        """
        删除条目并清理其文件与缩略图，不存在的标识直接忽略。

        先在删除前按索引定位文件，避免元数据已移除后无法再推导路径；
        文件删除失败只记日志，不回滚索引，避免留下不可见的脏条目。

        :param item_ids: 待删除的条目标识
        :return: 实际删除的条目标识
        """
        targets = set(item_ids)
        with self._lock:
            removed = [item for item in self._items if item.id in targets]
            if not removed:
                return []
            self._items = [item for item in self._items if item.id not in targets]
            self._save_items()
            for item in removed:
                try:
                    self._file_path(item).unlink(missing_ok=True)
                    self._thumbnail_path(item).unlink(missing_ok=True)
                except OSError:
                    logger.warning("删除文件集文件失败：%s", item.sha256[:12])
            logger.info("已删除文件集条目：%d 项", len(removed))
            return [item.id for item in removed]

    def resolve(self, item_id: str) -> tuple[Path, str]:
        """
        返回条目的真实文件路径与类型，供 IPC 层签发协议地址。

        :param item_id: 条目稳定标识
        :return: 文件绝对路径与 ``image`` / ``video`` 类型
        :raises FileSetError: 条目不存在或文件已丢失时抛出
        """
        with self._lock:
            item = self._find_item(item_id)
        path = self._file_path(item)
        if not path.is_file():
            raise FileSetError("文件集条目文件已丢失", "FILESET_FILE_NOT_FOUND")
        return path, item.kind

    def thumbnail_path(self, item_id: str) -> Path | None:
        """
        返回图片条目的缩略图路径，缺失时按需生成。

        :param item_id: 条目稳定标识
        :return: 缩略图路径；视频条目返回 None
        :raises FileSetError: 条目不存在或缩略图生成失败时抛出
        """
        with self._lock:
            item = self._find_item(item_id)
        if item.kind != "image":
            return None
        return self._ensure_thumbnail(item)

    def referenced_paths(self) -> dict[str, str]:
        """
        返回摘要到文件路径的映射，供引用检查比对背景配置。

        :return: 以 sha256 为键、绝对路径字符串为值的映射
        """
        with self._lock:
            return {item.sha256: str(self._file_path(item)) for item in self._items}

    def _index_of(self, item_id: str) -> int:
        for index, item in enumerate(self._items):
            if item.id == item_id:
                return index
        raise FileSetError("文件集条目不存在", "FILESET_ITEM_NOT_FOUND")

    def _kind_by_extension(self, extension: str) -> str:
        # 分类只看扩展名，视频不解码；图片随后按真实内容校验。
        if extension in self.image_extensions:
            return "image"
        if extension in self.video_extensions:
            return "video"
        raise FileSetError("仅支持常见图片格式与 MP4/WebM 视频", "FILESET_FILE_UNSUPPORTED")

    def _probe_image(self, data: bytes) -> tuple[int, int]:
        """
        按真实图片头读取尺寸并校验可解码，拒绝伪装扩展名的内容。

        :param data: 已读取的图片字节
        :return: 宽高像素
        :raises FileSetError: 内容无法解码或尺寸超过限制时抛出
        """
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                image.verify()
        except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise FileSetError("图片已损坏或无法安全解码", "FILESET_IMAGE_INVALID") from exc
        if width <= 0 or height <= 0:
            raise FileSetError("图片尺寸无效", "FILESET_IMAGE_INVALID")
        if width > self.max_image_dimension or height > self.max_image_dimension:
            raise FileSetError("图片尺寸不能超过 4096 像素", "FILESET_IMAGE_TOO_LARGE")
        return width, height

    def _ensure_thumbnail(self, item: FileSetItem) -> Path:
        """
        生成并缓存 WebP 缩略图；已有缓存直接复用。

        :param item: 图片条目
        :return: 缩略图路径
        :raises FileSetError: 源文件缺失或缩略图生成失败时抛出
        """
        target = self._thumbnail_path(item)
        if target.is_file():
            return target
        source = self._file_path(item)
        try:
            with Image.open(source) as image:
                image.thumbnail((self.thumbnail_size, self.thumbnail_size))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, "WEBP", quality=82, method=4)
            atomic_write_bytes(target, buffer.getvalue())
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise FileSetError("生成缩略图失败", "FILESET_THUMBNAIL_FAILED") from exc
        return target

    def _normalized_name(self, raw: str) -> str:
        normalized = raw.strip()[: self.max_name_length]
        return normalized or "未命名文件"
