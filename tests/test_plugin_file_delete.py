from __future__ import annotations

import ctypes
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from pydantic import ValidationError

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.runtime.models import FileDeleteRequest
from ECL.utils.windows_file_handles import FileBoundaryError, FileHandle
from tests.test_plugin_host_files import begin, call, chunk, commit, deny, files, finish, root

__all__ = ["files"]


def published(broker, manager, content=b"hello", name="target.bin"):
    target, write_ref = begin(broker, name, content)
    if content:
        chunk(broker, write_ref, content)
    view = finish(broker, manager, commit(broker, write_ref, content))
    assert view["state"] == "completed"
    return target, view["result"]["etag"]


def delete(broker, target, etag, key=None):
    return call(broker, "files.delete", {"target": target, "expected_etag": etag}, key)


@pytest.mark.parametrize(
    "change",
    [
        {"expected_etag": None},
        {"expected_etag": "A" * 64},
        {"expected_etag": "x"},
        {"force": True},
        {"recursive": False},
        {"target": {"root_ref": "a" * 32, "relative_path": ""}},
    ],
)
def test_delete_strict_request(change):
    request = {"target": {"root_ref": "a" * 32, "relative_path": "target"}, "expected_etag": "0" * 64}
    with pytest.raises(ValidationError):
        FileDeleteRequest.model_validate(request | change)


@pytest.mark.parametrize("content", [b"", b"hello"])
def test_delete_real_version_and_mutation_receipt(files, tmp_path, content):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager, content)
    receipt = delete(broker, target, etag, "delete-once")
    view = finish(broker, manager, receipt)
    assert view["state"] == "completed"
    assert view["effect"] == "committed"
    assert view["result"] == {"deleted": True}
    assert view["cleanup_pending"] is False
    assert delete(broker, target, etag, "delete-once") == receipt
    assert not storage.deletions(broker.session.run_id)
    assert not list(tmp_path.glob("plugin_data/*/files/target.bin"))
    missing = finish(broker, manager, delete(broker, target, etag))
    assert missing["error_code"] == "PLUGIN_RESOURCE_NOT_FOUND"
    assert missing["effect"] == "none"


def test_changed_version_and_same_bytes_replacement_are_preserved(files, tmp_path):
    broker, manager, _, _, _ = files
    target, etag = published(broker, manager)
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    target_path.unlink()
    target_path.write_bytes(b"hello")
    view = finish(broker, manager, delete(broker, target, etag))
    assert view["error_code"] == "PLUGIN_FILE_CHANGED"
    assert view["effect"] == "none"
    assert target_path.read_bytes() == b"hello"


def test_delete_permission_is_independent_and_result_does_not_need_read(files):
    broker, manager, _, store, _ = files
    target, etag = published(broker, manager)
    deny(broker, store, "filesystem.read")
    target["root_ref"] = root(broker, "filesystem.delete")
    view = finish(broker, manager, delete(broker, target, etag))
    assert view["result"] == {"deleted": True}
    deny(broker, store, "filesystem.delete")
    assert call(broker, "operations.get", {"operation_ref": view["operation_ref"]})["result"] is None
    with pytest.raises(ProtocolError) as error:
        delete(broker, target, etag)
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"


def test_queued_delete_reserves_name_but_does_not_open_target_and_cancels(files, tmp_path):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 5)
    try:
        receipt = delete(broker, target, etag)
        deletion = storage.deletions(broker.session.run_id)[0]
        assert deletion.stream is None
        assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"
        with pytest.raises(ProtocolError) as error:
            delete(broker, target, etag)
        assert error.value.error_code == "PLUGIN_FILE_BUSY"
        call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
    finally:
        gate.set()
        blocker.result(5)
    view = finish(broker, manager, receipt)
    assert view["state"] == "cancelled"
    assert view["effect"] == "none"


@pytest.mark.parametrize("fault", ["mark", "query", "close", "rollback"])
def test_native_failure_rolls_back_or_retains_unknown_until_real_cleanup(files, tmp_path, monkeypatch, fault):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    mark, close, query = FileHandle.mark_deletion, FileHandle.close, FileHandle.deletion_pending
    failed = False

    def fail_mark(stream, pending):
        nonlocal failed
        if fault == "mark" and pending and not failed:
            failed = True
            raise FileBoundaryError("测试标记失败", "PLUGIN_FILE_IO_FAILED")
        if fault == "rollback" and not pending:
            raise FileBoundaryError("测试撤回失败", "PLUGIN_FILE_IO_FAILED")
        return mark(stream, pending)

    def fail_close(stream):
        nonlocal failed
        if stream.deletion_pending() and (not failed or fault == "rollback"):
            failed = True
            raise FileBoundaryError("测试关闭失败", "PLUGIN_FILE_BUSY")
        return close(stream)

    def fail_query(stream):
        nonlocal failed
        if not failed:
            failed = True
            raise FileBoundaryError("测试查询失败", "PLUGIN_FILE_IO_FAILED")
        return query(stream)

    with monkeypatch.context() as patch:
        patch.setattr(FileHandle, "mark_deletion", fail_mark)
        if fault in {"close", "rollback"}:
            patch.setattr(FileHandle, "close", fail_close)
        if fault == "query":
            patch.setattr(FileHandle, "deletion_pending", fail_query)
        view = finish(broker, manager, delete(broker, target, etag))
        assert view["state"] == "failed"
        if fault == "rollback":
            assert view["effect"] == "unknown"
            assert view["cleanup_pending"]
            assert storage.deletions(broker.session.run_id)
        else:
            assert view["effect"] == "none"
            assert not view["cleanup_pending"]
    assert broker.operations.is_idle()
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"
    if fault == "rollback":
        view = call(broker, "operations.get", {"operation_ref": view["operation_ref"]})
        assert view["effect"] == "unknown"
        assert view["cleanup_pending"] is False


def test_delayed_removal_keeps_cleanup_pending_until_original_entry_gone(files, tmp_path, monkeypatch):
    from ctypes import wintypes

    import ECL.plugins.runtime.file_api as file_api

    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    metadata = create(str(target_path), 0x100080, 7, None, 3, 0x200000, None)
    assert metadata != ctypes.c_void_p(-1).value
    original_clock = monotonic
    start = original_clock()
    # 加速业务预算，原生目录查询仍使用真正单调时钟；不等待三十秒。
    monkeypatch.setattr(file_api, "monotonic", lambda: start + (original_clock() - start) * 100)
    try:
        view = finish(broker, manager, delete(broker, target, etag))
        assert view["error_code"] == "PLUGIN_FILE_DELETE_PENDING"
        assert view["effect"] == "unknown"
        assert view["cleanup_pending"]
        deletion = storage.deletions(broker.session.run_id)[0]
        assert deletion.stream.is_closed
        assert deletion.directory.entry_identity("target.bin", monotonic() + 2) == deletion.identity
    finally:
        assert storage._root._native.close(metadata)
    target_path.write_bytes(b"replacement")
    view = call(broker, "operations.get", {"operation_ref": view["operation_ref"]})
    assert view["effect"] == "unknown"
    assert not view["cleanup_pending"]
    assert target_path.read_bytes() == b"replacement"


@pytest.mark.parametrize("phase", ["executing", "committing", "terminal"])
def test_delete_journal_failure_does_not_repeat_delete(files, tmp_path, monkeypatch, phase):
    import ECL.services.operations as module

    broker, manager, _, _, _ = files
    target, etag = published(broker, manager)
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    real = module.atomic_write_text

    def fail(path, content):
        record = json.loads(content)
        if record.get("kind") == "plugin_file_delete" and record["phase"] == phase:
            raise OSError("journal failure")
        return real(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_text", fail)
        receipt = delete(broker, target, etag, "journal-delete")
        view = finish(broker, manager, receipt)
        assert target_path.exists() == (phase != "terminal")
        assert view["effect"] == ("committed" if phase == "terminal" else "none")
        assert view["cleanup_pending"] == (phase == "terminal")
    view = call(broker, "operations.get", {"operation_ref": receipt["operation_ref"]})
    assert not view["cleanup_pending"]
    assert delete(broker, target, etag, "journal-delete") == receipt


def test_revocation_before_acceptance_preserves_file(files, tmp_path, monkeypatch):
    broker, manager, _, store, _ = files
    target, etag = published(broker, manager)
    original = FileHandle.digest

    def revoke(stream, deadline, max_bytes=8388608):
        digest = original(stream, deadline, max_bytes)
        deny(broker, store, "filesystem.delete")
        return digest

    monkeypatch.setattr(FileHandle, "digest", revoke)
    view = finish(broker, manager, delete(broker, target, etag))
    assert view["effect"] == "none"
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"


def test_executor_rejection_and_concurrent_same_mutation_are_safe(files, monkeypatch):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    with monkeypatch.context() as patch:
        patch.setattr(manager._executor, "submit", lambda *args: (_ for _ in ()).throw(RuntimeError("closed")))
        with pytest.raises(ProtocolError):
            delete(broker, target, etag)
        assert not storage.deletions(broker.session.run_id)
    with ThreadPoolExecutor(4) as callers:
        receipts = list(callers.map(lambda _: delete(broker, target, etag, "one-key"), range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert finish(broker, manager, receipts[0])["result"] == {"deleted": True}


@pytest.mark.parametrize("kind", ["8MiB", "oversize", "directory", "hardlink", "readonly"])
def test_target_limits_and_native_rejection_keep_original(files, tmp_path, kind):
    broker, manager, _, _, _ = files
    target, etag = published(broker, manager)
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    if kind in {"8MiB", "oversize"}:
        target_path.write_bytes(b"x" * (8388608 + (kind == "oversize")))
        if kind == "8MiB":
            etag = call(broker, "files.stat", {"target": target})["etag"]
    elif kind == "directory":
        target_path.unlink()
        target_path.mkdir()
    elif kind == "hardlink":
        (target_path.parent / "alias.bin").hardlink_to(target_path)
    else:
        target_path.chmod(0o444)
    try:
        view = finish(broker, manager, delete(broker, target, etag))
        assert view["state"] == ("completed" if kind == "8MiB" else "failed")
        assert target_path.exists() == (kind != "8MiB")
        if kind == "oversize":
            assert view["error_code"] == "PLUGIN_FILE_TOO_LARGE"
    finally:
        if kind == "readonly":
            target_path.chmod(0o666)


def test_delete_limit_cross_source_and_upload_target_occupancy(files, tmp_path):
    broker, manager, storage, _, create = files
    target, etag = published(broker, manager)
    other = create()
    with pytest.raises(ProtocolError):
        delete(other, target, etag)
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 10)
    receipts = []
    try:
        for index in range(4):
            name = f"selected{index}.bin"
            receipts.append(delete(broker, dict(target, relative_path=name), etag))
        with pytest.raises(ProtocolError) as error:
            delete(broker, dict(target, relative_path="limit.bin"), etag)
        assert error.value.error_code == "PLUGIN_RATE_LIMITED"
        with pytest.raises(ProtocolError) as error:
            begin(broker, "SELECTED0.bin")
        assert error.value.error_code == "PLUGIN_FILE_BUSY"
        assert len(storage.deletions(broker.session.run_id)) == 4
    finally:
        gate.set()
        blocker.result(10)
        for receipt in receipts:
            finish(broker, manager, receipt)


def test_staging_metadata_handle_delays_quota_release(files, tmp_path):
    from ctypes import wintypes

    broker, _, storage, _, _ = files
    _, write_ref = begin(broker)
    upload = storage.find(write_ref, broker.session.run_id)
    temporary_path = next(tmp_path.glob(f"plugin_data/*/files/{upload.temporary_name}"))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    metadata = create(str(temporary_path), 0x100080, 7, None, 3, 0x200000, None)
    assert metadata != ctypes.c_void_p(-1).value
    try:
        assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": False}
        assert storage.find(write_ref, broker.session.run_id) is upload
        assert not storage.close()
    finally:
        assert upload.directory._native.close(metadata)
    assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": True}
    assert not temporary_path.exists()


def test_revocation_after_native_acceptance_does_not_undo_delete(files, tmp_path, monkeypatch):
    broker, manager, _, store, _ = files
    target, etag = published(broker, manager)
    mark = FileHandle.mark_deletion

    def revoke_after_mark(stream, pending):
        mark(stream, pending)
        if pending:
            deny(broker, store, "filesystem.delete")

    monkeypatch.setattr(FileHandle, "mark_deletion", revoke_after_mark)
    view = finish(broker, manager, delete(broker, target, etag))
    assert view["state"] == "completed" and view["effect"] == "committed"
    assert view["result"] is None
    assert not list(tmp_path.glob("plugin_data/*/files/target.bin"))


def test_queued_deadline_expires_without_opening_or_marking(files, tmp_path, monkeypatch):
    import ECL.plugins.runtime.file_api as file_api

    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 10)
    try:
        receipt = delete(broker, target, etag)
        monkeypatch.setattr(file_api, "monotonic", lambda: monotonic() + 31)
    finally:
        gate.set()
        blocker.result(10)
    view = finish(broker, manager, receipt)
    assert view["error_code"] == "PLUGIN_DEADLINE_EXCEEDED"
    assert view["effect"] == "none"
    assert not storage.deletions(broker.session.run_id)
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"
