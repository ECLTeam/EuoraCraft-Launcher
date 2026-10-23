# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：以物理目录身份和已打开的文件对象约束实例元数据读取，不写游戏目录。
#
# 公开接口：
#   - class InstanceReadError — 有稳定错误码的读取失败。
#   - class DirectoryIdentity — 目录物理身份。
#   - directory_identity — 取得无链接目录的物理身份。
#   - class InstanceReadBoundary — 管理截止时间、字节预算及句柄校验。
# ============================================================

from __future__ import annotations

import ctypes
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from pydantic import BaseModel, ConfigDict, StrictInt

from ECL.plugin_api.contracts import JsonValue, ProtocolError, _json_value


class InstanceReadError(ValueError):
    """
    只包含稳定中文说明和错误码，不暴露用户路径或原始文件内容。
    """

    def __init__(self, message: str, error_code: str = "PLUGIN_RESOURCE_UNAVAILABLE") -> None:
        """
        保存读取边界的失败原因。

        :param message: 不含路径的说明
        :param error_code: 可供 IPC 转换的错误码
        """
        super().__init__(message)
        self.error_code: str = error_code


class DirectoryIdentity(BaseModel):
    """
    记录物理目录身份；不以可修改路径字符串代替目录对象。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    device: StrictInt
    inode: StrictInt
    created_ns: StrictInt


def directory_identity(directory_path: Path) -> DirectoryIdentity:
    """
    拒绝符号链接和 Windows 重解析点后取得目录身份。

    :param directory_path: 宿主解析的目录
    :return: 可持久化的物理身份
    :raises InstanceReadError: 目录不存在、类型不符或不能可靠识别时抛出
    """
    try:
        info = directory_path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory_path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not info.st_ino
        ):
            raise InstanceReadError("实例资源不是可验证的普通目录")
        created_ns = info.st_ctime_ns if sys.platform == "win32" else int(getattr(info, "st_birthtime", 0) * 10**9)
        return DirectoryIdentity(device=info.st_dev, inode=info.st_ino, created_ns=created_ns)
    except OSError as exc:
        raise InstanceReadError("实例资源无法访问") from exc


class InstanceReadBoundary:
    """
    对一次查询持有根身份、总预算和截止时间；每个文件读取前验证实际打开对象。
    """

    def __init__(self, root_path: Path, deadline: float) -> None:
        """
        建立单次查询的读取边界，不创建文件或目录。

        :param root_path: 已批准的规范根目录
        :param deadline: 接收方单调时钟截止时间
        """
        self.root_path: Path = root_path
        self.identity: DirectoryIdentity = directory_identity(root_path)
        self.deadline: float = deadline
        self._remaining = 64 * 1024 * 1024

    def check(self) -> None:
        """
        拒绝超时或被替换的根目录，不把等待结束当作读取已经取消。

        :raises InstanceReadError: 超时或根身份变化时抛出
        """
        if monotonic() >= self.deadline:
            raise InstanceReadError("实例读取已超过等待时间", "PLUGIN_DEADLINE_EXCEEDED")
        if directory_identity(self.root_path) != self.identity:
            raise InstanceReadError("实例资源身份已经变化")

    def directory(self, relative_path: Path) -> DirectoryIdentity:
        """
        逐层验证根内目录，拒绝链接、重解析点和越界。

        :param relative_path: 根内相对目录
        :return: 最终目录身份
        :raises InstanceReadError: 路径越界或任意层不是普通目录时抛出
        """
        self.check()
        if relative_path.is_absolute() or any(part in {"..", "."} for part in relative_path.parts):
            raise InstanceReadError("实例读取目标超出批准范围")
        current_path = self.root_path
        identity = self.identity
        for part in relative_path.parts:
            current_path = current_path / part
            identity = directory_identity(current_path)
        return identity

    @staticmethod
    def _opened_path(descriptor: int) -> Path:
        """
        从已经打开的文件句柄取得实际位置，不依赖读取前的路径解析猜测。
        """
        if sys.platform == "win32":
            import msvcrt
            from ctypes import wintypes

            function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            size = function(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
            if not size or size >= len(buffer):
                raise InstanceReadError("不能核实实例文件句柄")
            value = buffer.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return Path(value)
        if sys.platform.startswith("linux"):
            return Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        if sys.platform == "darwin":
            library = ctypes.CDLL(None, use_errno=True)
            buffer = ctypes.create_string_buffer(4096)
            if library.fcntl(descriptor, 50, buffer) != 0:
                raise InstanceReadError("不能核实实例文件句柄")
            return Path(os.fsdecode(buffer.value))
        raise InstanceReadError("当前平台不能核实实例读取边界", "PLUGIN_RUNTIME_UNSUPPORTED")

    def read_json(
        self,
        relative_path: Path,
        max_bytes: int,
        *,
        optional: bool = False,
        capture: Callable[[bytes, os.stat_result], None] | None = None,
    ) -> dict[str, JsonValue] | None:
        """
        在实际句柄校验后有界读取 JSON，缺少可选文件才返回 None。

        :param relative_path: 根内已经确定的元数据位置
        :param max_bytes: 单文件字节上限
        :param optional: 首次缺少文件是否表示未设置
        :param capture: 可选的同一文件内容与稳定元数据接收方，不接受调用方路径
        :return: 校验后的 JSON 对象或缺少的可选文件
        :raises InstanceReadError: 身份、大小、格式或读取失败时抛出
        """
        self.check()
        target_path = self.root_path / relative_path
        try:
            if optional and not target_path.exists() and not target_path.is_symlink():
                # 缺少父目录只表示尚未设置，不允许链接父目录借缺省路径绕过边界。
                existing_parent = relative_path.parent
                while existing_parent.parts and not (self.root_path / existing_parent).exists():
                    if (self.root_path / existing_parent).is_symlink():
                        raise InstanceReadError("实例资料包含未批准的链接")
                    existing_parent = existing_parent.parent
                self.directory(existing_parent)
                return None
            self.directory(relative_path.parent)
            info = target_path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise InstanceReadError("实例文件不是普通文件")
            with target_path.open("rb") as stream:
                actual_path = self._opened_path(stream.fileno())
                opened = os.fstat(stream.fileno())
                if actual_path != target_path or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise InstanceReadError("实例文件身份已经变化")
                limit = min(max_bytes, self._remaining)
                if opened.st_size > limit:
                    raise InstanceReadError("实例元数据超过读取上限", "PLUGIN_MESSAGE_TOO_LARGE")
                content = stream.read(limit + 1)
                self._remaining -= len(content)
                if len(content) > limit:
                    raise InstanceReadError("实例元数据超过读取上限", "PLUGIN_MESSAGE_TOO_LARGE")
                self.directory(relative_path.parent)
                current = target_path.lstat()
                final = os.fstat(stream.fileno())
                self._check_version(current, opened, final)
            value = _json_value(json.loads(content.decode("utf-8-sig"), object_pairs_hook=self._unique_fields))
            if not isinstance(value, dict):
                raise ValueError("元数据不是对象")
            self.check()
            if capture is not None:
                capture(content, opened)
            return value
        except InstanceReadError:
            raise
        except (OSError, ValueError, RecursionError, ProtocolError) as exc:
            raise InstanceReadError("实例元数据无法读取或格式无效", "PLUGIN_INSTANCE_METADATA_INVALID") from exc

    @staticmethod
    def _check_version(current: os.stat_result, opened: os.stat_result, final: os.stat_result) -> None:
        """
        文件内容读完后核对同一对象及其稳定版本，拒绝原地修改与同名替换。
        """
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or (
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise InstanceReadError("实例文件身份已经变化")

    @staticmethod
    def _unique_fields(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("资料字段重复")
            result[name] = value
        return result
