# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 crash 规则目录的自动化测试。
#
# 公开接口：
#   - test_catalog_codes_are_unique_and_groups_declared() -> None
#   - test_named_group_samples_extract_parameters(tmp_path, text, expected_code, expected_parameters) -> None
#   - test_multiline_pattern_produces_evidence_and_reason_parameter(tmp_path) -> None
# ============================================================

from __future__ import annotations

from pathlib import Path

import pytest

from ECL.services.game.crash.analyzer import CrashAnalyzer
from ECL.services.game.crash.rules import CrashRuleCatalog


def test_catalog_codes_are_unique_and_groups_declared() -> None:
    codes = [rule.code for rule in CrashRuleCatalog.rules]

    assert len(codes) == len(set(codes))
    for rule in CrashRuleCatalog.rules:
        declared = set(rule.parameter_groups)
        available = {name for pattern in rule.patterns for name in pattern.groupindex}
        assert declared <= available, f"规则 {rule.code} 声明了未定义的命名捕获组"


@pytest.mark.parametrize(
    ("text", "expected_code", "expected_parameters"),
    [
        (
            "java.lang.UnsatisfiedLinkError: Failed to locate library: lwjgl.dll",
            "native.library_missing",
            {"library": "lwjgl.dll"},
        ),
        (
            "java.lang.SecurityException: SHA1 digest error for examplemod.jar",
            "files.integrity_failure",
            {"file": "examplemod.jar"},
        ),
        (
            "java.nio.file.FileAlreadyExistsException: config/example.toml",
            "files.already_exists",
            {"file": "config/example.toml"},
        ),
        (
            "java.lang.UnsupportedClassVersionError: Main has been compiled by a more recent version of the Java"
            " Runtime (class file version 65.0), this version of the Java Runtime only recognizes class file versions"
            " up to 52.0",
            "java.incompatible_version",
            {"expected": "65"},
        ),
        (
            "java.lang.ClassCastException: class jdk.internal.ref.CleanerImpl cannot be cast to class foo.Bar",
            "java.module_access",
            {},
        ),
        (
            "LoaderExceptionModCrash: Caught exception from Just Enough Items (jei)",
            "mod.initialization_failure",
            {"mod_name": "Just Enough Items", "mod_id": "jei"},
        ),
        (
            "Could not find required mod: emi requires jei",
            "mod.missing_dependency",
            {"source_mod": "emi", "target_mod": "jei"},
        ),
        (
            "Found conflicting mods: sodium conflicts with optifine",
            "mod.incompatible",
            {"source_mod": "sodium", "conflicting_mod": "optifine"},
        ),
        (
            "Found a duplicate mod examplemod at C:/mods/examplemod-1.0.jar",
            "mod.duplicate",
            {"name": "examplemod", "path": "C:/mods/examplemod-1.0.jar"},
        ),
        (
            "java.lang.NoClassDefFoundError: net/example/CrashMod",
            "mod.class_resolution_failure",
            {"class": "net/example/CrashMod"},
        ),
        ("Maximum id range exceeded", "mod.id_limit_exceeded", {}),
        ("Invalid module name: '我的模组' is not a Java identifier", "mod.invalid_module_name", {"name": "'我的模组'"}),
        ("ResolutionException: Module optifine reads another module named optifine", "mod.optifine_duplicate", {}),
        ("RivaTuner Statistics Server (RTSS) is not compatible with Sodium", "graphics.rtss_sodium", {}),
        (
            "java.lang.IllegalStateException: GLFW error before init: [0x10008]Cocoa: Failed to find service port"
            " for display",
            "graphics.macos_glfw",
            {},
        ),
        (
            "Terminating app due to uncaught exception 'NSInternalInconsistencyException', reason: 'NSWindow drag"
            " regions should only be invalidated on the Main Thread!'",
            "java.mac_jdk_legacy",
            {},
        ),
        (
            "Block: Block{minecraft:water}\nBlock location: World: (-1,64,-1), Section: (0,4,0)",
            "world.block_failure",
            {"block": "Block{minecraft:water}", "location": "(-1,64,-1), Section: (0,4,0)"},
        ),
        (
            "Entity Type: minecraft:zombie\nEntity's Exact location: 1.5, 64.0, 2.5",
            "world.entity_failure",
            {"entity": "minecraft:zombie", "location": "1.5, 64.0, 2.5"},
        ),
    ],
)
def test_named_group_samples_extract_parameters(
    tmp_path: Path, text: str, expected_code: str, expected_parameters: dict[str, str]
) -> None:
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        reasons = analyzer._collect_reasons(text)
    finally:
        analyzer.close()

    assert reasons and reasons[0]["code"] == expected_code
    for key, value in expected_parameters.items():
        assert reasons[0]["parameters"][key] == value


def test_multiline_pattern_produces_evidence_and_reason_parameter(tmp_path: Path) -> None:
    text = (
        "An exception was thrown, the game will display an error screen and halt.\n"
        "java.lang.RuntimeException: boom\n"
        "\tat a.b.CrashSources.crash(CrashSources.java:1)"
    )
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        reasons = analyzer._collect_reasons(text)
    finally:
        analyzer.close()

    assert reasons and reasons[0]["code"] == "loader.forge_error_screen"
    assert reasons[0]["parameters"]["reason"] == "java.lang.RuntimeException: boom"
    assert reasons[0]["evidence"][0].startswith("An exception was thrown")
