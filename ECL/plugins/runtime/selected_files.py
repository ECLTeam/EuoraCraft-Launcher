# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按可信设置中用户选择的精确文件交付只读分块，不接纳插件指定路径。
#
# 公开接口：
#   - read_selected_file() — 校验设置、文件身份及修订后交付只读数据。
#   - selected_file_paths() — 为实例扫描缓存提供用户已选择的文件。
# ============================================================

from __future__ import annotations

import base64
import hashlib
import stat
from pathlib import Path

from pydantic import Field, StrictInt

from ECL.plugin_api.contracts import JsonValue, ProtocolError
from ECL.plugins.runtime.models import RequestModel


class _SelectedFileRequest(RequestModel):
    """
    只接受当前插件的设置键与有界分块位置。
    """

    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
    offset: StrictInt = Field(default=0, ge=0, le=4 * 1024 * 1024)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def selected_file_paths(view: dict[str, JsonValue]) -> list[Path]:
    """
    返回可信设置选择的文件，用于监听修订；不接受 Worker 默认路径。

    :param view: 宿主从本安装读取的设置视图
    :return: 已选择的绝对路径，空选择不返回
    """
    values, schema = view.get("readonly_files"), view.get("schema")
    if not isinstance(values, dict) or not isinstance(schema, list):
        return []
    return [
        Path(value)
        for item in schema
        if isinstance(item, dict)
        and item.get("type") == "readonly_file"
        and isinstance(value := values.get(str(item.get("key"))), str)
        and value
        and Path(value).is_absolute()
    ]


def read_selected_file(view: dict[str, JsonValue], params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """
    在文件句柄内复核身份和版本，读取精确选择的普通文件的一块。

    文件内容最多 4 MiB，每块最多 8 KiB；修订变化要求从头读取。不提供写入操作。

    :param view: 可信用户设置的快照
    :param params: 设置键、偏移和可选修订
    :return: base64 分块、修订、大小及后续偏移
    :raises ProtocolError: 未选择、文件不安全、超限、修订过期或读取失败
    """
    query = _SelectedFileRequest.model_validate(params)
    schema, values = view.get("schema"), view.get("values")
    if (
        not isinstance(schema, list)
        or not isinstance(values, dict)
        or not any(
            isinstance(item, dict) and item.get("key") == query.key and item.get("type") == "readonly_file"
            for item in schema
        )
    ):
        raise ProtocolError("设置未声明只读文件", "PLUGIN_PERMISSION_DENIED")
    approved = view.get("readonly_files")
    value = approved.get(query.key) if isinstance(approved, dict) else None
    if value != values.get(query.key):
        raise ProtocolError("此文件未由用户选择", "PLUGIN_PERMISSION_DENIED")
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ProtocolError("请先在插件设置中选择索引文件", "PLUGIN_RESOURCE_NOT_FOUND")
    selected_path = Path(value)
    try:
        if selected_path.is_symlink() or selected_path.resolve() != selected_path.absolute():
            raise ProtocolError("不能读取链接文件", "PLUGIN_RESOURCE_DENIED")
        with selected_path.open("rb") as handle:
            import os

            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
                raise ProtocolError("只读文件类型或大小不支持", "PLUGIN_RESOURCE_DENIED")
            revision = hashlib.sha256(
                f"{view['revision']}:{before.st_dev}:{before.st_ino}:{before.st_size}:{before.st_mtime_ns}".encode()
            ).hexdigest()
            if (query.offset and query.revision is None) or (query.revision is not None and query.revision != revision):
                raise ProtocolError("文件已变化，请重新读取", "PLUGIN_REVISION_CONFLICT")
            if query.offset > before.st_size:
                raise ProtocolError("文件偏移超出范围", "PLUGIN_SCHEMA_INVALID")
            handle.seek(query.offset)
            chunk = handle.read(8192)
            after = os.fstat(handle.fileno())
            current = selected_path.stat()
            if selected_path.is_symlink() or selected_path.resolve() != selected_path.absolute():
                raise ProtocolError("读取时文件链接已变化", "PLUGIN_RESOURCE_DENIED")
            if any(
                (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                for item in (after, current)
            ):
                raise ProtocolError("读取时文件已变化", "PLUGIN_REVISION_CONFLICT")
    except OSError as exc:
        raise ProtocolError("选定文件无法读取", "PLUGIN_STORAGE_FAILED") from exc
    following = query.offset + len(chunk)
    return {
        "content": base64.b64encode(chunk).decode("ascii"),
        "revision": revision,
        "size": before.st_size,
        "next_offset": following if following < before.st_size else None,
    }
