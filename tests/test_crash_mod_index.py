# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：针对 crash 三源 Mod 索引的自动化测试。
#
# 公开接口：
#   - test_jar_sources_index_packages_and_ids(tmp_path) -> None
#   - test_crash_report_tables_are_ingested(tmp_path) -> None
#   - test_fabric_mod_block_is_ingested(tmp_path) -> None
#   - test_loader_mod_files_are_ingested(tmp_path) -> None
#   - test_fingerprint_cache_reuses_scan(tmp_path, monkeypatch) -> None
#   - test_resolve_frames_returns_unmatched_packages(tmp_path) -> None
# ============================================================

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from ECL.services.game.crash.mod_index import CrashModIndex


def _make_example_jar(mods_dir: Path) -> None:
    mods_dir.mkdir(parents=True)
    with ZipFile(mods_dir / "example.jar", "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "example", "name": "Example Display Name"}))
        archive.writestr("dev/example/core/Entrypoint.class", b"class bytes are not inspected")


def test_jar_sources_index_packages_and_ids(tmp_path: Path) -> None:
    mods_dir = tmp_path / "mods"
    _make_example_jar(mods_dir)
    index = CrashModIndex()

    index.refresh([mods_dir], [])

    assert index.resolve(["dev.example.core.Other"]) == ("Example Display Name",)
    assert index.resolve(["example"]) == ("Example Display Name",)


def test_crash_report_tables_are_ingested(tmp_path: Path) -> None:
    mods_dir = tmp_path / "mods"
    _make_example_jar(mods_dir)
    forge_table = "\n".join(
        [
            "Mod List:",
            "\t\texample.jar          |Example Display Name|example        |1.0.0|1.20.1|",
            "\t\tanother.jar          |Another Mod         |another        |2.0.0|1.20.1|",
        ]
    )
    fml_table = "\n".join(
        [
            "FML: MCP v9.05 FML v7.10.99.99 4 mods loaded",
            "\tmcp{9.05} [Minecraft Coder Pack] (minecraft.jar)",
            "\tlegacymod{1.2} [Legacy Mod] (legacy.jar)",
        ]
    )
    index = CrashModIndex()

    index.refresh([mods_dir], [forge_table, fml_table])

    assert index.resolve(["example"]) == ("Example Display Name",)
    assert index.resolve(["another"]) == ("Another Mod",)
    assert index.resolve(["legacymod"]) == ("Legacy Mod",)


def test_fabric_mod_block_is_ingested(tmp_path: Path) -> None:
    mods_dir = tmp_path / "mods"
    _make_example_jar(mods_dir)
    text = "\n".join(
        [
            "[main/INFO]: Loading 3 mods:",
            "\t- examplemod 1.0.0",
            "\t- fabric-api 0.92.0+1.20.1",
            "[main/INFO]: Reloading...",
        ]
    )
    index = CrashModIndex()

    index.refresh([mods_dir], [text])

    assert index.resolve(["examplemod"]) == ("examplemod",)
    assert index.resolve(["fabric-api"]) == ("fabric-api",)


def test_loader_mod_files_are_ingested(tmp_path: Path) -> None:
    mods_dir = tmp_path / "mods"
    _make_example_jar(mods_dir)
    text = "[DEBUG] [ModDiscoverer/SCAN]: Found mod file discovered.jar\n[DEBUG]: Loading mod file loaded.jar"
    index = CrashModIndex()

    index.refresh([mods_dir], [text])

    assert index.resolve(["discovered"]) == ("discovered",)
    assert index.resolve(["loaded"]) == ("loaded",)


def test_fingerprint_cache_reuses_scan(tmp_path: Path, monkeypatch) -> None:
    mods_dir = tmp_path / "mods"
    _make_example_jar(mods_dir)
    index = CrashModIndex()
    calls: list[Path] = []
    original_scan = CrashModIndex._scan_dir

    def counting_scan(self: CrashModIndex, mods_dir: Path) -> None:
        calls.append(mods_dir)
        original_scan(self, mods_dir)

    monkeypatch.setattr(CrashModIndex, "_scan_dir", counting_scan)

    index.refresh([mods_dir], [])
    index.refresh([mods_dir], [])
    assert len(calls) == 1

    (mods_dir / "extra.jar").write_bytes(b"not a zip")
    index.refresh([mods_dir], [])
    assert len(calls) == 2


def test_resolve_frames_returns_unmatched_packages(tmp_path: Path) -> None:
    mods_dir = tmp_path / "mods"
    _make_example_jar(mods_dir)
    index = CrashModIndex()
    index.refresh([mods_dir], [])

    attribution = index.resolve_frames(
        ["dev/example/core/Entrypoint", "net.unmatched.pkg.Target", "com.mojangIgnored.X"]
    )

    assert attribution.mods == ("Example Display Name",)
    assert attribution.packages == ("net.unmatched.pkg", "com.mojangIgnored.X")
