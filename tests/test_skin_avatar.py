from __future__ import annotations

import base64
import logging
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import MagicMock

import pytest
from PIL import Image
from pydantic import ValidationError

from ECL.api.files import FileHandlers
from ECL.api.models import SkinAvatarExportRequest, request_schemas
from ECL.foundation import EventBus
from ECL.services.skin_avatar import SkinAvatarError, SkinAvatarExporter


def png_bytes(size: int = 128) -> bytes:
    output = BytesIO()
    Image.new("RGBA", (size, size), (20, 40, 60, 128)).save(output, format="PNG")
    return output.getvalue()


def data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


@pytest.mark.parametrize("size", (64, 128, 256, 512))
def test_decode_preserves_exact_png_pixels_and_alpha(size: int) -> None:
    payload = png_bytes(size)
    assert SkinAvatarExporter.decode_png(data_url(payload), size) == payload
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (size, size)
        assert image.getpixel((0, 0)) == (20, 40, 60, 128)


@pytest.mark.parametrize(
    ("encoded", "size", "code"),
    (
        ("data:image/jpeg;base64,AAAA", 128, "SKIN_AVATAR_INVALID_PNG"),
        ("data:image/png;base64,%%%", 128, "SKIN_AVATAR_INVALID_PNG"),
        (data_url(b"not a png"), 128, "SKIN_AVATAR_INVALID_PNG"),
        (data_url(png_bytes()[:-15]), 128, "SKIN_AVATAR_INVALID_PNG"),
        (data_url(png_bytes(64)), 128, "SKIN_AVATAR_INVALID_SIZE"),
        (data_url(png_bytes()), 96, "SKIN_AVATAR_INVALID_SIZE"),
        ("", True, "SKIN_AVATAR_INVALID_SIZE"),
    ),
)
def test_decode_rejects_untrusted_content(encoded: str, size: int, code: str) -> None:
    with pytest.raises(SkinAvatarError) as caught:
        SkinAvatarExporter.decode_png(encoded, size)
    assert caught.value.error_code == code


def test_decode_rejects_oversized_payload_before_image_decode() -> None:
    with pytest.raises(SkinAvatarError, match="大小限制"):
        SkinAvatarExporter.decode_png("A" * (SkinAvatarExporter.max_data_url_chars + 1), 128)
    with pytest.raises(SkinAvatarError, match="大小限制"):
        SkinAvatarExporter.decode_png(data_url(b"x" * (SkinAvatarExporter.max_png_bytes + 1)), 128)


def test_decode_rejects_animated_and_non_square_png() -> None:
    output = BytesIO()
    Image.new("RGBA", (128, 128), "red").save(
        output, format="PNG", save_all=True, append_images=[Image.new("RGBA", (128, 128), "blue")]
    )
    with pytest.raises(SkinAvatarError, match="尺寸或帧数"):
        SkinAvatarExporter.decode_png(data_url(output.getvalue()), 128)
    output = BytesIO()
    Image.new("RGBA", (128, 64)).save(output, format="PNG")
    with pytest.raises(SkinAvatarError, match="尺寸或帧数"):
        SkinAvatarExporter.decode_png(data_url(output.getvalue()), 128)


def test_export_atomically_replaces_target_and_preserves_source(tmp_path: Path) -> None:
    source_path = tmp_path / "skin.png"
    target_path = tmp_path / "avatar.png"
    source_path.write_bytes(b"source skin")
    target_path.write_bytes(b"old avatar")
    payload = png_bytes()
    SkinAvatarExporter.save_png(target_path, source_path, payload)
    assert target_path.read_bytes() == payload
    assert source_path.read_bytes() == b"source skin"
    assert not list(tmp_path.glob("*.tmp"))


def test_export_rejects_source_and_hardlink_target(tmp_path: Path) -> None:
    source_path = tmp_path / "skin.png"
    source_path.write_bytes(b"source skin")
    with pytest.raises(SkinAvatarError, match="不能覆盖"):
        SkinAvatarExporter.save_png(source_path, source_path, png_bytes())
    linked_path = tmp_path / "linked.png"
    linked_path.hardlink_to(source_path)
    with pytest.raises(SkinAvatarError, match="不能覆盖"):
        SkinAvatarExporter.save_png(linked_path, source_path, png_bytes())
    assert source_path.read_bytes() == b"source skin"


@pytest.mark.parametrize(
    ("source", "target", "should_conflict"),
    (
        (PureWindowsPath("C:/Skins/skin.png"), PureWindowsPath("c:/skins/SKIN.PNG"), True),
        (PurePosixPath("/skins/skin.png"), PurePosixPath("/skins/skin.png"), True),
        (PurePosixPath("/skins/skin.png"), PurePosixPath("/skins/SKIN.PNG"), False),
    ),
)
def test_source_protection_respects_platform_path_semantics(source, target, should_conflict, monkeypatch) -> None:
    source_path = MagicMock(spec=Path)
    source_path.is_absolute.return_value = True
    source_path.resolve.return_value = source
    target_path = MagicMock(spec=Path)
    target_path.suffix = ".png"
    target_path.is_absolute.return_value = True
    target_path.resolve.return_value = target
    target_path.exists.return_value = False
    write = MagicMock()
    monkeypatch.setattr("ECL.services.skin_avatar.atomic_write_bytes", write)
    if should_conflict:
        with pytest.raises(SkinAvatarError, match="不能覆盖"):
            SkinAvatarExporter.save_png(target_path, source_path, b"validated")
        write.assert_not_called()
    else:
        SkinAvatarExporter.save_png(target_path, source_path, b"validated")
        write.assert_called_once()


def test_export_failure_preserves_existing_target(tmp_path: Path, monkeypatch) -> None:
    target_path = tmp_path / "avatar.png"
    target_path.write_bytes(b"old avatar")

    def fail_replace(self: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(type(target_path), "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        SkinAvatarExporter.save_png(target_path, tmp_path / "skin.png", png_bytes())
    assert target_path.read_bytes() == b"old avatar"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["avatar.png"]


def test_export_rejects_non_png_extension_and_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(SkinAvatarError, match=r"\.png"):
        SkinAvatarExporter.save_png(tmp_path / "avatar.jpg", tmp_path / "skin.png", png_bytes())
    with pytest.raises(SkinAvatarError, match="路径无效"):
        SkinAvatarExporter.save_png(Path("avatar.png"), tmp_path / "skin.png", png_bytes())


def test_export_request_rejects_target_path_invalid_size_and_source(tmp_path: Path) -> None:
    request = {"data_url": data_url(png_bytes()), "size": 128, "source_path": str(tmp_path / "skin.png")}
    assert SkinAvatarExportRequest.model_validate(request).size == 128
    for extra in (
        {"target_path": str(tmp_path / "avatar.png")},
        {"size": 96},
        {"size": "128"},
        {"source_path": "skin.png"},
        {"source_path": "bad\0.png"},
    ):
        with pytest.raises(ValidationError):
            SkinAvatarExportRequest.model_validate(request | extra)
    assert "skin_avatar_export" in request_schemas()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", (False, True))
async def test_export_ipc_uses_native_png_dialog_and_treats_cancel_as_normal(tmp_path, monkeypatch, cancelled) -> None:
    handler = object.__new__(FileHandlers)
    handler.logger = logging.getLogger("test.skin-avatar")
    handler.events = EventBus()
    handler._webview = object()
    target_path = tmp_path / "avatar.png"
    picker = MagicMock()
    picker.blocking_save_file.return_value = None if cancelled else str(target_path)
    monkeypatch.setattr("ECL.api.files.DialogExt.file", lambda _webview: picker)
    request = {"data_url": data_url(png_bytes()), "size": 128, "source_path": str(tmp_path / "skin.png")}
    result = await handler.skin_avatar_export(request)
    assert result == {"success": True, "data": {"path": None if cancelled else str(target_path)}}
    picker.blocking_save_file.assert_called_once_with(
        add_filter=("PNG 图片", ["png"]), set_file_name="skin-avatar.png", set_title="导出皮肤头像"
    )
    assert target_path.exists() is not cancelled
    if not cancelled:
        assert target_path.read_bytes() == png_bytes()


@pytest.mark.asyncio
async def test_export_ipc_rejects_invalid_image_before_opening_dialog(tmp_path, monkeypatch) -> None:
    handler = object.__new__(FileHandlers)
    handler.logger = logging.getLogger("test.skin-avatar")
    handler.events = EventBus()
    handler._webview = object()
    picker = MagicMock()
    monkeypatch.setattr("ECL.api.files.DialogExt.file", picker)
    result = await handler.skin_avatar_export(
        {"data_url": data_url(b"invalid"), "size": 128, "source_path": str(tmp_path / "skin.png")}
    )
    assert result["success"] is False
    assert result["errorCode"] == "SKIN_AVATAR_INVALID_PNG"
    picker.assert_not_called()
