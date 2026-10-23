from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from pydantic import ValidationError

from ECL.api.plugin_models import PluginFileRecoveryResolve
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.runtime.models import FileReplaceBeginRequest
from ECL.utils.windows_file_handles import FileBoundaryError, FileHandle
from tests.test_plugin_file_delete import published
from tests.test_plugin_host_files import call, chunk, commit, deny, files, finish, root

__all__ = ["files"]


def replace_begin(broker, target, etag, content=b"new bytes"):
    return call(
        broker,
        "files.replace_begin",
        {
            "target": target,
            "expected_etag": etag,
            "total_bytes": len(content),
        },
    )["write_ref"]


def test_replace_real_version_backup_and_cleanup(files, tmp_path):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    write_ref = replace_begin(broker, target, etag)
    chunk(broker, write_ref, b"new bytes")
    view = finish(broker, manager, commit(broker, write_ref, b"new bytes"))
    assert view["state"] == "completed"
    assert view["effect"] == "committed"
    assert not view["cleanup_pending"]
    assert view["result"]["etag"] != etag
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"new bytes"
    assert not storage.replacements.has_pending()
    assert list((tmp_path / "plugin_file_replacements").iterdir()) == []


@pytest.mark.parametrize("size", [0, 8388608])
def test_replace_supported_content_limits(files, tmp_path, size):
    broker, manager, _, _, _ = files
    target, version = published(broker, manager)
    content = b"r" * size
    upload = replace_begin(broker, target, version, content)
    for offset in range(0, size, 32768):
        chunk(broker, upload, content[offset : offset + 32768], offset)
    result = finish(broker, manager, commit(broker, upload, content))
    assert result["state"] == "completed" and result["effect"] == "committed"
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == content


@pytest.mark.parametrize("change", ["same_bytes_new_object", "changed_bytes"])
def test_replace_requires_exact_original_object_and_content(files, tmp_path, change):
    broker, manager, _, _, _ = files
    target, version = published(broker, manager)
    upload = replace_begin(broker, target, version)
    chunk(broker, upload, b"new bytes")
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    if change == "same_bytes_new_object":
        target_path.unlink()
        target_path.write_bytes(b"hello")
    else:
        target_path.write_bytes(b"changed")
    result = finish(broker, manager, commit(broker, upload, b"new bytes"))
    assert result["effect"] == "none" and result["error_code"] == "PLUGIN_FILE_CHANGED"
    assert target_path.read_bytes() == (b"hello" if change == "same_bytes_new_object" else b"changed")


@pytest.mark.parametrize(
    "change",
    [
        {"expected_etag": None},
        {"expected_etag": "A" * 64},
        {"total_bytes": True},
        {"total_bytes": -1},
        {"total_bytes": 8388609},
        {"overwrite": True},
        {"target": {"root_ref": "a" * 32, "relative_path": "NUL.txt"}},
    ],
)
def test_replace_request_rejects_ambiguous_or_unsafe_input(change):
    request = {
        "target": {"root_ref": "a" * 32, "relative_path": "file.bin"},
        "expected_etag": "b" * 64,
        "total_bytes": 0,
    }
    with pytest.raises(ValidationError):
        FileReplaceBeginRequest.model_validate(request | change)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "save_as"},
        {"action": "discard", "saved_name": "ignored.bin"},
        {"action": "save_as", "saved_name": "CON.txt"},
        {"expected_revision": True},
        {"action": "overwrite"},
        {"host_path": "C:/private"},
    ],
)
def test_recovery_choice_has_strict_action_and_version_boundary(change):
    request = {"recovery_ref": "a" * 32, "expected_revision": 1, "expected_etag": "b" * 64, "action": "restore"}
    with pytest.raises(ValidationError):
        PluginFileRecoveryResolve.model_validate(request | change)


def test_replace_permission_is_independent_and_result_requires_read(files, tmp_path):
    broker, manager, _, store, _ = files
    target, etag = published(broker, manager)
    deny(broker, store, "filesystem.write")
    deny(broker, store, "filesystem.delete")
    deny(broker, store, "filesystem.read")
    target["root_ref"] = root(broker, "filesystem.replace")
    upload = replace_begin(broker, target, etag)
    chunk(broker, upload, b"new bytes")
    result = finish(broker, manager, commit(broker, upload, b"new bytes"))
    assert result["state"] == "completed" and result["result"] is None
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"new bytes"
    deny(broker, store, "filesystem.replace")
    with pytest.raises(ProtocolError) as error:
        replace_begin(broker, target, etag)
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"


@pytest.mark.parametrize("reason", ["cancel", "revoke", "deadline"])
def test_queued_replace_keeps_original_and_actual_reservation_until_stopped(files, tmp_path, monkeypatch, reason):
    import ECL.plugins.runtime.file_api as api_module
    import ECL.plugins.runtime.operation_adapters as operation_module

    broker, manager, storage, store, _ = files
    target, etag = published(broker, manager)
    upload = replace_begin(broker, target, etag)
    chunk(broker, upload, b"new bytes")
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 5)
    try:
        receipt = commit(broker, upload, b"new bytes")
        staged = storage.find(upload, broker.session.run_id)
        assert staged.original is None
        assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"
        with pytest.raises(ProtocolError) as busy:
            replace_begin(broker, target, etag)
        assert busy.value.error_code == "PLUGIN_FILE_BUSY"
        if reason == "cancel":
            call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
        elif reason == "revoke":
            deny(broker, store, "filesystem.replace")
        else:
            now = monotonic() + 31
            monkeypatch.setattr(api_module, "monotonic", lambda: now)
            monkeypatch.setattr(operation_module, "monotonic", lambda: now)
    finally:
        gate.set()
        blocker.result(5)
    view = finish(broker, manager, receipt)
    assert view["state"] in {"failed", "cancelled"} and view["effect"] == "none"
    assert not view["cleanup_pending"]
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"


def test_concurrent_same_commit_receipt_runs_once(files, tmp_path):
    broker, manager, _, _, _ = files
    target, etag = published(broker, manager)
    upload = replace_begin(broker, target, etag)
    chunk(broker, upload, b"new bytes")
    with ThreadPoolExecutor(4) as callers:
        receipts = list(callers.map(lambda _: commit(broker, upload, b"new bytes", "one-replace"), range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert finish(broker, manager, receipts[0])["effect"] == "committed"
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"new bytes"


@pytest.mark.parametrize("phase", ["prepared", "old_saved", "published"])
def test_replacement_record_failure_preserves_original_or_actual_published_file(files, tmp_path, monkeypatch, phase):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    upload = replace_begin(broker, target, etag)
    chunk(broker, upload, b"new bytes")
    save = storage.replacements._save

    def fail(item):
        if item.record.phase == phase:
            raise FileBoundaryError("记录写入失败", "PLUGIN_STORAGE_FAILED")
        return save(item)

    with monkeypatch.context() as patch:
        patch.setattr(storage.replacements, "_save", fail)
        view = finish(broker, manager, commit(broker, upload, b"new bytes"))
    assert view["state"] == "failed"
    assert view["effect"] == ("unknown" if phase == "published" else "none")
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == (
        b"new bytes" if phase == "published" else b"hello"
    )
    assert not storage.replacements.has_pending()


@pytest.mark.parametrize("failure", ["version", "before_move", "after_move"])
def test_replace_failure_preserves_original_and_cleans_staging(files, tmp_path, monkeypatch, failure):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    write_ref = replace_begin(broker, target, "0" * 64 if failure == "version" else etag)
    chunk(broker, write_ref, b"new bytes")
    move = FileHandle.move

    def fail_move(stream, name):
        if name.startswith(".replace-"):
            if failure == "after_move":
                move(stream, name)
            raise FileBoundaryError("测试移动失败", "PLUGIN_STORAGE_FAILED")
        return move(stream, name)

    if failure != "version":
        monkeypatch.setattr(FileHandle, "move", fail_move)
    view = finish(broker, manager, commit(broker, write_ref, b"new bytes"))
    assert view["state"] == "failed"
    assert view["effect"] == "none"
    assert not view["cleanup_pending"]
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"
    assert not storage.replacements.has_pending()
    assert not storage.uploads(broker.session.run_id)


def conflicting(broker, manager, tmp_path, monkeypatch):
    target, etag = published(broker, manager)
    write_ref = replace_begin(broker, target, etag)
    chunk(broker, write_ref, b"new bytes")
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    publish = FileHandle.publish

    def race(stream, name):
        if name == "target.bin":
            target_path.write_bytes(b"foreign bytes")
        return publish(stream, name)

    with monkeypatch.context() as patch:
        patch.setattr(FileHandle, "publish", race)
        view = finish(broker, manager, commit(broker, write_ref, b"new bytes"))
    assert view["state"] == "failed" and view["effect"] == "unknown" and view["cleanup_pending"]
    return view, target_path


@pytest.mark.parametrize("action", ["save_as", "discard", "restore"])
def test_foreign_target_is_preserved_and_trusted_recovery_closes_pending(files, tmp_path, monkeypatch, action):
    broker, manager, storage, _, _ = files
    view, target_path = conflicting(broker, manager, tmp_path, monkeypatch)
    assert target_path.read_bytes() == b"foreign bytes"
    if action == "restore":
        target_path.unlink()
        # 构造恢复快照时禁止监管自动恢复，使本测试覆盖用户明确选择。
        with monkeypatch.context() as patch:
            patch.setattr(storage.replacements, "refresh", lambda *args: None)
            row = storage.replacements.page()["items"][0]
    else:
        row = storage.replacements.page()["items"][0]
    receipt = storage.replacements.resolve(
        row["recovery_ref"],
        row["revision"],
        row["etag"],
        action,
        "restored.bin" if action == "save_as" else None,
        manager,
    )
    assert (
        storage.replacements.resolve(
            row["recovery_ref"],
            row["revision"],
            row["etag"],
            action,
            "restored.bin" if action == "save_as" else None,
            manager,
        )
        == receipt
    )
    manager._operations[receipt["operationId"]].future.result(10)
    result = manager.get(receipt["operationId"])
    assert result["status"] == "completed", result
    assert target_path.read_bytes() == (b"hello" if action == "restore" else b"foreign bytes")
    if action == "save_as":
        assert (target_path.parent / "restored.bin").read_bytes() == b"hello"
    assert not storage.replacements.has_pending()
    task = call(broker, "operations.get", {"operation_ref": view["operation_ref"]})
    assert task["effect"] == "unknown" and not task["cleanup_pending"]


def test_recovery_rechecks_external_version_and_refresh_allows_failed_attempt_retry(files, tmp_path, monkeypatch):
    broker, manager, storage, _, _ = files
    _, target_path = conflicting(broker, manager, tmp_path, monkeypatch)
    store = storage.replacements
    row = store.page()["items"][0]
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 5)
    try:
        receipt = store.resolve(row["recovery_ref"], row["revision"], row["etag"], "save_as", "saved.bin", manager)
        target_path.write_bytes(b"changed foreign")
    finally:
        gate.set()
        blocker.result(5)
    manager._operations[receipt["operationId"]].future.result(5)
    assert manager.get(receipt["operationId"])["status"] == "failed"
    assert store.resolve(row["recovery_ref"], row["revision"], row["etag"], "save_as", "saved.bin", manager) == receipt
    refreshed = store.page()["items"][0]
    assert refreshed["revision"] > row["revision"]
    retried = store.resolve(
        refreshed["recovery_ref"], refreshed["revision"], refreshed["etag"], "save_as", "saved.bin", manager
    )
    manager._operations[retried["operationId"]].future.result(5)
    assert manager.get(retried["operationId"])["status"] == "completed"
    assert target_path.read_bytes() == b"changed foreign"
    assert (target_path.parent / "saved.bin").read_bytes() == b"hello"


def test_recovery_receipt_is_stable_when_task_status_changes_before_submit_returns(files, tmp_path, monkeypatch):
    broker, manager, storage, _, _ = files
    conflicting(broker, manager, tmp_path, monkeypatch)
    row = storage.replacements.page()["items"][0]
    submit = manager.submit

    def fast_status(*args):
        receipt = submit(*args)
        return {**receipt, "status": "running"}

    monkeypatch.setattr(manager, "submit", fast_status)
    receipt = storage.replacements.resolve(row["recovery_ref"], row["revision"], row["etag"], "discard", None, manager)
    assert receipt["status"] == "pending"
    manager._operations[receipt["operationId"]].future.result(5)
    assert manager.get(receipt["operationId"])["status"] == "completed"


def test_revocation_during_original_hash_prevents_acceptance(files, tmp_path, monkeypatch):
    broker, manager, _, grants, _ = files
    target, etag = published(broker, manager)
    upload = replace_begin(broker, target, etag)
    chunk(broker, upload, b"new bytes")
    # 只拦截正式原对象的哈希，暂存仍按真实内容核验。
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    original_digest = FileHandle.digest
    calls = 0

    def revoke_on_original(stream, deadline, max_bytes=8388608):
        nonlocal calls
        result = original_digest(stream, deadline, max_bytes)
        calls += 1
        if calls == 2:
            deny(broker, grants, "filesystem.replace")
        return result

    monkeypatch.setattr(FileHandle, "digest", revoke_on_original)
    view = finish(broker, manager, commit(broker, upload, b"new bytes"))
    assert view["effect"] == "none" and not view["cleanup_pending"]
    assert target_path.read_bytes() == b"hello"


def test_backup_metadata_handle_retains_cleanup_and_reservation_until_real_removal(files, tmp_path):
    from ctypes import wintypes

    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    target_path = next(tmp_path.glob("plugin_data/*/files/target.bin"))
    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
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
    try:
        upload = replace_begin(broker, target, etag)
        chunk(broker, upload, b"new bytes")
        view = finish(broker, manager, commit(broker, upload, b"new bytes"))
        assert view["effect"] == "committed" and view["cleanup_pending"]
        assert storage.replacements.has_pending()
        assert storage.uploads(broker.session.run_id)
        assert target_path.read_bytes() == b"new bytes"
    finally:
        assert storage._root._native.close(metadata)
    assert broker.operations.is_idle()
    assert not storage.replacements.has_pending()
    assert target_path.read_bytes() == b"new bytes"


def test_record_format_rejects_foreign_paths_and_preserves_unknown_file(tmp_path):
    from ECL.plugins.runtime.file_storage import PluginFileStorage

    records = tmp_path / "plugin_file_replacements"
    records.mkdir()
    invalid = records / ("a" * 32 + ".json")
    invalid.write_text(json.dumps({"target_name": "../private"}), encoding="utf-8")
    storage = PluginFileStorage(tmp_path)
    try:
        view = storage.replacements.page()
        assert view["items"][0]["state"] == "invalid"
        assert storage.replacements.has_pending()
        assert invalid.exists()
    finally:
        # 没有业务文件或实际句柄，测试主动关闭已打开的容器；生产仍拒绝丢弃未解决记录。
        assert not storage.close()
        for directory in (storage._replacement_records, storage._root):
            if directory:
                directory.close()


@pytest.mark.skipif(sys.platform != "win32", reason="真实Windows NTFS进程终止与恢复")
@pytest.mark.parametrize(
    "point",
    [
        "before_prepared",
        "prepared",
        "after_move",
        "old_saved",
        "after_publish",
        "published",
        "after_backup_deleted",
        "conflict",
    ],
)
def test_actual_process_crash_recovers_only_registered_objects_and_never_resumes_upload(tmp_path, point):
    from ECL.foundation import EventBus
    from ECL.plugins.runtime.file_storage import PluginFileStorage
    from ECL.services.operations import OperationManager

    process = subprocess.Popen(
        [sys.executable, "-m", "tests.plugin_file_replace_crash_helper", str(tmp_path), point],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = monotonic() + 15
        while not (tmp_path / "ready").exists():
            if process.poll() is not None:
                pytest.fail(process.communicate(timeout=1)[1].decode("utf-8", errors="replace"))
            assert monotonic() < deadline
            sleep(0.025)
    finally:
        process.terminate()
        process.communicate(timeout=5)
    storage = PluginFileStorage(tmp_path)
    manager = OperationManager(tmp_path, EventBus())
    target_path = tmp_path / "plugin_data" / ("a" * 32) / "files/target.bin"
    try:
        page = storage.replacements.page()
        if point == "conflict":
            assert target_path.read_bytes() == b"foreign"
            row = page["items"][0]
            receipt = storage.replacements.resolve(
                row["recovery_ref"], row["revision"], row["etag"], "save_as", "restored.bin", manager
            )
            manager._operations[receipt["operationId"]].future.result(5)
            assert manager.get(receipt["operationId"])["status"] == "completed"
            assert (target_path.parent / "restored.bin").read_bytes() == b"original"
            assert target_path.read_bytes() == b"foreign"
        else:
            assert page["items"] == []
            assert target_path.read_bytes() == (
                b"new" if point in {"after_publish", "published", "after_backup_deleted"} else b"original"
            )
        assert not storage.replacements.has_pending()
        if point != "before_prepared":
            assert not list(target_path.parent.glob(".upload-*"))
    finally:
        manager.close()
        assert storage.close()
