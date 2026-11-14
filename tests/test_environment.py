# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 environment 模块的自动化测试。
#
# 公开接口：
#   - test_apply_to_config_maps_non_default_section_to_nested_dict(tmp_path, monkeypatch) -> None
#   - test_apply_to_config_preserves_underscored_leaf_key(tmp_path, monkeypatch) -> None
#   - test_env_manager_reads_microsoft_client_id_from_dotenv(tmp_path, monkeypatch) -> None
#   - test_system_microsoft_client_id_overrides_dotenv(tmp_path, monkeypatch) -> None
#   - test_env_manager_reads_system_variables_without_dotenv(tmp_path, monkeypatch) -> None
#   - test_env_manager_reads_curseforge_api_key_from_dotenv(tmp_path, monkeypatch) -> None
#   - test_system_curseforge_api_key_overrides_dotenv(tmp_path, monkeypatch) -> None
#   - test_env_manager_reads_curseforge_api_key_from_ecl_prefix(tmp_path, monkeypatch) -> None
# ============================================================

from ECL.utils.config import default_config
from ECL.utils.environment import Environment


def test_apply_to_config_maps_non_default_section_to_nested_dict(tmp_path, monkeypatch) -> None:
    # tauri 分区不在默认配置中，但启动器读取 tauri.frontenddist 决定前端来源；
    # 环境变量覆盖必须仍能寻址到嵌套的 tauri.frontenddist，而非扁平键 tauri_frontenddist。
    monkeypatch.setenv("ECL_CONFIG_TAURI_FRONTENDDIST", "http://localhost:5173/")
    manager = Environment(tmp_path)

    result = manager.apply_to_config(default_config)

    assert result["tauri"]["frontenddist"] == "http://localhost:5173/"
    assert "tauri_frontenddist" not in result


def test_apply_to_config_preserves_underscored_leaf_key(tmp_path, monkeypatch) -> None:
    # 含下划线的叶子键应保持真实键名，而不是被拆成多层嵌套。
    monkeypatch.setenv("ECL_CONFIG_LAUNCHER_DEBUG_LOG_LEVEL", "debug")
    manager = Environment(tmp_path)

    result = manager.apply_to_config(default_config)

    assert result["launcher"]["debug_log_level"] == "debug"
    # 不应把 debug_log_level 拆成 launcher.debug.log.level：debug 仍保持默认布尔值而非嵌套字典。
    assert result["launcher"]["debug"] is False


def test_env_manager_reads_microsoft_client_id_from_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MICROSOFT_CLIENT_ID", raising=False)
    (tmp_path / ".env").write_text("MICROSOFT_CLIENT_ID=dotenv-client-id\n", encoding="utf-8")

    manager = Environment(tmp_path)

    assert manager.get_value("MICROSOFT_CLIENT_ID") == "dotenv-client-id"


def test_system_microsoft_client_id_overrides_dotenv(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("MICROSOFT_CLIENT_ID=dotenv-client-id\n", encoding="utf-8")
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "system-client-id")

    manager = Environment(tmp_path)

    assert manager.get_value("MICROSOFT_CLIENT_ID") == "system-client-id"


def test_env_manager_reads_system_variables_without_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "system-only-client-id")

    manager = Environment(tmp_path)

    assert manager.get_value("MICROSOFT_CLIENT_ID") == "system-only-client-id"


def test_env_manager_reads_curseforge_api_key_from_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    monkeypatch.delenv("ECL_CURSEFORGE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("CURSEFORGE_API_KEY=dotenv-cf-key\n", encoding="utf-8")

    manager = Environment(tmp_path)

    assert manager.get_value("CURSEFORGE_API_KEY") == "dotenv-cf-key"


def test_system_curseforge_api_key_overrides_dotenv(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("CURSEFORGE_API_KEY=dotenv-cf-key\n", encoding="utf-8")
    monkeypatch.setenv("CURSEFORGE_API_KEY", "system-cf-key")

    manager = Environment(tmp_path)

    assert manager.get_value("CURSEFORGE_API_KEY") == "system-cf-key"


def test_env_manager_reads_curseforge_api_key_from_ecl_prefix(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    monkeypatch.setenv("ECL_CURSEFORGE_API_KEY", "ecl-prefix-cf-key")

    manager = Environment(tmp_path)

    assert manager.get_value("CURSEFORGE_API_KEY") == "ecl-prefix-cf-key"
