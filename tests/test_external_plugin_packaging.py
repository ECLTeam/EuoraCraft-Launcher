from __future__ import annotations

from pathlib import Path

import pytest

import package_external_plugins


def test_download_uses_only_the_requested_platform_lock(tmp_path: Path, monkeypatch) -> None:
    locks_path = tmp_path / "locks"
    locks_path.mkdir()
    (locks_path / "linux-x86_64-cp312.txt").write_text("", encoding="utf-8")
    (locks_path / "windows-x86_64-cp311.txt").write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(package_external_plugins.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    package_external_plugins._download_wheels(tmp_path, "linux-x86_64-cp312")
    assert len(calls) == 1
    command, options = calls[0]
    assert "--require-hashes" in command
    assert "--no-deps" in command
    assert "--platform=manylinux_2_28_x86_64" in command
    assert command[-1] == str(locks_path / "linux-x86_64-cp312.txt")
    assert options["check"] is True


def test_target_without_download_fails_before_preparing_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        package_external_plugins.sys, "argv", ["package_external_plugins.py", "--download-target", "linux-x86_64-cp312"]
    )
    with pytest.raises(SystemExit) as error:
        package_external_plugins.main()
    assert error.value.code == 2
    assert not (tmp_path / "output").exists()
