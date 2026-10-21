# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：以 Windows 本地 NTFS 目录和文件句柄定位私有文件，禁止跟随重解析及覆盖发布。
#
# 公开接口：
#   - class FileBoundaryError — 不包含路径的文件边界错误。
#   - class FileIdentity / FileInformation — 已打开对象的物理身份与元数据。
#   - class FileDirectory / FileHandle — 相对目录对象执行有限的文件操作并显式关闭。
# ============================================================

from __future__ import annotations

import ctypes
import hashlib
import json
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ECL.plugin_api.contracts import ProtocolError


class FileBoundaryError(ProtocolError):
    """
    只暴露稳定业务错误，不包含宿主路径、原始内容或 NTSTATUS。
    """


class FileIdentity(BaseModel):
    """
    绑定目录或文件对象，不以可修改的名称代替对象身份。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    volume: int
    index: int
    created: int


@dataclass(frozen=True, slots=True)
class FileInformation:
    """
    描述句柄确认的普通对象，纳秒时间不直接放入 JSON DTO。
    """

    identity: FileIdentity
    size: int
    modified_ns: int
    attributes: int
    links: int

    def etag(self, digest: str) -> str:
        """
        将物理身份与完整内容摘要组合为不透明版本。

        :param digest: 文件完整字节的 SHA-256
        :return: 可安全传输的版本字符串
        """
        value = [self.identity.model_dump(), self.size, self.modified_ns, digest]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", wintypes.USHORT), ("maximum_length", wintypes.USHORT), ("buffer", wintypes.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root", wintypes.HANDLE),
        ("name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security", ctypes.c_void_p),
        ("quality", ctypes.c_void_p),
    ]


class _StatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _HandleInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("modified", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _NativeFiles:
    """
    仅在受支持运行时绑定原生 ABI，函数对象不作为业务状态传播。
    """

    def __init__(self) -> None:
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise FileBoundaryError("当前平台不支持插件私有文件", "PLUGIN_RUNTIME_UNSUPPORTED")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        native = ctypes.WinDLL("ntdll")
        self.open = self._bind(
            native,
            "NtCreateFile",
            [
                ctypes.POINTER(wintypes.HANDLE),
                wintypes.ULONG,
                ctypes.POINTER(_ObjectAttributes),
                ctypes.POINTER(_StatusBlock),
                ctypes.c_void_p,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.ULONG,
                ctypes.c_void_p,
                wintypes.ULONG,
            ],
            wintypes.LONG,
        )
        self.set = self._bind(
            native,
            "NtSetInformationFile",
            [wintypes.HANDLE, ctypes.POINTER(_StatusBlock), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG],
            wintypes.LONG,
        )
        self.list = self._bind(
            native,
            "NtQueryDirectoryFile",
            [
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(_StatusBlock),
                ctypes.c_void_p,
                wintypes.ULONG,
                wintypes.ULONG,
                wintypes.BOOLEAN,
                ctypes.c_void_p,
                wintypes.BOOLEAN,
            ],
            wintypes.LONG,
        )
        self.info = self._bind(
            kernel, "GetFileInformationByHandle", [wintypes.HANDLE, ctypes.POINTER(_HandleInformation)], wintypes.BOOL
        )
        self.volume = self._bind(
            kernel,
            "GetVolumeInformationByHandleW",
            [
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.LPWSTR,
                wintypes.DWORD,
            ],
            wintypes.BOOL,
        )
        self.path = self._bind(
            kernel,
            "GetFinalPathNameByHandleW",
            [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD],
            wintypes.DWORD,
        )
        self.close = self._bind(kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
        self.read = self._bind(
            kernel,
            "ReadFile",
            [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p],
            wintypes.BOOL,
        )
        self.write = self._bind(
            kernel,
            "WriteFile",
            [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p],
            wintypes.BOOL,
        )
        self.seek = self._bind(
            kernel,
            "SetFilePointerEx",
            [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        )
        self.flush = self._bind(kernel, "FlushFileBuffers", [wintypes.HANDLE], wintypes.BOOL)

    @staticmethod
    def _bind(library: ctypes.CDLL, name: str, args: list[object], result: object) -> Callable[..., int]:
        function = getattr(library, name)
        function.argtypes = args
        function.restype = result
        return function

    @staticmethod
    def check(status: int) -> None:
        """
        将预期内核结果转换为稳定错误，未认识的失败保持拒绝。
        """
        if status >= 0:
            return
        code = status & 0xFFFFFFFF
        if code in {0xC0000034, 0xC000003A}:
            raise FileBoundaryError("插件文件不存在", "PLUGIN_RESOURCE_NOT_FOUND")
        if code == 0xC0000035:
            raise FileBoundaryError("文件已存在", "PLUGIN_FILE_EXISTS")
        if code in {0xC0000043, 0xC0000056}:
            raise FileBoundaryError("插件文件正被占用", "PLUGIN_FILE_BUSY")
        raise FileBoundaryError("插件文件对象不可用", "PLUGIN_FILE_INVALID")

    def create(self, root: int | None, name: str, *, directory: bool, create: bool, writable: bool) -> int:
        """
        相对已持有对象交由内核创建/打开，所有路径解析均拒绝重解析，不共享删除。
        """
        text = ctypes.create_unicode_buffer(name)
        string = _UnicodeString(len(name.encode("utf-16-le")), ctypes.sizeof(text), ctypes.cast(text, wintypes.LPWSTR))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes), root, ctypes.pointer(string), 0x1040, None, None
        )
        status, output = _StatusBlock(), wintypes.HANDLE()
        access = 0x100080 | (1 if not directory else 0x21)
        if writable:
            access |= (2 | 4) if directory else (2 | 0x10000)
        options = 0x200020 | (1 if directory else 0x40)
        self.check(
            self.open(
                ctypes.byref(output),
                access,
                ctypes.byref(attributes),
                ctypes.byref(status),
                None,
                0x80,
                3 if directory else 1,
                2 if create else 1,
                options,
                None,
                0,
            )
        )
        assert output.value is not None
        return output.value

    def information(self, handle: int) -> FileInformation:
        """
        从实际内核句柄读取身份和长度，不触发名称解析或打开其他对象。
        """
        value = _HandleInformation()
        if not self.info(handle, ctypes.byref(value)):
            raise FileBoundaryError("不能核实插件文件句柄", "PLUGIN_FILE_IO_FAILED")
        created = (value.created.dwHighDateTime << 32) | value.created.dwLowDateTime
        modified = (value.modified.dwHighDateTime << 32) | value.modified.dwLowDateTime
        return FileInformation(
            FileIdentity(volume=value.volume, index=(value.index_high << 32) | value.index_low, created=created),
            (value.size_high << 32) | value.size_low,
            (modified - 116444736000000000) * 100,
            value.attributes,
            value.links,
        )


class FileDirectory:
    """
    持有禁止外部重命名的目录句柄，子对象操作相对于该对象并拒绝重解析。
    """

    def __init__(self, native: _NativeFiles, handle: int) -> None:
        """
        接管已打开的目录句柄，调用方在初始化失败时负责关闭它。

        :param native: 当前运行的原生绑定
        :param handle: 需要校验类型及重解析属性的句柄
        """
        self._native = native
        self._handle = handle
        self.identity: FileIdentity = self.information().identity

    @classmethod
    def open(cls, directory_path: Path) -> FileDirectory:
        """
        打开可信数据根，拒绝任何解析阶段的重解析和远程/非 NTFS 空间。

        :param directory_path: 宿主指定的绝对目录
        :return: 调用方必须关闭的目录对象
        """
        native = _NativeFiles()
        path_text = str(directory_path)
        if path_text.startswith("\\\\?\\") and path_text[5:7] == ":\\":
            path_text = path_text[4:]
        if not directory_path.is_absolute() or path_text.startswith("\\\\"):
            raise FileBoundaryError("插件文件需要本地目录", "PLUGIN_RUNTIME_UNSUPPORTED")
        handle = native.create(None, "\\??\\" + path_text, directory=True, create=False, writable=True)
        try:
            filesystem, actual = ctypes.create_unicode_buffer(32), ctypes.create_unicode_buffer(32768)
            if (
                not native.volume(handle, None, 0, None, None, None, filesystem, len(filesystem))
                or filesystem.value != "NTFS"
            ):
                raise FileBoundaryError("插件文件需要本地 NTFS", "PLUGIN_RUNTIME_UNSUPPORTED")
            count = native.path(handle, actual, len(actual), 0)
            if not count or count >= len(actual) or actual.value.startswith("\\\\?\\UNC\\"):
                raise FileBoundaryError("不能核实本地目录", "PLUGIN_RUNTIME_UNSUPPORTED")
            return cls(native, handle)
        except BaseException:
            native.close(handle)
            raise

    def information(self) -> FileInformation:
        """
        从实际目录句柄取得元数据并拒绝重解析及类型变化。

        :return: 已验证目录信息
        """
        value = self._native.information(self._handle)
        if not value.attributes & 0x10 or value.attributes & 0x400:
            raise FileBoundaryError("插件目录身份不可用", "PLUGIN_FILE_INVALID")
        return value

    def child(self, name: str, *, create: bool = False) -> FileDirectory:
        """
        相对已验证目录打开或独占创建一个宿主决定的子目录。

        :param name: 单个受信目录组件
        :param create: 是否要求新建，存在时拒绝
        :return: 调用方必须关闭的子目录
        """
        self._component(name)
        self.information()
        handle = self._native.create(self._handle, name, directory=True, create=create, writable=True)
        try:
            return FileDirectory(self._native, handle)
        except BaseException:
            self._native.close(handle)
            raise

    @staticmethod
    def _component(name: str) -> None:
        if not name or name in {".", ".."} or any(character in name for character in "/\\:\0"):
            raise FileBoundaryError("插件文件名称无效", "PLUGIN_FILE_INVALID")

    def file(
        self, name: str, *, create: bool = False, on_create: Callable[[FileHandle], None] | None = None
    ) -> FileHandle:
        """
        在产生任何新文件副作用之前绑定父目录，不沿路径重新解析。

        :param name: 已校验的单个文件名或宿主暂存名
        :param create: 是否独占创建暂存
        :param on_create: 新暂存的句柄所有者登记；先登记再做可能失败的元数据校验
        :return: 调用方必须关闭或清理的普通文件句柄
        """
        self._component(name)
        self.information()
        handle = self._native.create(self._handle, name, directory=False, create=create, writable=create)
        stream = FileHandle(self, handle)
        try:
            if create and on_create is not None:
                on_create(stream)
            stream._identity = stream.information().identity
            return stream
        except BaseException:
            if not create:
                stream.close()
            elif on_create is None:
                stream.discard()
            # 已交给创建者的句柄由创建者清理；删除/关闭失败也不能丢失上传归属。
            raise

    def names(self, deadline: float, limit: int = 1024) -> tuple[str, ...]:
        """
        通过目录句柄枚举有界名称，不将目录路径重新交给文件系统解析。

        :param deadline: 整次枚举截止时间
        :param limit: 总项数上限，包含内部暂存
        :return: 直属名称集合，不包括点目录
        """
        self.information()
        result: list[str] = []
        restart = True
        while True:
            if monotonic() >= deadline:
                raise FileBoundaryError("文件查询超时", "PLUGIN_DEADLINE_EXCEEDED")
            buffer, status = ctypes.create_string_buffer(65536), _StatusBlock()
            outcome = self._native.list(
                self._handle, None, None, None, ctypes.byref(status), buffer, len(buffer), 12, False, None, restart
            )
            if outcome & 0xFFFFFFFF == 0x80000006:
                return tuple(result)
            self._native.check(outcome)
            restart = False
            offset = 0
            content = buffer.raw[: status.information]
            while True:
                following = int.from_bytes(content[offset : offset + 4], "little")
                length = int.from_bytes(content[offset + 8 : offset + 12], "little")
                if offset + 12 + length > len(content) or length % 2:
                    raise FileBoundaryError("目录枚举结果无效", "PLUGIN_FILE_INVALID")
                name = content[offset + 12 : offset + 12 + length].decode("utf-16-le")
                if name not in {".", ".."}:
                    result.append(name)
                if len(result) > limit:
                    raise FileBoundaryError("私有目录项数超限", "PLUGIN_STORAGE_QUOTA_EXCEEDED")
                if not following:
                    break
                offset += following

    def close(self) -> None:
        """
        幂等释放目录句柄，必须在其所有子文件已经结束后调用。
        """
        if self._handle:
            if not self._native.close(self._handle):
                raise FileBoundaryError("目录句柄尚未关闭", "PLUGIN_FILE_BUSY")
            self._handle = 0

    def save_record(self, name: str, content: bytes) -> None:
        """
        在宿主独立元数据目录中原子替换记录，不能用于插件目标目录。

        :param name: 宿主生成的记录名
        :param content: 严格模型序列化后的有限字节
        """
        stream = self.file(f".record-{uuid4().hex}", create=True)
        try:
            stream.write(0, content)
            stream.flush()
            stream._rename(name, replace=True)
        finally:
            if stream.is_published:
                stream.close()
            else:
                stream.discard()


class FileHandle:
    """
    持有不共享写入和删除的普通文件，发布成功后禁止按暂存删除。
    """

    def __init__(self, directory: FileDirectory, handle: int) -> None:
        """
        接管根下的文件句柄；FileDirectory 在交接资源归属后立即校验身份。

        :param directory: 已验证且仍持有的父目录
        :param handle: 调用方在初始化失败时负责关闭的句柄
        """
        self.directory: FileDirectory = directory
        self._native = directory._native
        self._handle = handle
        self.is_published: bool = False
        self._is_delete_pending = False
        self._identity: FileIdentity | None = None

    @property
    def identity(self) -> FileIdentity:
        """
        保存初次校验的物理身份，失败时句柄仍由已登记的创建者负责。

        :return: 普通单链接同卷文件的初始身份
        """
        if self._identity is None:
            raise FileBoundaryError("文件元数据尚未确认", "PLUGIN_FILE_INVALID")
        return self._identity

    def information(self) -> FileInformation:
        """
        校验普通文件、单链接和同卷，不通过文件名猜测已打开对象。

        :return: 当前实际对象信息
        """
        value = self._native.information(self._handle)
        if (
            value.attributes & (0x400 | 0x10)
            or value.links != 1
            or value.identity.volume != self.directory.identity.volume
        ):
            raise FileBoundaryError("插件文件不是单链接普通文件", "PLUGIN_FILE_INVALID")
        return value

    def read(self, offset: int, count: int) -> bytes:
        """
        从独占稳定视图读取有限字节，不隐式读取到文件末尾。

        :param offset: 非负文件偏移
        :param count: 本次有限字节数
        :return: 实际读取内容
        """
        buffer, received = ctypes.create_string_buffer(count), wintypes.DWORD()
        if not self._native.seek(self._handle, offset, None, 0) or not self._native.read(
            self._handle, buffer, count, ctypes.byref(received), None
        ):
            raise FileBoundaryError("插件文件读取失败", "PLUGIN_FILE_IO_FAILED")
        return buffer.raw[: received.value]

    def digest(self, deadline: float, max_bytes: int = 8388608) -> str:
        """
        在同一文件句柄上校验总量并计算完整内容摘要。

        :param deadline: 单调截止时间
        :param max_bytes: 最大文件大小
        :return: SHA-256
        """
        size = self.information().size
        if size > max_bytes:
            raise FileBoundaryError("插件文件大小超限", "PLUGIN_WRITE_SIZE_EXCEEDED")
        digest, offset = hashlib.sha256(), 0
        while offset < size:
            if monotonic() >= deadline:
                raise FileBoundaryError("文件查询超时", "PLUGIN_DEADLINE_EXCEEDED")
            block = self.read(offset, min(32768, size - offset))
            if not block:
                raise FileBoundaryError("插件文件内容已变化", "PLUGIN_FILE_CHANGED")
            digest.update(block)
            offset += len(block)
        return digest.hexdigest()

    def write(self, offset: int, content: bytes) -> None:
        """
        将一个已接受的有限块写入当前暂存，部分写入明确失败。

        :param offset: 上传会话确认的偏移
        :param content: 已校验的有限块
        """
        written = wintypes.DWORD()
        if (
            self.is_published
            or not self._native.seek(self._handle, offset, None, 0)
            or not self._native.write(self._handle, content, len(content), ctypes.byref(written), None)
            or written.value != len(content)
        ):
            raise FileBoundaryError("插件文件写入失败", "PLUGIN_STORAGE_FAILED")

    def flush(self) -> None:
        """
        发布前要求有限暂存内容完成系统刷新，失败时不发布。
        """
        if not self._native.flush(self._handle):
            raise FileBoundaryError("插件文件刷新失败", "PLUGIN_STORAGE_FAILED")

    def publish(self, name: str) -> None:
        """
        将当前暂存按根句柄原子发布为新名称，绝不替换已有目标。

        :param name: 已校验的最终单个文件名
        """
        self._rename(name, replace=False)

    def _rename(self, name: str, *, replace: bool) -> None:
        """
        区分宿主元数据替换与插件不可覆盖发布，共享同一原生 ABI。
        """
        self.directory._component(name)
        self.directory.information()
        if self.is_published:
            raise FileBoundaryError("文件已经发布", "PLUGIN_FILE_INVALID")

        class Rename(ctypes.Structure):
            _fields_ = [
                ("replace", wintypes.BOOLEAN),
                ("root", wintypes.HANDLE),
                ("length", wintypes.ULONG),
                ("name", wintypes.WCHAR * (len(name) + 1)),
            ]

        value = Rename(replace, self.directory._handle, len(name.encode("utf-16-le")), name)
        status = _StatusBlock()
        self._native.check(
            self._native.set(
                self._handle, ctypes.byref(status), ctypes.byref(value), Rename.name.offset + value.length, 10
            )
        )
        self.is_published = True

    def discard(self) -> None:
        """
        只按当前对象清理未发布暂存；删除确认失败时保留句柄供重试。
        """
        if self.is_published:
            raise FileBoundaryError("不能删除已经发布的文件", "PLUGIN_FILE_INVALID")
        if self._handle and not self._is_delete_pending:
            status, value = _StatusBlock(), wintypes.BOOLEAN(True)
            self._native.check(
                self._native.set(self._handle, ctypes.byref(status), ctypes.byref(value), ctypes.sizeof(value), 13)
            )
            self._is_delete_pending = True
        self.close()

    def close(self) -> None:
        """
        幂等释放文件句柄，关闭失败时不误报资源已经收敛。
        """
        if self._handle:
            if not self._native.close(self._handle):
                raise FileBoundaryError("文件句柄尚未关闭", "PLUGIN_FILE_BUSY")
            self._handle = 0
