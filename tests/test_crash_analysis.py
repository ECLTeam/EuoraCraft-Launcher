# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 crash 子包分析器的自动化测试。
#
# 公开接口：
#   - test_manual_analysis_matches_structured_rules(tmp_path, line, expected_code) -> None
#   - test_runtime_analysis_filters_old_logs_and_redacts_secrets(tmp_path) -> None
#   - test_archive_rejects_path_traversal(tmp_path) -> None
#   - test_archive_rejects_nested_archives_and_binary_files(tmp_path) -> None
#   - test_manual_file_rejects_oversize_input(tmp_path, monkeypatch) -> None
#   - test_stack_fallback_maps_package_to_fabric_mod_metadata(tmp_path) -> None
#   - test_report_is_session_only_and_removed_on_close(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from ECL.services.game import GameServiceError
from ECL.services.game.crash import analyzer as crash_analyzer_module
from ECL.services.game.crash.analyzer import CrashAnalyzer


def _game(tmp_path: Path) -> tuple[Path, Path]:
    game_path = tmp_path / ".minecraft"
    version_path = game_path / "versions" / "Test"
    version_path.mkdir(parents=True)
    (version_path / "Test.json").write_text('{"id":"Test","accessToken":"secret"}', encoding="utf-8")
    return game_path, version_path


@pytest.mark.parametrize(
    ("line", "expected_code"),
    [
        ("Error: Could not create the Java Virtual Machine", "jvm.invalid_arguments"),
        ("java.lang.OutOfMemoryError: Java heap space", "memory.out_of_memory"),
        ("The driver does not appear to support OpenGL", "graphics.opengl_unsupported"),
        ("net.minecraftforge.fml.loading.DuplicateModsFoundException", "mod.duplicate"),
        ("Missing or unsupported mandatory dependencies", "mod.missing_dependency"),
        ("Mixin apply failed example.mixin.json", "mod.mixin_failure"),
        ("Failed loading config file common.toml", "mod.config_failure"),
        ("Description: Ticking entity", "world.entity_failure"),
    ],
)
def test_manual_analysis_matches_structured_rules(tmp_path: Path, line: str, expected_code: str) -> None:
    game_path, _ = _game(tmp_path)
    source = tmp_path / "latest.log"
    source.write_text(line, encoding="utf-8")
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()

    assert result["versionId"] == "Test"
    assert result["detectedBy"] == ["manual"]
    assert result["reasons"][0]["code"] == expected_code
    assert result["reasons"][0]["evidence"]


def test_runtime_analysis_filters_old_logs_and_redacts_secrets(tmp_path: Path) -> None:
    game_path, version_path = _game(tmp_path)
    logs_path = version_path / "logs"
    logs_path.mkdir()
    (logs_path / "latest.log").write_text(
        "Authorization: Bearer very-secret\njava.lang.OutOfMemoryError: heap space",
        encoding="utf-8",
    )
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        result = analyzer.analyze_runtime(
            version_id="Test",
            game_path=game_path,
            game_directory=version_path,
            started_wall_time=0,
            output_lines=["--accessToken another-secret", "OutOfMemoryError"],
            exit_code=1,
            detected_by=["exit_code", "crash_log"],
        )
        output = analyzer.output(result["reportId"])
        exported = analyzer.export(result["reportId"])
    finally:
        analyzer.close()

    assert result["exitCode"] == 1
    assert result["hasOutput"] is True
    assert result["detectedBy"] == ["exit_code", "crash_log"]
    assert "another-secret" not in output["content"]
    assert "***" in output["content"]
    export_path = Path(exported["path"])
    assert export_path.is_file()
    with ZipFile(export_path) as archive:
        metadata = json.loads(archive.read("metadata/version.json"))
    assert metadata["accessToken"] == "***"


def test_archive_rejects_path_traversal(tmp_path: Path) -> None:
    game_path, _ = _game(tmp_path)
    archive_path = tmp_path / "unsafe.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.log", "crash")
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        with pytest.raises(GameServiceError, match="不安全路径") as raised:
            analyzer.analyze_file(archive_path, game_path, "Test")
    finally:
        analyzer.close()
    assert raised.value.error_code == "CRASH_ARCHIVE_UNSAFE"


def test_archive_rejects_nested_archives_and_binary_files(tmp_path: Path) -> None:
    game_path, _ = _game(tmp_path)
    archive_path = tmp_path / "nested.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("report.zip", b"not a nested archive")
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        with pytest.raises(GameServiceError) as raised:
            analyzer.analyze_file(archive_path, game_path, "Test")
    finally:
        analyzer.close()
    assert raised.value.error_code == "CRASH_ARCHIVE_UNSUPPORTED"


def test_manual_file_rejects_oversize_input(tmp_path: Path, monkeypatch) -> None:
    game_path, _ = _game(tmp_path)
    monkeypatch.setattr(crash_analyzer_module.CrashAnalysisPolicy, "max_source_bytes", 32)
    source = tmp_path / "oversize.log"
    source.write_text("x" * 64, encoding="utf-8")
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        with pytest.raises(GameServiceError) as raised:
            analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()
    assert raised.value.error_code == "CRASH_FILE_TOO_LARGE"


def test_stack_fallback_maps_package_to_fabric_mod_metadata(tmp_path: Path) -> None:
    game_path, version_path = _game(tmp_path)
    mods_path = version_path / "mods"
    mods_path.mkdir()
    with ZipFile(mods_path / "opaque-file-name.jar", "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "example", "name": "Example Display Name"}))
        archive.writestr("dev/example/crasher/Entrypoint.class", b"class bytes are not inspected")
    source = tmp_path / "latest.log"
    source.write_text("at dev.example.crasher.Entrypoint.initialize(Entrypoint.java:42)", encoding="utf-8")
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()

    assert result["reasons"] == [
        {
            "code": "stack.suspected_mod",
            "confidence": "possible",
            "evidence": ["dev.example.crasher"],
            "parameters": {"mods": ["Example Display Name"]},
            "mods": ["Example Display Name"],
            "packages": ["dev.example.crasher"],
        }
    ]


def test_rule_matches_gain_mod_attribution(tmp_path: Path) -> None:
    game_path, version_path = _game(tmp_path)
    mods_path = version_path / "mods"
    mods_path.mkdir()
    with ZipFile(mods_path / "example.jar", "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "example", "name": "Example Display Name"}))
    source = tmp_path / "latest.log"
    source.write_text("LoaderExceptionModCrash: Caught exception from Example (example)", encoding="utf-8")
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()

    reason = result["reasons"][0]
    assert reason["code"] == "mod.initialization_failure"
    assert reason["parameters"]["mod_id"] == "example"
    assert reason["mods"] == ["Example Display Name"]


def test_reasons_are_ordered_and_capped(tmp_path: Path) -> None:
    game_path, _ = _game(tmp_path)
    source = tmp_path / "latest.log"
    source.write_text(
        "\n".join(
            [
                "Could not save crash report",
                "Mixin apply failed example.mixin.json",
                "Failed loading config file common.toml",
                "Mod loading has failed",
                "OptiFine is not compatible with Forge",
                "java.lang.OutOfMemoryError: Java heap space",
            ]
        ),
        encoding="utf-8",
    )
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()

    codes = [reason["code"] for reason in result["reasons"]]
    assert len(codes) == 5
    assert codes[0] == "memory.out_of_memory"
    assert "game.crash_report" not in codes


def test_stack_companion_appended_when_rules_match(tmp_path: Path) -> None:
    game_path, version_path = _game(tmp_path)
    mods_path = version_path / "mods"
    mods_path.mkdir()
    with ZipFile(mods_path / "example.jar", "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "example", "name": "Example Display Name"}))
        archive.writestr("dev/example/crasher/Entrypoint.class", b"class bytes are not inspected")
    source = tmp_path / "latest.log"
    source.write_text(
        "java.lang.OutOfMemoryError: Java heap space\nat dev.example.crasher.Entrypoint.initialize(Entrypoint.java:42)",
        encoding="utf-8",
    )
    analyzer = CrashAnalyzer(tmp_path / "data")
    try:
        result = analyzer.analyze_file(source, game_path, "Test")
    finally:
        analyzer.close()

    assert [reason["code"] for reason in result["reasons"]] == [
        "memory.out_of_memory",
        "stack.suspected_mod",
    ]
    assert result["reasons"][1]["mods"] == ["Example Display Name"]


def test_report_is_session_only_and_removed_on_close(tmp_path: Path) -> None:
    game_path, _ = _game(tmp_path)
    source = tmp_path / "crash.txt"
    source.write_text("This crash report has been saved to: crash-reports/crash.txt", encoding="utf-8")
    analyzer = CrashAnalyzer(tmp_path / "data")
    result = analyzer.analyze_file(source, game_path, "Test")
    session_path = analyzer.session_path
    assert (
        json.loads((session_path / result["reportId"] / "analysis.json").read_text(encoding="utf-8"))["reportId"]
        == result["reportId"]
    )

    analyzer.close()

    assert not session_path.exists()
    with pytest.raises(GameServiceError) as raised:
        analyzer.output(result["reportId"])
    assert raised.value.error_code == "CRASH_REPORT_NOT_FOUND"
