from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from ECL.plugins.runtime_assets import (
    PluginRuntimeAsset,
    PluginRuntimeError,
    PluginRuntimeStore,
    load_plugin_runtime_asset,
)


def _load_builder():
    script_path = Path(__file__).resolve().parent.parent / "packaging" / "build-plugin-runtime.py"
    spec = importlib.util.spec_from_file_location("ecl_plugin_runtime_builder", script_path)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder


def _make_portable_python(python_root: Path) -> None:
    python_file = python_root / "bin" / "python"
    python_file.parent.mkdir(parents=True)
    python_file.write_bytes(b"test-python")
    include_file = python_root / "include" / "Python.h"
    include_file.parent.mkdir()
    include_file.write_bytes(b"not-needed")
    base_package = python_root / "Lib" / "site-packages" / "pip" / "__init__.py"
    base_package.parent.mkdir(parents=True)
    base_package.write_bytes(b"not-needed")


def test_runtime_builder_emits_usable_asset_and_embedded_manifest(tmp_path: Path, monkeypatch) -> None:
    """
    发行脚本把 Python/uv 单独打包，启动器清单固定该资产摘要。
    """
    builder = _load_builder()

    python_root = tmp_path / "portable-python"
    _make_portable_python(python_root)
    uv_file = tmp_path / "uv"
    uv_file.write_bytes(b"test-uv")
    monkeypatch.setattr(builder, "_uv_executable", lambda: uv_file)
    monkeypatch.setattr(builder, "_check_uv_version", lambda _uv_path: None)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)

    output_path = tmp_path / "assets"
    manifest_path = tmp_path / "resources" / "plugin_runtime_manifest.json"
    assert (
        builder.main(
            [
                "--output-dir",
                str(output_path),
                "--manifest",
                str(manifest_path),
                "--python-root",
                str(python_root),
                "--python-executable",
                "bin/python",
            ]
        )
        == 0
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["download_url"] is None
    assert load_plugin_runtime_asset(manifest_path) == PluginRuntimeAsset(**manifest)
    archive_path = next(output_path.glob("plugin-runtime-*.zip"))
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read(manifest["python_relpath"]) == b"test-python"
        assert archive.read(manifest["uv_relpath"]) == b"test-uv"
        assert b"def main(" in archive.read(manifest["worker_relpath"])
        assert b"class Plugin:" in archive.read("ecl_plugin_sdk.py")
        assert "python/include/Python.h" not in archive.namelist()
        assert "python/Lib/site-packages/pip/__init__.py" not in archive.namelist()

    runtime_paths = PluginRuntimeStore(tmp_path / "data").ensure(
        PluginRuntimeAsset(**manifest), offline_pack=archive_path
    )
    assert runtime_paths.python_path.read_bytes() == b"test-python"
    assert runtime_paths.uv_path.read_bytes() == b"test-uv"
    assert runtime_paths.worker_path.is_file()
    assert (runtime_paths.root_path / "ecl_plugin_sdk.py").is_file()


def test_runtime_builder_dedupes_case_insensitive_paths(tmp_path: Path, monkeypatch) -> None:
    """
    仅大小写不同的发行树路径（terminfo 的 Eterm/eterm）在构建期去重，资产可过校验。

    大小写敏感的文件系统（Linux CI）上夹具产生两个文件，修复前资产校验会拒绝；
    大小写不敏感的文件系统上夹具自然合并为单文件，测试退化为常规构建路径。
    """
    builder = _load_builder()

    python_root = tmp_path / "portable-python"
    _make_portable_python(python_root)
    upper_file = python_root / "share" / "terminfo" / "E" / "Eterm"
    upper_file.parent.mkdir(parents=True, exist_ok=True)
    upper_file.write_bytes(b"upper-terminfo")
    lower_file = python_root / "share" / "terminfo" / "e" / "eterm"
    lower_file.parent.mkdir(parents=True, exist_ok=True)
    lower_file.write_bytes(b"lower-terminfo")
    uv_file = tmp_path / "uv"
    uv_file.write_bytes(b"test-uv")
    monkeypatch.setattr(builder, "_uv_executable", lambda: uv_file)
    monkeypatch.setattr(builder, "_check_uv_version", lambda _uv_path: None)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)

    output_path = tmp_path / "assets"
    manifest_path = tmp_path / "resources" / "plugin_runtime_manifest.json"
    assert (
        builder.main(
            [
                "--output-dir",
                str(output_path),
                "--manifest",
                str(manifest_path),
                "--python-root",
                str(python_root),
                "--python-executable",
                "bin/python",
            ]
        )
        == 0
    )

    archive_path = next(output_path.glob("plugin-runtime-*.zip"))
    with zipfile.ZipFile(archive_path) as archive:
        folded_names = {name.casefold() for name in archive.namelist()}
        assert len(folded_names) == len(archive.namelist())
        kept = [name for name in archive.namelist() if name.casefold().endswith("terminfo/e/eterm")]
        assert len(kept) == 1
        assert archive.read(kept[0]) in (b"upper-terminfo", b"lower-terminfo")

    asset = load_plugin_runtime_asset(manifest_path)
    assert PluginRuntimeStore(tmp_path / "data").ensure(asset, offline_pack=archive_path) is not None


def test_runtime_asset_still_rejects_duplicate_paths(tmp_path: Path, monkeypatch) -> None:
    """
    构建期去重不放松校验：手工注入重复路径的资产仍被拒绝。
    """
    builder = _load_builder()

    python_root = tmp_path / "portable-python"
    _make_portable_python(python_root)
    uv_file = tmp_path / "uv"
    uv_file.write_bytes(b"test-uv")
    monkeypatch.setattr(builder, "_uv_executable", lambda: uv_file)
    monkeypatch.setattr(builder, "_check_uv_version", lambda _uv_path: None)
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)

    output_path = tmp_path / "assets"
    manifest_path = tmp_path / "resources" / "plugin_runtime_manifest.json"
    assert (
        builder.main(
            [
                "--output-dir",
                str(output_path),
                "--manifest",
                str(manifest_path),
                "--python-root",
                str(python_root),
                "--python-executable",
                "bin/python",
            ]
        )
        == 0
    )

    archive_path = next(output_path.glob("plugin-runtime-*.zip"))
    with zipfile.ZipFile(archive_path) as archive:
        original = [(entry.filename, archive.read(entry)) for entry in archive.infolist()]
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, content in original:
            archive.writestr(name, content)
        archive.writestr("python/BIN/PYTHON", b"payload")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest["archive_size_bytes"] = archive_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    asset = load_plugin_runtime_asset(manifest_path)
    with pytest.raises(PluginRuntimeError, match="重复路径"):
        PluginRuntimeStore(tmp_path / "data").ensure(asset, offline_pack=archive_path)
