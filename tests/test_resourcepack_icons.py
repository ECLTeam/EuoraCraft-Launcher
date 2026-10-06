from __future__ import annotations

import base64
import json
import logging
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from ECL.api.workspace import WorkspaceHandlers
from ECL.foundation import EventBus
from ECL.services.game.resources import ResourceCoordinator
from ECL.services.game.workspace import WorkspaceCoordinator, resolve_instance_target


class ResourceService(ResourceCoordinator, WorkspaceCoordinator):
    pass


def _png(width: int = 1, height: int = 1, color: bytes = b"\xff\x00\x00\xff") -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0" + color))
        + chunk(b"IEND", b"")
    )


def _pack(
    root: Path,
    kind: str,
    icon: bytes | None,
    *,
    metadata: bytes | None = b'{"pack":{"description":"Test pack","pack_format":34}}',
    icon_name: str = "pack.png",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    target_path = root / ("test.zip" if kind == "zip" else "test")
    if kind == "zip":
        with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if metadata is not None:
                archive.writestr("pack.mcmeta", metadata)
            if icon is not None:
                archive.writestr(icon_name, icon)
    else:
        target_path.mkdir(exist_ok=True)
        if metadata is not None:
            (target_path / "pack.mcmeta").write_bytes(metadata)
        if icon is not None:
            icon_path = target_path / icon_name
            icon_path.parent.mkdir(parents=True, exist_ok=True)
            icon_path.write_bytes(icon)
    return target_path


@pytest.mark.parametrize("kind", ["zip", "directory"])
@pytest.mark.parametrize("is_isolated", [False, True])
def test_list_returns_actual_pack_icon_and_refreshes(tmp_path: Path, kind: str, is_isolated: bool) -> None:
    root = resolve_instance_target(tmp_path, "1.21.1", is_isolated).data_path / "resourcepacks"
    first_icon = _png()
    _pack(root, kind, first_icon)
    service = ResourceService()

    item = service.list_resources(tmp_path, "1.21.1", "resourcepack", is_isolated)[0]
    assert base64.b64decode(item["iconData"].removeprefix("data:image/png;base64,")) == first_icon
    assert item["name"] == "Test pack"

    next_icon = _png(color=b"\0\xff\0\xff")
    _pack(root, kind, next_icon)
    refreshed = service.list_resources(tmp_path, "1.21.1", "resourcepack", is_isolated)[0]
    assert base64.b64decode(refreshed["iconData"].split(",", 1)[1]) == next_icon


@pytest.mark.parametrize("kind", ["zip", "directory"])
@pytest.mark.parametrize("metadata", [None, b"not json"])
def test_icon_is_independent_of_metadata(tmp_path: Path, kind: str, metadata: bytes | None) -> None:
    root = tmp_path / "versions" / "resourcepacks"
    pack_path = _pack(root, kind, _png(), metadata=metadata)
    assert ResourceService()._read_resourcepack_icon(pack_path).startswith("data:image/png;base64,")
    assert ResourceService().list_resources(tmp_path, "1.21.1", "resourcepack") == []


@pytest.mark.parametrize("kind", ["zip", "directory"])
@pytest.mark.parametrize(
    "icon",
    [None, b"not png", b"\x89PNG\r\n\x1a\n", _png(0), _png(4097), _png(height=4097), _png() + b"\0" * (1024 * 1024)],
    ids=["missing", "invalid", "truncated", "zero-width", "wide", "tall", "oversized"],
)
def test_invalid_icons_fall_back_without_breaking_list(tmp_path: Path, kind: str, icon: bytes | None) -> None:
    root = tmp_path / "versions" / "resourcepacks"
    _pack(root, kind, icon)
    _pack(root, "zip" if kind == "directory" else "directory", _png())
    items = ResourceService().list_resources(tmp_path, "1.21.1", "resourcepack")
    assert len(items) == 2
    assert sum(item.get("iconData") is None for item in items) == 1


@pytest.mark.parametrize("kind", ["zip", "directory"])
def test_nested_icon_is_not_used(tmp_path: Path, kind: str) -> None:
    _pack(tmp_path / "versions" / "resourcepacks", kind, _png(), icon_name="nested/pack.png")
    assert ResourceService().list_resources(tmp_path, "1.21.1", "resourcepack")[0]["iconData"] is None


def test_broken_zip_has_no_icon(tmp_path: Path) -> None:
    root = tmp_path / "versions" / "resourcepacks"
    root.mkdir(parents=True)
    (root / "broken.zip").write_bytes(b"broken zip")
    assert ResourceService()._read_resourcepack_icon(root / "broken.zip") is None
    assert ResourceService().list_resources(tmp_path, "1.21.1", "resourcepack") == []


def test_icon_symlink_cannot_escape_pack(tmp_path: Path) -> None:
    pack_path = _pack(tmp_path / "versions" / "resourcepacks", "directory", None)
    outside_icon = tmp_path / "private.png"
    outside_icon.write_bytes(_png())
    try:
        (pack_path / "pack.png").symlink_to(outside_icon)
    except OSError as exc:
        pytest.skip(f"当前系统没有创建符号链接的权限：{exc}")
    assert ResourceService().list_resources(tmp_path, "1.21.1", "resourcepack")[0]["iconData"] is None


def test_datapacks_do_not_load_pack_icons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pack(tmp_path / "versions" / "saves" / "world" / "datapacks", "directory", _png())

    def unexpected_read(*_args: object) -> None:
        pytest.fail("数据包不应读取资源包图标")

    monkeypatch.setattr(ResourceService, "_read_resourcepack_icon", unexpected_read)
    item = ResourceService().list_resources(tmp_path, "1.21.1", "datapack", world_id="world")[0]
    assert "iconData" not in item


@pytest.mark.parametrize("output_format", ["json", "csv"])
def test_manifest_does_not_export_icon_data(tmp_path: Path, output_format: str) -> None:
    _pack(tmp_path / "versions" / "resourcepacks", "zip", _png())
    output_path = tmp_path / f"manifest.{output_format}"
    service = ResourceService()
    service.export_resource_manifest(tmp_path, "1.21.1", "resourcepack", output_path, output_format)
    content = output_path.read_text(encoding="utf-8")
    assert "iconData" not in content
    assert "data:image" not in content
    if output_format == "json":
        assert json.loads(content)["resources"][0]["name"] == "Test pack"
    assert service.list_resources(tmp_path, "1.21.1", "resourcepack")[0]["iconData"] is not None


async def test_resource_list_ipc_preserves_icon_data(tmp_path: Path) -> None:
    _pack(tmp_path / "versions" / "resourcepacks", "zip", _png())
    handler = WorkspaceHandlers.__new__(WorkspaceHandlers)
    handler.game = ResourceService()
    handler.logger = logging.getLogger("test.resourcepack_icons")
    handler.events = EventBus()
    response = await handler.game_resource_list(
        {
            "game_path": str(tmp_path),
            "version_id": "1.21.1",
            "resource_type": "resourcepack",
            "version_isolation": False,
        }
    )
    assert response["success"] is True
    assert response["data"][0]["iconData"].startswith("data:image/png;base64,")
