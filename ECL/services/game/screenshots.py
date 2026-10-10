# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：截图服务：截图清单、缩略图与封面/背景设置。
#
# 公开接口：
#   - class ScreenshotCoordinator — 管理实例截图索引、缩略图、剪贴板、封面和删除操作。
#       - list_screenshots(game_path, version_id, version_isolation=…) -> list[dict[str, Any]] — 按修改时间降序返回可读图片，并提供前端日期分组键。
#       - screenshot_thumbnail(game_path, version_id, screenshot_id, version_isolation=…, size=…) -> dict[str, Any] — 以源路径、大小和修改时间为键生成 WebP 缩略图缓存。
#       - save_screenshot_as(game_path, version_id, screenshot_id, output_path, version_isolation=…) -> dict[str, str] — 通过原子替换把截图复制到用户选择的导出路径。
#       - copy_screenshot(game_path, version_id, screenshot_id, version_isolation=…) -> None — 在 Windows 上把截图以 CF_DIB 格式写入系统剪贴板。
#       - delete_screenshot(game_path, version_id, screenshot_id, version_isolation=…) -> None — 把指定截图直接删除。
#       - set_instance_cover(game_path, version_id, screenshot_id, version_isolation=…) -> dict[str, Any] — 复制截图为 ECL 私有封面并记录相对配置，不修改原截图。
#       - set_launcher_background_candidate(game_path, version_id, screenshot_id, version_isolation=…) -> dict[str, str] — 复制截图到启动器数据目录，供 API 层原子写入背景配置。
# ============================================================

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from ECL.utils import atomic_write_bytes

from .base import GameServiceError
from .workspace import delete_path, resolve_relative_id


class ScreenshotCoordinator:
    """
    管理实例截图索引、缩略图、剪贴板、封面和删除操作。
    """

    _EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

    def _screenshot_root(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> Path:
        return self.resolve_instance(game_path, version_id, version_isolation).game_data_path / "screenshots"

    def list_screenshots(self, game_path: Any, version_id: Any, version_isolation: Any = False) -> list[dict[str, Any]]:
        """
        按修改时间降序返回可读图片，并提供前端日期分组键。
        """
        root = self._screenshot_root(game_path, version_id, version_isolation)
        if not root.is_dir():
            return []
        results: list[dict[str, Any]] = []
        for path in root.iterdir():
            if not path.is_file() or path.suffix.casefold() not in self._EXTENSIONS:
                continue
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except (OSError, UnidentifiedImageError):
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            results.append(
                {
                    "id": path.name,
                    "name": path.name,
                    "path": str(path),
                    "width": width,
                    "height": height,
                    "size": path.stat().st_size,
                    "modifiedAt": modified.isoformat(),
                    "dateGroup": modified.date().isoformat(),
                }
            )
        return sorted(results, key=lambda item: item["modifiedAt"], reverse=True)

    def screenshot_thumbnail(
        self,
        game_path: Any,
        version_id: Any,
        screenshot_id: Any,
        version_isolation: Any = False,
        size: int = 360,
    ) -> dict[str, Any]:
        """
        以源路径、大小和修改时间为键生成 WebP 缩略图缓存。
        """
        source = resolve_relative_id(self._screenshot_root(game_path, version_id, version_isolation), screenshot_id)
        stat = source.stat()
        cache_key = hashlib.sha256(f"{source}|{stat.st_size}|{stat.st_mtime_ns}|{size}".encode()).hexdigest()
        cache_path = self._data_path / "cache" / "screenshots" / f"{cache_key}.webp"
        if not cache_path.is_file():
            try:
                with Image.open(source) as image:
                    image.thumbnail((max(64, min(size, 1024)),) * 2)
                    buffer = io.BytesIO()
                    image.convert("RGB").save(buffer, "WEBP", quality=82, method=4)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(cache_path, buffer.getvalue())
            except (OSError, UnidentifiedImageError) as exc:
                raise GameServiceError(f"生成截图缩略图失败：{exc}", "SCREENSHOT_THUMBNAIL_FAILED") from exc
        return {"path": str(cache_path), "sourcePath": str(source)}

    def save_screenshot_as(
        self, game_path: Any, version_id: Any, screenshot_id: Any, output_path: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        """
        通过原子替换把截图复制到用户选择的导出路径。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :param screenshot_id: ``screenshots`` 目录内相对安全的截图文件名
        :param output_path: 用户选择的导出目标路径
        :param version_isolation: 是否启用版本目录隔离
        :return: 导出后文件的绝对路径
        """
        source = resolve_relative_id(self._screenshot_root(game_path, version_id, version_isolation), screenshot_id)
        destination = Path(str(output_path)).expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.ecl-tmp")
        try:
            shutil.copy2(source, temp)
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)
        return {"path": str(destination)}

    def copy_screenshot(
        self, game_path: Any, version_id: Any, screenshot_id: Any, version_isolation: Any = False
    ) -> None:
        """
        在 Windows 上把截图以 CF_DIB 格式写入系统剪贴板。
        """
        if not hasattr(ctypes, "windll"):
            raise GameServiceError("当前系统不支持图片剪贴板", "CLIPBOARD_UNAVAILABLE")
        source = resolve_relative_id(self._screenshot_root(game_path, version_id, version_isolation), screenshot_id)
        try:
            with Image.open(source) as image:
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, "BMP")
            data = buffer.getvalue()[14:]
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            handle = kernel32.GlobalAlloc(0x0002, len(data))
            pointer = kernel32.GlobalLock(handle)
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.OpenClipboard(None):
                kernel32.GlobalFree(handle)
                raise OSError("OpenClipboard failed")
            try:
                user32.EmptyClipboard()
                if not user32.SetClipboardData(8, handle):
                    kernel32.GlobalFree(handle)
                    raise OSError("SetClipboardData failed")
            finally:
                user32.CloseClipboard()
        except Exception as exc:
            raise GameServiceError(f"复制截图到剪贴板失败：{exc}", "CLIPBOARD_WRITE_FAILED") from exc

    def delete_screenshot(
        self, game_path: Any, version_id: Any, screenshot_id: Any, version_isolation: Any = False
    ) -> None:
        """
        把指定截图直接删除。

        :param game_path: Minecraft 游戏根目录
        :param version_id: 版本目录名称
        :param screenshot_id: ``screenshots`` 目录内相对安全的截图文件名
        :param version_isolation: 是否启用版本目录隔离
        """
        source = resolve_relative_id(self._screenshot_root(game_path, version_id, version_isolation), screenshot_id)
        delete_path(source)

    def set_instance_cover(
        self, game_path: Any, version_id: Any, screenshot_id: Any, version_isolation: Any = False
    ) -> dict[str, Any]:
        """
        复制截图为 ECL 私有封面并记录相对配置，不修改原截图。
        """
        target = self.resolve_instance(game_path, version_id, version_isolation)
        source = resolve_relative_id(self._screenshot_root(game_path, version_id, version_isolation), screenshot_id)
        try:
            with Image.open(source) as image:
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise GameServiceError("截图文件不可读", "INVALID_SCREENSHOT") from exc
        ecl_dir = target.instance_path / ".ecl"
        ecl_dir.mkdir(parents=True, exist_ok=True)
        destination = ecl_dir / f"cover{source.suffix.casefold()}"
        for old in ecl_dir.glob("cover.*"):
            if old != destination:
                old.unlink(missing_ok=True)
        atomic_write_bytes(destination, source.read_bytes())
        return self.patch_instance_profile(
            target.minecraft_root_path,
            target.instance_directory_name,
            {"cover": {"type": "local", "value": destination.name}},
        )

    def set_launcher_background_candidate(
        self, game_path: Any, version_id: Any, screenshot_id: Any, version_isolation: Any = False
    ) -> dict[str, str]:
        """
        复制截图到启动器数据目录，供 API 层原子写入背景配置。
        """
        source = resolve_relative_id(self._screenshot_root(game_path, version_id, version_isolation), screenshot_id)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        destination = self._data_path / "backgrounds" / f"screenshot-{digest}{source.suffix.casefold()}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(destination, source.read_bytes())
        return {"path": str(destination), "config": json.dumps({"type": "local", "path": str(destination)})}
