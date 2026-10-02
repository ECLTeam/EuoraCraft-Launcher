# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 config_paths 模块的自动化测试。
#
# 公开接口：
#   - test_new_config_uses_absolute_minecraft_path_and_creates_directory(tmp_path) -> None
#   - test_existing_config_preserves_saved_window_chrome(tmp_path, window_chrome) -> None
#   - test_existing_empty_game_paths_are_initialized(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from ECL.utils import ConfigStore
from ECL.utils.config import default_config


def test_new_config_uses_absolute_minecraft_path_and_creates_directory(tmp_path) -> None:
    data_path = tmp_path / "ECL_data"
    manager = ConfigStore(data_path)

    config = manager.get_config()
    minecraft_path = (tmp_path / ".minecraft").resolve()

    assert config["game"]["minecraft_paths"] == [{"name": "默认路径", "path": str(minecraft_path)}]
    assert config["game"]["last_install_path"] == str(minecraft_path)
    assert config["game"]["last_manage_path"] == str(minecraft_path)
    assert config["ui"]["theme"]["window_chrome"] == "system_shadow"
    assert minecraft_path.is_dir()
    assert minecraft_path.is_absolute()

    saved_config = json.loads((data_path / "setting.json").read_text(encoding="utf-8"))
    assert saved_config["game"]["minecraft_paths"] == [{"name": "默认路径", "path": str(minecraft_path)}]
    assert saved_config["ui"]["theme"]["window_chrome"] == "system_shadow"


@pytest.mark.parametrize("window_chrome", ["custom", "native", "system_shadow"])
def test_existing_config_preserves_saved_window_chrome(tmp_path, window_chrome: str) -> None:
    data_path = tmp_path / "ECL_data"
    data_path.mkdir()
    existing_config = deepcopy(default_config)
    existing_config["ui"]["theme"]["window_chrome"] = window_chrome
    config_path = data_path / "setting.json"
    config_path.write_text(json.dumps(existing_config), encoding="utf-8")

    loaded_config = ConfigStore(data_path).get_config()
    saved_config = json.loads(config_path.read_text(encoding="utf-8"))

    assert loaded_config["ui"]["theme"]["window_chrome"] == window_chrome
    assert saved_config["ui"]["theme"]["window_chrome"] == window_chrome


def test_existing_empty_game_paths_are_initialized(tmp_path) -> None:
    data_path = tmp_path / "ECL_data"
    data_path.mkdir()
    (data_path / "setting.json").write_text(
        json.dumps(default_config, ensure_ascii=False),
        encoding="utf-8",
    )

    config = ConfigStore(data_path).get_config()
    minecraft_path = (tmp_path / ".minecraft").resolve()

    assert config["game"]["minecraft_paths"] == [{"name": "默认路径", "path": str(minecraft_path)}]
    assert config["game"]["last_install_path"] == str(minecraft_path)
    assert config["game"]["last_manage_path"] == str(minecraft_path)
    assert minecraft_path.is_dir()
