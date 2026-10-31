from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ECL.services.game.instance_profile_transactions import InstanceProfileTarget, NativeProfileTarget
from ECL.utils.instance_reading import InstanceReadError, directory_identity
from ECL.utils.windows_file_handles import FileBoundaryError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="真实Windows NTFS共享实例提交")


def target(root: Path) -> InstanceProfileTarget:
    instance = root / "versions/人工实例"
    instance.mkdir(parents=True)
    return InstanceProfileTarget(root, "人工实例", directory_identity(root), directory_identity(instance))


def test_native_directory_identity_is_derived_from_same_handle_and_pins_every_component(tmp_path):
    approved = target(tmp_path)
    native = NativeProfileTarget(approved)
    accepted = []
    try:
        native.open(initialize=True, admit=lambda: accepted.append(True))
        assert accepted == [True]
        assert len(native.directories) == 4
        for path in (tmp_path / "versions", tmp_path / "versions/人工实例", tmp_path / "versions/人工实例/.ecl"):
            with pytest.raises(OSError):
                path.rename(path.with_name(path.name + "-moved"))
        assert NativeProfileTarget.identity(native.directories[0]) == approved.root_identity
        assert NativeProfileTarget.identity(native.directories[2]) == approved.instance_identity
    finally:
        assert native.close()


def test_same_name_new_instance_never_inherits_old_approval(tmp_path):
    approved = target(tmp_path)
    instance = tmp_path / "versions/人工实例"
    instance.rename(instance.with_name("original"))
    instance.mkdir()
    native = NativeProfileTarget(approved)
    try:
        with pytest.raises(InstanceReadError):
            native.open(initialize=True, admit=lambda: None)
        assert not (instance / ".ecl").exists()
    finally:
        assert native.close()


def test_ecl_junction_cannot_publish_outside_selected_instance(tmp_path):
    approved = target(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / "versions/人工实例/.ecl"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    native = NativeProfileTarget(approved)
    try:
        with pytest.raises(FileBoundaryError):
            native.open(initialize=True, admit=lambda: None)
        assert list(outside.iterdir()) == []
    finally:
        assert native.close()
