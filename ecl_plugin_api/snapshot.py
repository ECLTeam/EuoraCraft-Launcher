# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：计算和复核受信加载许可中的代码快照，不赋予文件系统沙箱能力。
#
# 公开接口：
#   - class CodeFile — 快照中一个相对文件的大小及摘要。
#   - class CodeSnapshot — 不可变代码清单及逐文件复核接口。
# ============================================================

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar

from ecl_plugin_api.contracts import JsonValue, ProtocolError


@dataclass(frozen=True, slots=True)
class CodeFile:
    """
    保存快照内文件的相对路径和内容身份。
    """

    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CodeSnapshot:
    """
    保存宿主选择的代码根和内容清单，不以插件自报摘要授权。

    复核可发现检查时已经发生的变化，不能阻止同用户恶意磁盘篡改。
    """

    root_path: Path
    files: tuple[CodeFile, ...]
    max_files: ClassVar[int] = 10000
    max_bytes: ClassVar[int] = 256 * 1024 * 1024

    @classmethod
    def capture(cls, root_path: Path) -> CodeSnapshot:
        """
        从宿主指定的目录建立内容清单，拒绝链接及越界路径。

        :param root_path: 待审阅或用于冻结验收的代码目录
        :return: 包含全部文件的快照
        :raises ProtocolError: 目录、路径或大小非法时抛出
        """
        root_path = Path(root_path)
        if root_path.is_symlink() or not root_path.is_dir():
            raise ProtocolError("插件代码根目录无效", "PLUGIN_CONTENT_CHANGED")
        root_path = root_path.resolve()
        entries = tuple(sorted(root_path.rglob("*")))
        if any(entry.is_symlink() or not entry.resolve().is_relative_to(root_path) for entry in entries):
            raise ProtocolError("插件快照包含链接或越界文件", "PLUGIN_CONTENT_CHANGED")
        file_paths = tuple(entry for entry in entries if entry.is_file())
        if len(file_paths) > cls.max_files or sum(entry.stat().st_size for entry in file_paths) > cls.max_bytes:
            raise ProtocolError("插件快照超过数量或大小上限", "PLUGIN_CONTENT_CHANGED")
        names = [entry.relative_to(root_path).as_posix() for entry in file_paths]
        if len({name.casefold() for name in names}) != len(names) or any(not _relative_name(name) for name in names):
            raise ProtocolError("插件快照路径无效或重复", "PLUGIN_CONTENT_CHANGED")
        files = tuple(
            CodeFile(
                entry.relative_to(root_path).as_posix(),
                entry.stat().st_size,
                hashlib.sha256(entry.read_bytes()).hexdigest(),
            )
            for entry in file_paths
        )
        if len(files) > cls.max_files or sum(item.size for item in files) > cls.max_bytes:
            raise ProtocolError("插件快照超过数量或大小上限", "PLUGIN_CONTENT_CHANGED")
        return cls(root_path, files)

    def digest(self) -> str:
        """
        计算排序后完整清单的摘要，用于绑定本次加载许可。

        :return: 小写 SHA-256 摘要
        """
        raw = json.dumps(self.records(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def records(self) -> list[JsonValue]:
        """
        返回可发送到受信引导的完整清单，不包含文件内容。

        :return: 路径、大小和摘要记录
        """
        return [{"name": item.name, "size": item.size, "sha256": item.sha256} for item in self.files]

    @classmethod
    def from_records(cls, root_path: Path, records: JsonValue) -> CodeSnapshot:
        """
        校验加载许可里的完整清单和相对路径。

        :param root_path: 宿主下发的代码根目录
        :param records: JSON 文件清单
        :return: 可以进一步复核实际字节的快照
        :raises ProtocolError: 清单重复、越界或类型非法时抛出
        """
        if not isinstance(records, list) or len(records) > cls.max_files:
            raise ProtocolError("插件快照清单格式无效")
        files: list[CodeFile] = []
        names: set[str] = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
                raise ProtocolError("插件快照记录格式无效")
            name, size, digest = record["name"], record["size"], record["sha256"]
            if not isinstance(name, str) or not _relative_name(name) or name.casefold() in names:
                raise ProtocolError("插件快照路径无效或重复")
            if type(size) is not int or size < 0 or not _digest(digest):
                raise ProtocolError("插件快照摘要或大小无效")
            names.add(name.casefold())
            files.append(CodeFile(name, size, str(digest)))
        if sum(item.size for item in files) > cls.max_bytes:
            raise ProtocolError("插件快照超过大小上限")
        return cls(Path(root_path), tuple(sorted(files, key=lambda item: item.name)))

    def verify(self, expected_digest: str) -> None:
        """
        在执行入口前复核全部文件与清单，发现变化时拒绝加载。

        :param expected_digest: 宿主批准的内容摘要
        :raises ProtocolError: 清单或实际文件与许可不符时抛出
        """
        actual = self.capture(self.root_path)
        if self.digest() != expected_digest or actual.files != self.files:
            raise ProtocolError("插件内容与已批准快照不一致", "PLUGIN_CONTENT_CHANGED")


def _relative_name(name: str) -> bool:
    """
    拒绝会被 Windows 或 POSIX 解释成不同边界的相对路径。
    """
    return (
        bool(name)
        and not name.startswith("/")
        and ":" not in name
        and "\\" not in name
        and all(part not in {"", ".", ".."} for part in name.split("/"))
        and not PurePosixPath(name).is_absolute()
    )


def _digest(value: JsonValue) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
