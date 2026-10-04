# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：校验前端生成的皮肤头像 PNG，并保护源文件和原子保存目标。
# 不读取账户或下载远程纹理，保存位置由文件 IPC 的原生对话框提供。
#
# 公开接口：
#   - class SkinAvatarError — 携带稳定错误码的头像导出异常。
#   - class SkinAvatarExporter — 校验有界 PNG 并原子导出到用户选择的路径。
# ============================================================

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ECL.utils.files import atomic_write_bytes


class SkinAvatarError(ValueError):
    """
    表示预期的头像导出失败，携带可供 IPC 转换的稳定错误码。
    """

    error_code: str

    def __init__(self, message: str, code: str) -> None:
        """
        初始化不含图片内容或个人路径的领域错误。

        :param message: 可向用户显示的失败原因
        :param code: 稳定错误码
        """
        super().__init__(message)
        self.error_code = code


class SkinAvatarExporter:
    """
    校验有界的方形头像 PNG，并通过原子替换保存到原生选择的目标。

    图片解码及文件操作均为同步操作，异步调用方必须使用线程边界。
    """

    allowed_sizes: tuple[int, ...] = (64, 128, 256, 512)
    max_png_bytes: int = 2 * 1024 * 1024
    max_data_url_chars: int = 22 + 4 * ((max_png_bytes + 2) // 3)
    png_prefix: str = "data:image/png;base64,"

    @classmethod
    def decode_png(cls, data_url: str, size: int) -> bytes:
        """
        解码并验证实际 PNG 内容，不信任 MIME 声明或前端的尺寸检查。

        在完整解码前限制宽高，只接受指定尺寸的单帧 PNG。

        :param data_url: 有长度上限的 PNG Data URL
        :param size: 声明的方形输出边长
        :return: 验证后的原始 PNG 字节
        :raises SkinAvatarError: 编码、格式、长度或尺寸无效时抛出
        """
        if size not in cls.allowed_sizes or isinstance(size, bool):
            raise SkinAvatarError("头像尺寸无效", "SKIN_AVATAR_INVALID_SIZE")
        if len(data_url) > cls.max_data_url_chars:
            raise SkinAvatarError("头像图片超过大小限制", "SKIN_AVATAR_TOO_LARGE")
        if not data_url.startswith(cls.png_prefix):
            raise SkinAvatarError("头像必须是 PNG 图片", "SKIN_AVATAR_INVALID_PNG")
        try:
            png_bytes = base64.b64decode(data_url[len(cls.png_prefix) :], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SkinAvatarError("头像图片编码无效", "SKIN_AVATAR_INVALID_PNG") from exc
        if len(png_bytes) > cls.max_png_bytes:
            raise SkinAvatarError("头像图片超过大小限制", "SKIN_AVATAR_TOO_LARGE")
        try:
            with Image.open(BytesIO(png_bytes), formats=("PNG",)) as image:
                if image.size != (size, size) or getattr(image, "n_frames", 1) != 1:
                    raise SkinAvatarError("头像实际尺寸或帧数无效", "SKIN_AVATAR_INVALID_SIZE")
                image.verify()
            with Image.open(BytesIO(png_bytes), formats=("PNG",)) as image:
                image.load()
        except SkinAvatarError:
            raise
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise SkinAvatarError("头像 PNG 已损坏或无法解码", "SKIN_AVATAR_INVALID_PNG") from exc
        return png_bytes

    @staticmethod
    def save_png(target_path: Path, source_path: Path, png_bytes: bytes) -> None:
        """
        保护导入的皮肤文件，并原子保存经过校验的头像字节。

        调用前必须使用 decode_png 校验字节；目标必须来自原生保存对话框。
        不自动更换扩展名，避免绕过原生对话框对既有目标的覆盖确认。

        :param target_path: 原生对话框选定的 PNG 目标
        :param source_path: 本次导入皮肤的绝对路径
        :param png_bytes: 已经验证的单帧头像 PNG
        :raises SkinAvatarError: 扩展名无效或目标指向源皮肤时抛出
        :raises OSError: 保存或原子替换失败时抛出，已有文件保持不变
        """
        if target_path.suffix.lower() != ".png":
            raise SkinAvatarError("请选择以 .png 结尾的保存文件", "SKIN_AVATAR_INVALID_EXTENSION")
        if not target_path.is_absolute() or not source_path.is_absolute():
            raise SkinAvatarError("头像文件路径无效", "SKIN_AVATAR_INVALID_PATH")
        if target_path.resolve() == source_path.resolve() or (
            target_path.exists() and source_path.exists() and target_path.samefile(source_path)
        ):
            raise SkinAvatarError("头像不能覆盖导入的皮肤，请选择其他文件名", "SKIN_AVATAR_SOURCE_CONFLICT")
        atomic_write_bytes(target_path, png_bytes)
