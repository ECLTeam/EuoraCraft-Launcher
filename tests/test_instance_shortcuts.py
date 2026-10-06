from __future__ import annotations

import base64
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from ECL.utils.errors import GameServiceError
from ECL.utils.instance_shortcuts import InstanceShortcutService, ShortcutIcon


def test_shortcut_command_quotes_unicode_and_spaces(tmp_path) -> None:
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


def test_posix_rejects_without_creating_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    service = InstanceShortcutService(tmp_path, tmp_path, is_frozen=False, app_path=tmp_path)
    with pytest.raises(GameServiceError) as error:
        service.create(tmp_path / "instance", "Foo", ShortcutIcon("builtin", "grass"))
    assert error.value.error_code == "SHORTCUT_PLATFORM_UNSUPPORTED"
    assert not list(tmp_path.iterdir())


def test_data_icon_converts_to_stable_ico(tmp_path) -> None:
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM 快捷方式验证")
def test_real_windows_shortcut_is_created_and_existing_file_is_preserved(tmp_path) -> None:
    resource_path = Path(__file__).resolve().parents[1]
    service = InstanceShortcutService(tmp_path / "data", resource_path, is_frozen=False, app_path=resource_path)
    target = tmp_path / "快捷方式目录" / "My Instance.lnk"
    result = service.create(tmp_path / "版本 with space", "自定义实例", ShortcutIcon("builtin", "command"), target)
    assert Path(result["path"]).is_file()
    assert Path(result["iconPath"]).is_file()
    original = target.read_bytes()
    with pytest.raises(GameServiceError, match="已存在"):
        service.create(tmp_path / "another", "Other", ShortcutIcon("builtin", "grass"), target)
    assert target.read_bytes() == original


def test_onefile_shortcut_targets_original_executable_not_temporary_payload(tmp_path, monkeypatch) -> None:
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
