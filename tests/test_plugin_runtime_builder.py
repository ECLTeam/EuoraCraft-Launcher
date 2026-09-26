from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimeStore, load_plugin_runtime_asset


def test_runtime_builder_emits_usable_asset_and_embedded_manifest(tmp_path: Path, monkeypatch) -> None:
    """
    发行脚本把 Python/uv 单独打包，启动器清单固定该资产摘要。
    """
    script_path = Path(__file__).resolve().parent.parent / "packaging" / "build-plugin-runtime.py"
    spec = importlib.util.spec_from_file_location("ecl_plugin_runtime_builder", script_path)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    python_root = tmp_path / "portable-python"
    python_file = python_root / "bin" / "python"
    python_file.parent.mkdir(parents=True)
    python_file.write_bytes(b"test-python")
    include_file = python_root / "include" / "Python.h"
    include_file.parent.mkdir()
    include_file.write_bytes(b"not-needed")
    base_package = python_root / "Lib" / "site-packages" / "pip" / "__init__.py"
    base_package.parent.mkdir(parents=True)
    base_package.write_bytes(b"not-needed")
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
        assert "python/include/Python.h" not in archive.namelist()
        assert "python/Lib/site-packages/pip/__init__.py" not in archive.namelist()

    runtime_paths = PluginRuntimeStore(tmp_path / "data").ensure(
        PluginRuntimeAsset(**manifest), offline_pack=archive_path
    )
    assert runtime_paths.python_path.read_bytes() == b"test-python"
    assert runtime_paths.uv_path.read_bytes() == b"test-uv"
