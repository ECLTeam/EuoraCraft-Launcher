# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：绑定本地 POSIX 文件身份、原子重命名及错误转换，不跟随链接。
#
# 公开接口：
#   - PosixFiles — Linux/macOS 的目录相对原生文件操作。
#   - file_errors — 在文件系统边界转换稳定错误，不泄漏路径。
# ============================================================

from __future__ import annotations

import ctypes
import errno
import os
import stat
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from ECL.utils.file_models import FileBoundaryError, FileIdentity, FileInformation


@contextmanager
def file_errors() -> Iterator[None]:
    """
    将原生路径异常转换为脱敏且稳定的文件边界错误。

    :raises FileBoundaryError: 保留失败类别，不返回成功或默认内容
    """
    try:
        yield
    except OSError as error:
        if error.errno == errno.ENOENT:
            code = "PLUGIN_RESOURCE_NOT_FOUND"
        elif error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            code = "PLUGIN_FILE_EXISTS"
        elif error.errno in {errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK}:
            code = "PLUGIN_FILE_BUSY"
        elif error.errno in {
            errno.ELOOP,
            errno.ENOTDIR,
            errno.EISDIR,
            errno.EINVAL,
            errno.EMLINK,
            errno.EACCES,
            errno.EPERM,
        }:
            code = "PLUGIN_FILE_INVALID"
        elif error.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EXDEV}:
            code = "PLUGIN_RUNTIME_UNSUPPORTED"
        else:
            code = "PLUGIN_FILE_IO_FAILED"
        raise FileBoundaryError("受控文件操作未完成", code) from None


class PosixFiles:
    """
    仅支持能提供出生时间和本地原子重命名的文件系统。
    """

    linux_filesystems: ClassVar[frozenset[int]] = frozenset({0xEF53, 0x58465342, 0x9123683E, 0x794C7630, 0x01021994})

    def __init__(self) -> None:
        """
        运行时绑定当前平台 ABI，不在 Windows 导入阶段读取专属属性。

        :raises FileBoundaryError: 平台或必需原生函数不支持
        """
        if sys.platform not in {"linux", "darwin"} or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise FileBoundaryError("当前平台不支持 POSIX 文件事务", "PLUGIN_RUNTIME_UNSUPPORTED")
        self._library = ctypes.CDLL(None, use_errno=True)
        try:
            self._rename = self._library.renameat2 if sys.platform == "linux" else self._library.renameatx_np
            self._rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            self._rename.restype = ctypes.c_int
            self._filesystem = self._library.fstatfs
            self._filesystem.argtypes = [ctypes.c_int, ctypes.c_void_p]
            self._filesystem.restype = ctypes.c_int
            if sys.platform == "linux":
                self._statx = self._library.statx
                self._statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
                self._statx.restype = ctypes.c_int
        except AttributeError:
            raise FileBoundaryError("缺少可靠文件事务接口", "PLUGIN_RUNTIME_UNSUPPORTED") from None

    def check_local(self, descriptor: int) -> None:
        """
        核对已打开对象所属文件系统，不将远程或未知文件系统视为本地。

        :param descriptor: 仍由调用方持有的目录描述符
        :raises FileBoundaryError: 文件系统未支持或查询失败
        """
        with file_errors():
            buffer = ctypes.create_string_buffer(4096)
            if self._filesystem(descriptor, buffer):
                raise OSError(ctypes.get_errno(), "filesystem")
            if sys.platform == "linux":
                allowed = ctypes.c_long.from_buffer(buffer).value in self.linux_filesystems
            else:
                name = buffer.raw[72:88].split(b"\0", 1)[0]
                flags = struct.unpack_from("=I", buffer.raw, 64)[0]
                allowed = name in {b"apfs", b"hfs", b"ufs"} and bool(flags & 0x1000)
            if not allowed:
                raise FileBoundaryError("文件系统尚未支持受控事务", "PLUGIN_RUNTIME_UNSUPPORTED")

    def _created(self, descriptor: int, name: str | None, information: os.stat_result) -> int:
        """
        使用出生时间而非会随写入变化的状态时间，缺少该能力时明确拒绝。
        """
        if sys.platform == "darwin":
            return int(information.st_birthtime * 1_000_000_000)
        buffer = ctypes.create_string_buffer(256)
        flags = 0x100 | 0x800 | (0x1000 if name is None else 0)
        if self._statx(descriptor, b"" if name is None else os.fsencode(name), flags, 0xFFF, buffer):
            raise OSError(ctypes.get_errno(), "statx")
        content = buffer.raw
        if not struct.unpack_from("=I", content)[0] & 0x800:
            raise FileBoundaryError("文件系统没有可靠出生时间", "PLUGIN_RUNTIME_UNSUPPORTED")
        inode = struct.unpack_from("=Q", content, 32)[0]
        major, minor = struct.unpack_from("=II", content, 136)
        if inode != information.st_ino or os.makedev(major, minor) != information.st_dev:
            raise FileBoundaryError("文件身份查询期间发生变化", "PLUGIN_FILE_CHANGED")
        seconds, nanoseconds = struct.unpack_from("=qI", content, 80)
        return seconds * 1_000_000_000 + nanoseconds

    def _information(self, descriptor: int, name: str | None) -> FileInformation:
        """
        将不跟随链接的 POSIX 元数据转换为共同模型，不把特殊对象视为普通文件。
        """
        with file_errors():
            information = (
                os.fstat(descriptor) if name is None else os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            )
            attributes = 0x10 if stat.S_ISDIR(information.st_mode) else 0
            if not information.st_mode & 0o222:
                attributes |= 1
            if not (stat.S_ISREG(information.st_mode) or stat.S_ISDIR(information.st_mode)):
                attributes |= 0x400
            return FileInformation(
                FileIdentity(
                    volume=information.st_dev,
                    index=information.st_ino,
                    created=self._created(descriptor, name, information),
                ),
                information.st_size,
                information.st_mtime_ns,
                attributes,
                information.st_nlink,
            )

    def information(self, descriptor: int) -> FileInformation:
        """
        从已经持有的对象取得元数据，不重新解析文件名。

        :param descriptor: 已打开且未关闭的对象
        :return: 物理身份、长度和时间信息
        """
        return self._information(descriptor, None)

    def entry(self, descriptor: int, name: str) -> FileInformation:
        """
        相对目录查询名称本身的身份，不打开设备或跟随链接。

        :param descriptor: 父目录描述符
        :param name: 调用方已验证的单个组件
        :return: 当前目录项自身元数据
        """
        return self._information(descriptor, name)

    def rename(self, source: int, name: str, target: int, destination: str, *, replace: bool) -> None:
        """
        在已打开目录之间原子发布，不覆盖模式由内核保证。

        :param source: 源目录描述符
        :param name: 源名称
        :param target: 目标目录描述符
        :param destination: 目标名称
        :param replace: 是否由已批准的替换事务允许覆盖
        """
        with file_errors():
            flags = 0 if replace else (1 if sys.platform == "linux" else 4)
            if self._rename(source, os.fsencode(name), target, os.fsencode(destination), flags):
                raise OSError(ctypes.get_errno(), "rename")
