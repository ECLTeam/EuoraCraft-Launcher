from __future__ import annotations

import base64
import hashlib
import json
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from ECL.utils.errors import GameServiceError
from ECL.utils.instance_shortcuts import InstanceShortcutService, ShortcutIcon


def test_shortcut_command_quotes_unicode_and_spaces(tmp_path: Path) -> None:
    service = InstanceShortcutService(
        tmp_path / "用户 data", tmp_path, is_frozen=False, app_path=tmp_path / "launcher source"
    )
    executable, arguments = service.launch_command(tmp_path / "游戏根" / "versions" / "My Snapshot")
    assert executable.is_absolute()
    assert '"' in arguments
    assert "--data-dir" in arguments and "--launch" in arguments
    assert "main.py" in arguments and "My Snapshot" in arguments
    frozen = InstanceShortcutService(tmp_path, tmp_path, is_frozen=True, app_path=tmp_path)
    assert "main.py" not in frozen.launch_command(tmp_path / "versions" / "Foo")[1]


def test_posix_rejects_without_creating_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    with pytest.raises(GameServiceError) as error:
        service.create(tmp_path / "instance", "Foo", ShortcutIcon("builtin", "grass"))
    assert error.value.error_code == "SHORTCUT_PLATFORM_UNSUPPORTED"
    assert not list(tmp_path.iterdir())


def test_data_icon_converts_to_stable_ico(tmp_path: Path) -> None:
    image = Image.new("RGBA", (32, 32), (40, 140, 240, 255))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    icon = ShortcutIcon("data", "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    first = service._cache_icon(icon)
    assert service._cache_icon(icon) == first
    with Image.open(first) as converted:
        assert converted.format == "ICO"
        assert converted.size == (256, 256)


@pytest.mark.parametrize("source_size", [32, 64, 512])
def test_shortcut_icon_fills_canvas_at_every_windows_size(tmp_path: Path, source_size: int) -> None:
    image = Image.new("RGBA", (source_size, source_size), (40, 140, 240, 255))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    icon = ShortcutIcon("data", "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    icon_file = service._cache_icon(icon)
    with Image.open(icon_file) as converted:
        largest = converted.ico.getimage((256, 256)).convert("RGBA")
        assert largest.getchannel("A").getbbox() == (0, 0, 256, 256)
        expected_sizes = {(size, size) for size in (16, 24, 32, 48, 64, 96, 128, 256)}
        assert converted.ico.sizes() == expected_sizes
        for width, height in expected_sizes:
            layer = converted.ico.getimage((width, height)).convert("RGBA")
            assert layer.getchannel("A").getbbox() == (0, 0, width, height)


def test_shortcut_icon_preserves_aspect_ratio_and_centers_content(tmp_path: Path) -> None:
    image = Image.new("RGBA", (64, 32), (40, 140, 240, 255))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    icon = ShortcutIcon("data", "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    with Image.open(service._cache_icon(icon)) as converted:
        layer = converted.ico.getimage((256, 256)).convert("RGBA")
        assert layer.getchannel("A").getbbox() == (0, 64, 256, 192)


def test_shortcut_icon_preserves_original_transparent_margins(tmp_path: Path) -> None:
    image = Image.new("RGBA", (64, 64))
    image.paste((40, 140, 240, 255), (16, 16, 48, 48))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    icon = ShortcutIcon("data", "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    with Image.open(service._cache_icon(icon)) as converted:
        layer = converted.ico.getimage((256, 256)).convert("RGBA")
        opaque_bounds = layer.getchannel("A").point(lambda value: 255 if value >= 128 else 0).getbbox()
        assert opaque_bounds == (64, 64, 192, 192)
        assert layer.getpixel((0, 0))[3] == 0


def test_shortcut_icon_uses_new_cache_and_keeps_existing_icon(tmp_path: Path) -> None:
    image = Image.new("RGBA", (32, 32), (40, 140, 240, 255))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    image_bytes = buffer.getvalue()
    old_cache_file = tmp_path / "shortcut-icons" / f"{hashlib.sha256(image_bytes).hexdigest()}.ico"
    old_cache_file.parent.mkdir()
    old_cache_file.write_bytes(b"previous shortcut icon")
    icon = ShortcutIcon("data", "data:image/png;base64," + base64.b64encode(image_bytes).decode())
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    icon_file = service._cache_icon(icon)
    assert icon_file != old_cache_file
    assert old_cache_file.read_bytes() == b"previous shortcut icon"
    original_bytes = icon_file.read_bytes()
    original_mtime = icon_file.stat().st_mtime_ns
    assert service._cache_icon(icon) == icon_file
    assert icon_file.read_bytes() == original_bytes
    assert icon_file.stat().st_mtime_ns == original_mtime


@pytest.mark.parametrize("key", ["grass", "command", "fabric"])
def test_builtin_shortcut_icon_has_normal_content_size(tmp_path: Path, key: str) -> None:
    resource_path = Path(__file__).resolve().parents[1]
    service = InstanceShortcutService(tmp_path, resource_path, is_frozen=False, app_path=resource_path)
    with Image.open(service._cache_icon(ShortcutIcon("builtin", key))) as converted:
        layer = converted.ico.getimage((256, 256)).convert("RGBA")
        left, top, right, bottom = layer.getchannel("A").getbbox()
        assert right - left >= 240
        assert bottom - top >= 240


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM 快捷方式验证")
def test_real_windows_shortcut_is_created_and_existing_file_is_preserved(tmp_path: Path) -> None:
    resource_path = Path(__file__).resolve().parents[1]
    service = InstanceShortcutService(tmp_path / "data", resource_path, is_frozen=False, app_path=resource_path)
    target = tmp_path / "快捷方式目录" / "My Instance.lnk"
    result = service.create(tmp_path / "版本 with space", "自定义实例", ShortcutIcon("builtin", "command"), target)
    assert Path(result["path"]).is_file()
    assert Path(result["iconPath"]).is_file()
    link_details = json.loads(
        service._powershell(
            """
            $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut($request.path)
            $link | Select-Object TargetPath, Arguments, WorkingDirectory, IconLocation | ConvertTo-Json -Compress
            """,
            {"path": result["path"]},
        ).stdout
    )
    executable, arguments = service.launch_command(tmp_path / "版本 with space")
    assert Path(link_details["TargetPath"]).resolve() == executable.resolve()
    assert link_details["Arguments"] == arguments
    assert Path(link_details["WorkingDirectory"]).resolve() == resource_path
    assert Path(link_details["IconLocation"].rsplit(",", 1)[0]) == Path(result["iconPath"])
    assert Path(result["iconPath"]).parent.name == "v2"
    original = target.read_bytes()
    with pytest.raises(GameServiceError, match="已存在"):
        service.create(tmp_path / "another", "Other", ShortcutIcon("builtin", "grass"), target)
    assert target.read_bytes() == original


def test_onefile_shortcut_targets_original_executable_not_temporary_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "installed" / "Launcher.exe"
    payload = tmp_path / "temporary" / "main.exe"
    original.parent.mkdir()
    payload.parent.mkdir()
    original.touch()
    payload.touch()
    monkeypatch.setattr(sys, "executable", str(payload))
    monkeypatch.setattr(sys, "argv", [str(original), "--debug"])
    service = InstanceShortcutService(tmp_path / "data", payload.parent, is_frozen=True, app_path=original.parent)
    assert service.launch_command(tmp_path / "instance")[0] == original
