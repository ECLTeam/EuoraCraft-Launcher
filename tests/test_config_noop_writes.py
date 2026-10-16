from __future__ import annotations

from pathlib import Path

import pytest

from ECL.foundation import EventBus
from ECL.utils.config import ConfigStore
from ECL.utils.download_settings import DownloadSettingsPatch


def test_identical_section_save_does_not_write_or_broadcast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    store = ConfigStore(tmp_path / "data", bus)
    current = store.get_config("ui")
    notifications: list[str] = []
    bus.subscribe("config:updated", lambda section, _snapshot: notifications.append(section))

    def unexpected_write(_snapshot: object) -> None:
        pytest.fail("相同配置不应重新写入文件")

    monkeypatch.setattr(store, "_write_config", unexpected_write)
    store.save_config("ui", current)
    assert notifications == []


def test_identical_download_patch_does_not_write_or_broadcast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    store = ConfigStore(tmp_path / "data", bus)
    current = store.get_config("download")
    notifications: list[str] = []
    bus.subscribe("config:updated", lambda section, _snapshot: notifications.append(section))

    def unexpected_write(_snapshot: object) -> None:
        pytest.fail("相同下载配置不应重新写入文件")

    monkeypatch.setattr(store, "_write_config", unexpected_write)
    assert store.patch_download(DownloadSettingsPatch(mirror_source=current["mirror_source"])) == current
    assert notifications == []
