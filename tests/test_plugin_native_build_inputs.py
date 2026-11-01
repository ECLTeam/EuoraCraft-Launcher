from __future__ import annotations

import hashlib
import importlib.util
import io
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


@pytest.fixture
def fixed_runtime():
    script = Path(__file__).resolve().parents[1] / "native/pytauri-runtime/prepare-webview2.py"
    spec = importlib.util.spec_from_file_location("native_webview_fixture", script)
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


@pytest.mark.parametrize("version", [[], "156.*", "155.0.4283.62,156.0.4314.58", 155, ""])
def test_native_build_rejects_wildcards_and_invalid_runtime_versions(builder, tmp_path, monkeypatch, version):
    script = tmp_path / "native/runtime/build-wheel.py"
    script.parent.mkdir(parents=True)
    (script.parent / "source.json").write_text(json.dumps({"webview2_version": version}), encoding="utf-8")
    monkeypatch.setattr(builder, "__file__", str(script))
    monkeypatch.setattr(sys, "argv", [str(script), "--output-directory", str(tmp_path / "output")])
    with pytest.raises(ValueError, match="验收版本无效"):
        builder.main()
    assert not (tmp_path / "output").exists()


def test_tampered_fixed_runtime_is_never_expanded(fixed_runtime, tmp_path, monkeypatch):
    script = tmp_path / "tool/prepare-webview2.py"
    script.parent.mkdir()
    manifest = {
        "webview2_fixed_runtime": {
            "url": "https://msedge.sf.dl.delivery.mp.microsoft.com/runtime.cab",
            "sha256": "0" * 64,
        }
    }
    (script.parent / "source.json").write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "output"
    monkeypatch.setattr(fixed_runtime, "__file__", str(script))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", [str(script), "--output-directory", str(output)])
    monkeypatch.setattr(fixed_runtime, "urlopen", lambda *args, **kwargs: io.BytesIO(b"tampered"))

    def never_execute(*args, **kwargs):
        raise AssertionError("Unverified archive must never be expanded")

    monkeypatch.setattr(fixed_runtime.subprocess, "run", never_execute)
    with pytest.raises(ValueError, match="摘要不一致"):
        fixed_runtime.main()
    assert not (output / "runtime.cab").exists()
    assert (output / "runtime.partial").read_bytes() == b"tampered"


def test_fixed_runtime_is_published_only_after_digest_validation(fixed_runtime, tmp_path, monkeypatch):
    content = b"authenticated fixture"
    target = tmp_path / "runtime.cab"
    monkeypatch.setattr(fixed_runtime, "urlopen", lambda *args, **kwargs: io.BytesIO(content))
    fixed_runtime._download("https://fixture.invalid", target, hashlib.sha256(content).hexdigest())
    assert target.read_bytes() == content
    assert not target.with_suffix(".partial").exists()
