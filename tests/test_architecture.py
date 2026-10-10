# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 architecture 模块的自动化测试。
#
# 公开接口：
#   - ecl_root（路径夹具）
#   - test_application_imports_game_only_through_public_entrypoint() -> None
#   - test_no_service_locator_api_or_duplicate_libs_remains() -> None
#   - test_no_internal_compatibility_aliases_remain() -> None
#   - test_ipc_registry_has_no_retired_compatibility_commands() -> None
#   - test_game_package_root_contains_only_public_boundary_modules() -> None
#   - test_game_service_raw_body_is_not_typed_as_any() -> None
#   - test_production_docstrings_are_multiline_and_have_no_placeholders() -> None
# ============================================================

from __future__ import annotations

import ast
from pathlib import Path

import ECL

ecl_root = Path(ECL.__file__).parent


def _python_files() -> list[Path]:
    submodule_roots = (ecl_root / "game", ecl_root / "services" / "florolding")
    return [path for path in ecl_root.rglob("*.py") if not any(path.is_relative_to(root) for root in submodule_roots)]


def test_application_imports_game_only_through_public_entrypoint() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ECL.game."):
                violations.append(f"{path}:{node.lineno}:{node.module}")
            if isinstance(node, ast.Import):
                violations.extend(
                    f"{path}:{node.lineno}:{alias.name}" for alias in node.names if alias.name.startswith("ECL.game.")
                )
    assert violations == []


def test_no_service_locator_api_or_duplicate_libs_remains() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in _python_files())

    assert "register_services" not in source
    assert "EventBus().get(" not in source
    assert "EventBus().register(" not in source
    assert [path for path in ecl_root.rglob("Libs.py") if not path.is_relative_to(ecl_root / "game")] == []
    assert not (ecl_root / "api" / "legacy").exists()
    assert not (ecl_root / "api" / "domain_handlers.py").exists()


def test_no_internal_compatibility_aliases_remain() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in _python_files())

    for alias in ("ConfigManager", "EnvManager", "LoggerManager", "PluginFramework"):
        assert alias not in source


def test_ipc_registry_has_no_retired_compatibility_commands() -> None:
    from ECL.api.registry import IpcCommandRegistry

    retired = {
        "connector_match_instances",
        "connector_easytier_status",
        "connector_easytier_download",
        "config_get",
        "config_set",
        "config_list",
        "config_get_all",
        "config_get_many",
        "minecraft_versions",
        "minecraft_versions_classified",
        "fabric_versions",
        "forge_versions",
        "neoforge_versions",
        "optifine_versions",
        "quilt_versions",
        "scan_versions",
        "install_version",
        "uninstall_version",
        "java_scan",
        "java_list",
        "ecl_config_get",
        "ecl_config_set",
        "ecl_config_patch",
        "instances_list",
        "launch_instance",
        "cancel_launch",
        "instance_stop",
    }

    assert retired.isdisjoint(IpcCommandRegistry.command_names)


def test_game_package_root_contains_only_public_boundary_modules() -> None:
    root_modules = {path.name for path in (ecl_root / "game").glob("*.py")}

    assert root_modules == {"__init__.py"}


def test_game_service_raw_body_is_not_typed_as_any() -> None:
    methods = {}
    for path in (ecl_root / "services" / "game").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        methods.update(
            {
                node.name: node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {"install_version", "launch_instance"}
            }
        )

    for method in methods.values():
        body = next(argument for argument in method.args.args if argument.arg == "body")
        assert "Any" not in ast.unparse(body.annotation)


def test_production_docstrings_are_multiline_and_have_no_placeholders() -> None:
    violations: list[str] = []
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        assert "???" not in source
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not node.body:
                continue
            expression = node.body[0]
            if (
                isinstance(expression, ast.Expr)
                and isinstance(expression.value, ast.Constant)
                and isinstance(expression.value.value, str)
                and expression.lineno == expression.end_lineno
            ):
                violations.append(f"{path}:{node.lineno}:{node.name}")
    assert violations == []
