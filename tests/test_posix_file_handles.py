from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest

from ECL.utils.file_models import FileBoundaryError
from ECL.utils.posix_file_handles import FileDirectory

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="需要真实 POSIX 文件系统")


@pytest.fixture
def directory(tmp_path):
    opened = FileDirectory.open(tmp_path)
    yield opened
    opened.close()


def test_created_identity_survives_writes_and_publishes_atomically(directory, tmp_path):
    stream = directory.file(".stage", create=True)
    identity = stream.identity
    try:
        stream.write(0, b"content")
        stream.flush()
        assert stream.information().identity == identity
        assert stream.read(0, 100) == b"content"
        stream.publish("final")
        assert stream.identity == identity
        assert directory.entry_identity("final", monotonic() + 1) == identity
        with pytest.raises(FileBoundaryError):
            stream.discard()
    finally:
        stream.close()
    assert (tmp_path / "final").read_bytes() == b"content"


def test_create_only_conflict_preserves_both_objects(directory, tmp_path):
    (tmp_path / "final").write_bytes(b"original")
    stream = directory.file(".stage", create=True)
    stream.write(0, b"new")
    try:
        with pytest.raises(FileBoundaryError) as error:
            stream.publish("final")
        assert error.value.error_code == "PLUGIN_FILE_EXISTS"
        assert not stream.is_published
    finally:
        stream.discard()
    assert (tmp_path / "final").read_bytes() == b"original"
    assert set(directory.names(monotonic() + 1)) == {"final"}


def test_replacement_preserves_complete_content_and_removes_staging(directory, tmp_path):
    (tmp_path / "final").write_bytes(b"original")
    stream = directory.file(".stage", create=True)
    stream.write(0, b"replacement")
    stream.flush()
    try:
        stream.replace("final")
    finally:
        stream.close()
    assert (tmp_path / "final").read_bytes() == b"replacement"
    assert not (tmp_path / ".stage").exists()


def test_symlink_hardlink_and_fifo_are_rejected_without_changing_data(directory, tmp_path):
    source = tmp_path / "original"
    source.write_bytes(b"preserve")
    (tmp_path / "link").symlink_to(source)
    os.link(source, tmp_path / "hardlink")
    os.mkfifo(tmp_path / "fifo")
    for name in ("link", "hardlink", "fifo"):
        with pytest.raises(FileBoundaryError):
            directory.file(name)
    assert source.read_bytes() == b"preserve"
    assert (tmp_path / "hardlink").read_bytes() == b"preserve"


def test_directory_rebinding_rejects_old_view_without_touching_replacement(tmp_path):
    target = tmp_path / "root"
    target.mkdir()
    directory = FileDirectory.open(target)
    stream = directory.file("file", create=True)
    stream.write(0, b"original")
    target.rename(tmp_path / "displaced")
    target.mkdir()
    (target / "file").write_bytes(b"replacement")
    try:
        with pytest.raises(FileBoundaryError) as error:
            stream.publish("new")
        assert error.value.error_code == "PLUGIN_FILE_CHANGED"
    finally:
        stream.close()
        directory.close()
    assert (target / "file").read_bytes() == b"replacement"
    assert (tmp_path / "displaced/file").read_bytes() == b"original"


def test_delete_mark_can_be_revoked_and_close_is_idempotent(directory, tmp_path):
    (tmp_path / "target").write_bytes(b"preserve")
    streams = []
    stream = directory.delete_target("target", streams.append)
    stream.mark_deletion(True)
    assert stream.deletion_pending()
    assert (tmp_path / "target").exists()
    stream.mark_deletion(False)
    stream.close()
    stream.close()
    assert (tmp_path / "target").read_bytes() == b"preserve"
    stream = directory.delete_target("target", streams.append)
    stream.mark_deletion(True)
    stream.close()
    assert stream.is_closed and not stream.has_pending_cleanup
    assert directory.names(monotonic() + 1) == ()


def test_substituted_delete_target_is_restored_instead_of_deleted(directory, tmp_path, monkeypatch):
    (tmp_path / "target").write_bytes(b"original")
    streams = []
    stream = directory.delete_target("target", streams.append)
    original = directory._native.rename

    def substitute(source, name, target, destination, *, replace):
        if name == "target" and destination == "object":
            (tmp_path / "target").rename(tmp_path / "displaced")
            (tmp_path / "target").write_bytes(b"other-data")
        return original(source, name, target, destination, replace=replace)

    monkeypatch.setattr(directory._native, "rename", substitute)
    stream.mark_deletion(True)
    try:
        with pytest.raises(FileBoundaryError) as error:
            stream.close()
        assert error.value.error_code == "PLUGIN_FILE_CHANGED"
        stream.mark_deletion(False)
    finally:
        stream.close()
    assert (tmp_path / "target").read_bytes() == b"other-data"
    assert (tmp_path / "displaced").read_bytes() == b"original"
    assert set(directory.names(monotonic() + 1)) == {"target", "displaced"}


def test_interrupted_delete_restores_original_without_restarting_task(tmp_path):
    (tmp_path / "target").write_bytes(b"original")
    source = """import os, sys
from pathlib import Path
from ECL.utils.posix_file_handles import FileDirectory
from ECL.utils.posix_file_transactions import FileDeletion
root = FileDirectory.open(Path(sys.argv[1]))
held = root.file('target')
txn = FileDeletion.start(root, 'target', held.identity)
txn._prepare()
root._native.rename(root._handle, 'target', txn._folder_fd, 'object', replace=False)
os.fsync(txn._folder_fd)
os.fsync(root._handle)
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        timeout=15,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "target").exists()
    recovered = FileDirectory.open(tmp_path)
    recovered.close()
    assert (tmp_path / "target").read_bytes() == b"original"
    assert {path.name for path in tmp_path.iterdir()} == {"target"}


def test_corrupted_recovery_record_is_preserved(tmp_path):
    marker = tmp_path / (".ecl-delete-" + "a" * 32 + ".json")
    marker.write_text("corrupted", encoding="utf-8")
    marker.chmod(0o600)
    with pytest.raises(FileBoundaryError) as error:
        FileDirectory.open(tmp_path)
    assert error.value.error_code == "PLUGIN_STORAGE_FAILED"
    assert marker.read_text() == "corrupted"


def test_cleanup_failure_after_unlink_preserves_committed_effect_and_retry(directory, tmp_path, monkeypatch):
    from ECL.utils.posix_file_transactions import FileDeletion

    (tmp_path / "target").write_bytes(b"original")
    streams = []
    stream = directory.delete_target("target", streams.append)
    finish = FileDeletion.finish
    with monkeypatch.context() as patch:
        patch.setattr(
            FileDeletion,
            "finish",
            lambda self: (_ for _ in ()).throw(FileBoundaryError("清理失败", "PLUGIN_FILE_BUSY")),
        )
        stream.mark_deletion(True)
        with pytest.raises(FileBoundaryError):
            stream.close()
        assert stream.is_closed and stream.has_pending_cleanup
        assert not (tmp_path / "target").exists()
        with pytest.raises(FileBoundaryError):
            stream.mark_deletion(False)
    assert FileDeletion.finish is finish
    stream.close()
    assert not stream.has_pending_cleanup
    assert directory.names(monotonic() + 1) == ()
