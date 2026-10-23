from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

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


def test_original_move_publish_and_conflict_restore_are_never_overwriting(tmp_path):
    native = NativeProfileTarget(target(tmp_path))
    streams = []
    try:
        native.open(initialize=True, admit=lambda: None)
        directory = native.profile_directory
        initial = directory.file(".initial", create=True, on_create=streams.append)
        initial.write(0, b"original")
        initial.flush()
        initial.publish("instance.json")
        initial.close()
        original = directory.delete_target("instance.json", streams.append)
        original.move(".backup")
        foreign = directory.file("instance.json", create=True, on_create=streams.append)
        foreign.write(0, b"foreign")
        foreign.close()
        with pytest.raises(FileBoundaryError):
            original.move("instance.json")
        assert original.read(0, 8) == b"original"
        original.move("recovered-profile-test.json")
        original.close()
        assert (tmp_path / "versions/人工实例/.ecl/instance.json").read_bytes() == b"foreign"
        assert directory.entry_identity(".backup", monotonic() + 1) is None
    finally:
        for stream in reversed(streams):
            stream.close()
        assert native.close()


@pytest.mark.parametrize(
    "has_original,point",
    [
        *(
            (True, point)
            for point in (
                "before_prepared",
                "prepared",
                "after_move",
                "old_saved",
                "after_publish",
                "published",
                "after_backup_deleted",
                "conflict",
            )
        ),
        *((False, point) for point in ("before_prepared", "prepared", "after_publish", "published", "conflict")),
    ],
)
def test_actual_process_exit_recovers_shared_profile_and_never_replays_patch(tmp_path, has_original, point):
    from ECL.foundation import EventBus
    from ECL.services.game.instance_profiles import InstanceProfileStore
    from ECL.services.game.version_stats import VersionStatsStore
    from ECL.services.operations import OperationManager

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.instance_profile_crash_helper",
            str(tmp_path),
            point,
            "existing" if has_original else "first",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        end = monotonic() + 15
        while not (tmp_path / "ready").exists():
            if process.poll() is not None:
                pytest.fail(process.communicate(timeout=1)[1].decode("utf-8", errors="replace"))
            assert monotonic() < end
            sleep(0.025)
    finally:
        process.terminate()
        process.communicate(timeout=5)
    notified = []
    store = InstanceProfileStore(tmp_path / "data", VersionStatsStore(), on_changed=notified.append)
    manager = OperationManager(tmp_path / "data", EventBus())
    path = tmp_path / "game/versions/one/.ecl/instance.json"
    try:
        page = store.transactions.page()
        if point == "conflict":
            assert json.loads(path.read_bytes())["alias"] == "External"
            if has_original:
                row = page["items"][0]
                receipt = store.transactions.resolve(
                    row["recovery_ref"], row["revision"], row["etag"], "save_as", "original", manager
                )
                manager._operations[receipt["operationId"]].future.result(5)
                assert manager.get(receipt["operationId"])["status"] == "completed"
                assert json.loads((path.parent / "recovered-profile-original.json").read_bytes())["alias"] == "Original"
            else:
                assert page["items"] == []
        else:
            assert page["items"] == []
            if point in {"after_publish", "published", "after_backup_deleted"}:
                assert json.loads(path.read_bytes())["alias"] == "Published"
            elif has_original:
                assert json.loads(path.read_bytes())["alias"] == "Original"
            else:
                assert not path.exists()
        if point != "before_prepared":
            assert not list(path.parent.glob(".profile-new-*"))
            assert notified == [tmp_path / "game"]
        assert not store.transactions.has_pending()
    finally:
        manager.close()
        assert store.transactions.close()
