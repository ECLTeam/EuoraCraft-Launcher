from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def builder():
    script_path = Path(__file__).resolve().parents[1] / "native/pytauri-runtime/build-wheel.py"
    spec = importlib.util.spec_from_file_location("native_runtime_builder", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_manifest_authenticates_every_policy_and_patch(builder):
    root = Path(builder.__file__).parent
    manifest = json.loads((root / "source.json").read_text(encoding="utf-8"))
    assert set(manifest["policies"]) == {
        "plugin_isolation.rs",
        "plugin_isolation_unix.rs",
        "plugin_isolation_gtk.rs",
        "plugin_isolation_macos.rs",
    }
    for name, expected in manifest["policies"].items():
        builder._assert_digest(root / name, expected)
    builder._assert_digest(root / "runtime.patch", manifest["patch_sha256"])


def test_native_build_rejects_tampered_input(builder, tmp_path):
    file_path = tmp_path / "policy.rs"
    file_path.write_bytes(b"reviewed")
    expected = hashlib.sha256(file_path.read_bytes()).hexdigest()
    file_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="摘要不一致"):
        builder._assert_digest(file_path, expected)


def test_native_build_preserves_existing_source_before_running_tools(builder, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    marker = source / "uncommitted"
    marker.write_bytes(b"preserve")
    monkeypatch.setattr(sys, "argv", [builder.__file__, "--output-directory", str(tmp_path)])
    with pytest.raises(ValueError, match="源码目录已存在"):
        builder.main()
    assert marker.read_bytes() == b"preserve"
    assert not (tmp_path / "wheels").exists()


def test_native_builder_reports_chinese_under_western_windows_console(builder):
    result = subprocess.run(
        [sys.executable, builder.__file__, "--help"],
        env=dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0"),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "构建固定的插件原生窗口运行时" in result.stdout.decode("utf-8")


@pytest.mark.parametrize("versions", [[], ["155.*"], ["155.0.4283.62,156.0.4314.58"], [155], "155.0.4283.62"])
def test_native_build_rejects_wildcards_and_invalid_runtime_allowlists(builder, tmp_path, monkeypatch, versions):
    script = tmp_path / "native/runtime/build-wheel.py"
    script.parent.mkdir(parents=True)
    (script.parent / "source.json").write_text(json.dumps({"webview2_versions": versions}), encoding="utf-8")
    monkeypatch.setattr(builder, "__file__", str(script))
    monkeypatch.setattr(sys, "argv", [str(script), "--output-directory", str(tmp_path / "output")])
    with pytest.raises(ValueError, match="验收版本清单无效"):
        builder.main()
    assert not (tmp_path / "output").exists()
