# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：从 logo.ico 生成 macOS 应用包使用的 logo.icns 图标。
#
# 公开接口：
#   - main() -> int — 重新生成 resources/img/logo.icns 并输出中文摘要。
# ============================================================

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image
from PIL.IcoImagePlugin import IcoImageFile

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ICON = REPO_ROOT / "resources" / "img" / "logo.ico"
TARGET_ICON = REPO_ROOT / "resources" / "img" / "logo.icns"

# icns 容器以 4 字节魔数加 4 字节总长度开头，其后是若干「4 字节类型 + 4 字节长度 + 负载」条目，
# 条目长度含自身 8 字节头。容器不写 TOC，与 iconutil 产出的结构一致。
container_magic = b"icns"
container_header_size = 8
entry_header_size = 8


def _load_master_image() -> Image.Image:
    """
    读取 ``logo.ico`` 中像素尺寸最大的图层作为缩放母版。

    Windows 图标常同时保存多档尺寸，选最大图层可避免母版本身已被缩小。
    读取后立即脱离文件句柄，母版不再依赖磁盘上的 ``.ico``。

    :return: 已转换为 RGBA 的母版图像
    :raises SystemExit: 图标文件缺失或不是 Windows 图标时抛出
    """
    if not SOURCE_ICON.is_file():
        raise SystemExit(f"缺少图标源文件：{SOURCE_ICON}")
    with Image.open(SOURCE_ICON) as ico_image:
        if not isinstance(ico_image, IcoImageFile):
            raise SystemExit(f"图标源文件不是 Windows 图标：{SOURCE_ICON}")
        sizes = sorted(ico_image.ico.sizes())
        if not sizes:
            raise SystemExit(f"图标源文件不包含任何图层：{SOURCE_ICON}")
        master = ico_image.ico.getimage(sizes[-1]).convert("RGBA")
    return master


def _render_entries(master: Image.Image) -> list[tuple[bytes, bytes]]:
    """
    按 macOS 需要的条目类型与像素尺寸渲染 PNG 负载。

    条目覆盖 16 至 256 像素：16/32/64 供菜单栏与 Dock 使用，128/256 供
    Finder 列表与图标视图使用。母版只有 256 像素，放大到 512 以上不会带来
    额外细节，故不生成 512/1024 条目，由系统缩放。

    同一像素尺寸只缩放一次再复用负载，母版原始尺寸的条目为无损拷贝。

    :param master: 缩放母版
    :return: 按 icns 条目顺序排列的「类型 + PNG 负载」列表
    """
    # (icns 条目类型, 像素边长)；icp4/icp5 是 16/32 像素的旧式条目，
    # ic11/ic12 是 16/32 像素的 Retina 条目，ic07/ic13/ic08 是 128/256 像素条目。
    required_entries = (
        (b"icp4", 16),
        (b"icp5", 32),
        (b"ic11", 32),
        (b"ic12", 64),
        (b"ic07", 128),
        (b"ic13", 256),
        (b"ic08", 256),
    )
    payload_by_size: dict[int, bytes] = {}
    entries: list[tuple[bytes, bytes]] = []
    for entry_type, size in required_entries:
        if size not in payload_by_size:
            image = master if master.size == (size, size) else master.resize((size, size), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            payload_by_size[size] = buffer.getvalue()
        entries.append((entry_type, payload_by_size[size]))
    return entries


def _pack_container(entries: list[tuple[bytes, bytes]]) -> bytes:
    """
    把条目负载拼装成 icns 容器字节。

    条目顺序按 icp4、icp5、ic11、ic12、ic07、ic13、ic08 排列，系统按类型而非顺序
    查找条目，因此顺序只影响可读性。总长度覆盖魔数、长度字段与全部条目。

    :param entries: 「条目类型 + 负载」列表
    :return: 完整的 icns 文件字节
    """
    blocks = [
        struct.pack(">4sI", entry_type, entry_header_size + len(payload)) + payload for entry_type, payload in entries
    ]
    return (
        container_magic
        + struct.pack(">I", container_header_size + sum(len(block) for block in blocks))
        + b"".join(blocks)
    )


def _write_atomically(target: Path, payload: bytes) -> None:
    """
    先写临时文件再原子替换目标文件。

    生成中断时保留原有图标，避免 PyInstaller 读到写坏的 ``.icns``。

    :param target: 目标图标路径
    :param payload: 待写入的 icns 字节
    """
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)


def main() -> int:
    """
    重新生成 macOS 应用图标。

    在仓库根目录执行 ``python packaging/build-macos-icon.py``；换标时先替换
    ``resources/img/logo.ico``，再执行本脚本，最后运行后端测试确认条目与母版一致。

    :return: 生成完成时返回 0
    :raises SystemExit: 图标源文件缺失或无法读取时抛出
    """
    master = _load_master_image()
    entries = _render_entries(master)
    _write_atomically(TARGET_ICON, _pack_container(entries))
    print(f"母版尺寸：{master.size[0]}×{master.size[1]}")
    print(f"条目数量：{len(entries)}")
    print(f"文件大小：{TARGET_ICON.stat().st_size} 字节")
    print(f"输出路径：{TARGET_ICON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
