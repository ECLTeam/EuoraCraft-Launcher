from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ECL.utils.config import ConfigStore
from ECL.utils.download_settings import DownloadSettingsPatch, resolve_mirror_source
from ECL.utils.errors import ConfigError


def test_concurrent_download_patches_preserve_each_resource_type(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "data")
    initial = store.get_config("download")
    store.save_config("download", {**initial, "extension": {"keep": True}})
    patches = [
        DownloadSettingsPatch.model_validate({"resourceInstallCache": {kind: {"gamePath": kind, "versionId": "demo"}}})
        for kind in ("mod", "resourcepack", "shaderpack")
    ]
    patches.append(DownloadSettingsPatch(mirror_source="bmclapi"))
    patches.append(DownloadSettingsPatch(mod_source="mcim"))
    patches.append(DownloadSettingsPatch(resourceSaveDirectories={"mod": "saved/mods"}))
    with ThreadPoolExecutor(max_workers=5) as workers:
        list(workers.map(store.patch_download, patches))
    saved = store.get_config("download")
    assert set(saved["resourceInstallCache"]) == {"mod", "resourcepack", "shaderpack"}
    assert saved["mirror_source"] == "bmclapi"
    assert saved["mod_source"] == "mcim"
    assert saved["extension"] == {"keep": True}
    assert saved["resourceSaveDirectories"] == {"mod": "saved/mods"}
    assert json.loads(store.config_path.read_text(encoding="utf-8"))["download"] == saved


def test_download_patch_failure_preserves_memory_disk_and_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ConfigStore(tmp_path / "data")
    before = store.get_config()
    before_bytes = store.config_path.read_bytes()
    payload = {"resourceInstallCache": {"mod": {"gamePath": "", "versionId": ""}}}
    original_payload = deepcopy(payload)
    patch = DownloadSettingsPatch.model_validate(payload)

    def fail_write(_snapshot: dict[str, object]) -> None:
        raise ConfigError("test write failure")

    monkeypatch.setattr(store, "_write_config", fail_write)
    with pytest.raises(ConfigError):
        store.patch_download(patch)
    assert store.get_config() == before
    assert store.config_path.read_bytes() == before_bytes
    assert payload == original_payload


@pytest.mark.parametrize(
    "payload",
    [
        {"mirror_source": "unknown"},
        {"mirror_source": None},
        {"mod_source": "unknown"},
        {"mod_source": "bmclapi"},
        {"mod_source": None},
        {"resourceInstallCache": None},
        {"resourceInstallCache": {"mod": {"gamePath": "bad\0path", "versionId": "demo"}}},
        {"resourceSaveDirectories": {"": "path"}},
        {"resourceSaveDirectories": {"mod": "bad\0path"}},
        {"unsupported": True},
    ],
)
def test_download_patch_rejects_invalid_boundary_input(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DownloadSettingsPatch.model_validate(payload)


def test_empty_binding_clears_only_requested_resource_type(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "data")
    store.patch_download(
        DownloadSettingsPatch(
            resourceInstallCache={
                "mod": {"gamePath": "A", "versionId": "demo"},
                "resourcepack": {"gamePath": "B", "versionId": "demo"},
            }
        )
    )
    saved = store.patch_download(DownloadSettingsPatch(resourceInstallCache={"mod": {"gamePath": "", "versionId": ""}}))
    assert saved["resourceInstallCache"] == {
        "mod": {"gamePath": "", "versionId": ""},
        "resourcepack": {"gamePath": "B", "versionId": "demo"},
    }


@pytest.mark.parametrize("locale", ["zh-TW", "en-US", "ja-JP", "ru-RU", "de-DE"])
def test_mirror_source_falls_back_to_official_outside_simplified_chinese(locale: str) -> None:
    configuration = {"download": {"mirror_source": "bmclapi"}, "ui": {"locale": locale}}

    assert resolve_mirror_source(configuration) == "official"


def test_mirror_source_keeps_saved_choice_in_simplified_chinese() -> None:
    configuration = {"download": {"mirror_source": "bmclapi"}, "ui": {"locale": "zh-CN"}}

    assert resolve_mirror_source(configuration) == "bmclapi"


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({}, "official"),
        ({"download": {"mirror_source": "bmclapi"}}, "bmclapi"),
        ({"download": {"mirror_source": "bmclapi"}, "ui": {"locale": None}}, "bmclapi"),
        ({"download": {"mirror_source": "unknown"}, "ui": {"locale": "zh-CN"}}, "official"),
        ({"download": None, "ui": {"locale": "zh-CN"}}, "official"),
    ],
)
def test_mirror_source_falls_back_to_official_without_valid_saved_value(
    configuration: dict[str, object], expected: str
) -> None:
    # 缺少界面语言时按默认的简体中文处理，只校验保存值；非法保存值不传递给下载服务。
    assert resolve_mirror_source(configuration) == expected


def test_resolving_mirror_source_does_not_change_saved_config(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "data")
    store.patch_download(DownloadSettingsPatch(mirror_source="bmclapi"))
    before_bytes = store.config_path.read_bytes()

    assert resolve_mirror_source({**store.get_config(), "ui": {"locale": "en-US"}}) == "official"
    assert store.get_config("download")["mirror_source"] == "bmclapi"
    assert store.config_path.read_bytes() == before_bytes
