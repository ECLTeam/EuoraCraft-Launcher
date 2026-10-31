from __future__ import annotations

import base64
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ECL.foundation import EventBus
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.file_storage import PluginFileStorage
from ECL.plugins.runtime.models import FileRef, FileWriteBeginRequest
from ECL.services.operations import OperationManager
from ECL.utils.windows_file_handles import FileBoundaryError, FileHandle


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "a/b",
        "a\\b",
        "/root",
        "C:ads",
        "a:",
        "NUL.txt",
        "COM9",
        "a.",
        ".",
        "..",
        "a\n",
        "a ",
        "中文",
        "x" * 121,
        " a",
        "a\0",
    ],
)
def test_file_names_are_rejected_instead_of_sanitized(name):
    with pytest.raises(ValidationError):
        FileRef(root_ref="a" * 32, relative_path=name)


@pytest.mark.parametrize("overwrite", [True, 0, 1, "false", None])
def test_create_only_requires_explicit_strict_false(overwrite):
    with pytest.raises(ValidationError):
        FileWriteBeginRequest(target=FileRef(root_ref="a" * 32, relative_path="x"), total_bytes=0, overwrite=overwrite)


def test_filesystem_declaration_cannot_select_host_paths():
    for capability in ():
        with pytest.raises(ValidationError):
            PermissionDeclaration(capability=capability, resource_refs=("host-path",))


@pytest.fixture
def files(tmp_path):
    if sys.platform != "win32":
        pytest.skip("真实 Windows NTFS 文件事务")
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("value = 1", encoding="utf-8")
    capabilities = set()
    binding = PluginContentBinding.capture(
        code, tuple(PermissionDeclaration(capability=c) for c in sorted(capabilities)), "windows-x86_64-cp311"
    )
    store = PluginGrantStore(tmp_path, implemented_capabilities=frozenset(capabilities))
    manager = OperationManager(tmp_path, EventBus(), max_workers=1)
    storage = PluginFileStorage(tmp_path)
    brokers = []

    def create(installation=None, allowed=None):
        session = store.open_session(installation or uuid4().hex, binding)
        store.update(
            session,
            tuple(
                PermissionDecision(capability=c, decision="allow")
                for c in (capabilities if allowed is None else allowed)
            ),
            expected_revision=session.view().revision,
            content_digest=binding.content_digest,
            caller=PermissionManagementCaller.main_webview,
        )
        broker = PluginHostBroker(
            session, tmp_path, lambda *args: {}, run_state=lambda: "enabled", operations=manager, file_storage=storage
        )
        brokers.append(broker)
        return broker

    broker = create()
    yield broker, manager, storage, store, create
    for item in brokers:
        item.close()
        item.session.close()
    manager.close()
    for item in tuple(manager._operations.values()):
        if item.future is not None and not item.future.cancelled():
            item.future.result(10)
    for item in brokers:
        assert item.wait_idle(1)
    assert storage.close()


def call(broker, method, params=None, key=None):
    return broker.handle(RpcRequest(method, params or {}, monotonic() + 30, key or uuid4().hex))


def root(broker, capability="plugin.base"):
    result = call(broker, "resources.list", {"capability": capability})
    return result["items"][0]["root_ref"]


def begin(broker, name="snapshot.json", content=b"hello", reference=None):
    target = {"root_ref": reference or root(broker), "relative_path": name}
    result = call(broker, "files.write_begin", {"target": target, "total_bytes": len(content), "overwrite": False})
    return target, result["write_ref"]


def chunk(broker, write_ref, content=b"hello", offset=0, key=None):
    return call(
        broker,
        "files.write_chunk",
        {"write_ref": write_ref, "offset": offset, "data_b64": base64.b64encode(content).decode()},
        key,
    )


def commit(broker, write_ref, content=b"hello", key=None):
    return call(
        broker, "files.write_commit", {"write_ref": write_ref, "sha256": hashlib.sha256(content).hexdigest()}, key
    )


def finish(broker, manager, receipt):
    reference = receipt["operation_ref"]
    identity = broker.operations._references[reference].operation_id
    manager._operations[identity].future.result(10)
    return call(broker, "operations.get", {"operation_ref": reference})


def invalidate_base(broker, store):
    store.update(
        broker.session,
        (),
        expected_revision=broker.session.view().revision,
        content_digest=broker.session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


def deny(broker, store, capability):
    store.update(
        broker.session,
        (PermissionDecision(capability=capability, decision="deny"),),
        expected_revision=broker.session.view().revision,
        content_digest=broker.session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


def test_first_read_has_no_writes_and_zero_length_commit_is_real(files, tmp_path):
    broker, manager, storage, _, _ = files
    reference = root(broker, "plugin.base")
    target = {"root_ref": reference, "relative_path": ""}
    assert call(broker, "files.stat", {"target": target})["kind"] == "directory"
    assert call(broker, "files.list", {"directory": target}) == {"items": [], "next_cursor": None}
    assert not (tmp_path / "plugin_data").exists()
    assert not (tmp_path / "plugin_file_spaces").exists()
    target, write_ref = begin(broker, content=b"", reference=reference)
    receipt = commit(broker, write_ref, b"", "commit")
    assert receipt["state"] == "queued"
    assert commit(broker, write_ref, b"", "commit") == receipt
    view = finish(broker, manager, receipt)
    assert view["state"] == "completed"
    assert view["effect"] == "committed"
    assert view["result"]["size"] == 0
    assert storage.uploads(broker.session.run_id) == ()
    read = call(broker, "files.read", {"target": target, "offset": 0, "count": 1})
    assert read["data_b64"] == "" and read["eof"]
    record = json.loads(next((tmp_path / "plugin_file_spaces").glob("*.json")).read_bytes())
    assert record["state"] == "ready"
    assert set(view["result"]) == {"file_ref", "name", "kind", "size", "modified_ms", "etag"}


def test_chunks_dedupe_sequence_hash_failure_and_create_only_collision(files, tmp_path):
    broker, manager, storage, _, _ = files
    target, write_ref = begin(broker)
    assert chunk(broker, write_ref, key="one") == chunk(broker, write_ref, key="one") == {"received_bytes": 5}
    with pytest.raises(ProtocolError) as error:
        chunk(broker, write_ref)
    assert error.value.error_code == "PLUGIN_WRITE_SEQUENCE_INVALID"
    view = finish(broker, manager, commit(broker, write_ref, b"wrong"))
    assert (view["state"], view["effect"], view["error_code"]) == ("failed", "none", "PLUGIN_HASH_MISMATCH")
    assert not storage.uploads(broker.session.run_id)
    target, write_ref = begin(broker, reference=target["root_ref"])
    chunk(broker, write_ref)
    directory = tmp_path / "plugin_data" / broker.session.view().installation_id / "files"
    (directory / "snapshot.json").write_bytes(b"external")
    view = finish(broker, manager, commit(broker, write_ref))
    assert view["state"] == "failed" and view["error_code"] == "PLUGIN_FILE_EXISTS"
    assert (directory / "snapshot.json").read_bytes() == b"external"
    assert list(directory.iterdir()) == [directory / "snapshot.json"]


def test_read_etag_detects_equal_size_content_with_restored_time(files, tmp_path):
    import os

    broker, manager, _, _, _ = files
    target, write_ref = begin(broker)
    chunk(broker, write_ref)
    finish(broker, manager, commit(broker, write_ref))
    view = call(broker, "files.stat", {"target": target})
    path = tmp_path / "plugin_data" / broker.session.view().installation_id / "files/snapshot.json"
    stat = path.stat()
    path.write_bytes(b"other")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(ProtocolError) as error:
        call(broker, "files.read", {"target": target, "offset": 0, "count": 5, "expected_etag": view["etag"]})
    assert error.value.error_code == "PLUGIN_FILE_CHANGED"
    read = call(broker, "files.read", {"target": target, "offset": 1, "count": 2})
    assert base64.b64decode(read["data_b64"]) == b"th" and not read["eof"]
    assert (
        call(broker, "files.list", {"directory": {**target, "relative_path": ""}})["items"][0]["name"]
        == "snapshot.json"
    )


@pytest.mark.parametrize(
    "encoded",
    ["!!!!", "aA===", "aB==", "YWJj\n", "", "YQ==" * 20000],
    ids=["alphabet", "padding", "pad-bits", "newline", "empty", "oversized"],
)
def test_strict_chunks_reject_invalid_or_oversized_base64(files, encoded):
    broker, _, _, _, _ = files
    _, write_ref = begin(broker)
    with pytest.raises(ProtocolError):
        call(broker, "files.write_chunk", {"write_ref": write_ref, "offset": 0, "data_b64": encoded})
    assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": True}
    assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": False}


def test_basic_private_access_returns_result_and_old_epoch_hides_snapshot(files):
    broker, manager, _, store, create = files
    writer = create(allowed=set())
    target, write_ref = begin(writer)
    chunk(writer, write_ref)
    assert finish(writer, manager, commit(writer, write_ref))["result"] is not None
    assert call(writer, "files.stat", {"target": target})["size"] == 5
    _, write_ref = begin(broker)
    chunk(broker, write_ref)
    receipt = commit(broker, write_ref, key="accepted")
    assert finish(broker, manager, receipt)["result"] is not None
    invalidate_base(broker, store)
    assert call(broker, "operations.get", {"operation_ref": receipt["operation_ref"]})["result"] is None
    assert call(broker, "mutations.get", {"mutation_key": "accepted"})["result"] == receipt


def test_permission_change_and_full_receipt_table_still_allow_abort(files):
    broker, _, storage, store, _ = files
    _, write_ref = begin(broker)
    broker.max_receipts = len(broker._receipts)
    invalidate_base(broker, store)
    assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": True}
    assert not storage.uploads(broker.session.run_id)


def test_queued_cancel_runs_cleanup_without_business_worker(files):
    broker, manager, storage, _, _ = files
    entered, release = Event(), Event()
    manager.submit("host_blocker", lambda context: (entered.set(), release.wait(10)))
    assert entered.wait(3)
    try:
        _, write_ref = begin(broker)
        chunk(broker, write_ref)
        receipt = commit(broker, write_ref)
        assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": False}
        call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
        release.set()
        view = finish(broker, manager, receipt)
        assert view["state"] == "cancelled" and view["effect"] == "none"
        assert not storage.uploads(broker.session.run_id)
    finally:
        release.set()


def test_cleanup_failure_holds_quota_and_retries_without_republishing(files, monkeypatch):
    broker, manager, storage, _, _ = files
    _, write_ref = begin(broker)
    chunk(broker, write_ref)
    real = FileHandle.discard

    def fail(self):
        raise FileBoundaryError("busy", "PLUGIN_FILE_BUSY")

    monkeypatch.setattr(FileHandle, "discard", fail)
    view = finish(broker, manager, commit(broker, write_ref, b"wrong"))
    assert view["effect"] == "staged"
    assert storage.uploads(broker.session.run_id)
    assert not broker.wait_idle()
    monkeypatch.setattr(FileHandle, "discard", real)
    assert broker.wait_idle()
    assert call(broker, "operations.get", {"operation_ref": view["operation_ref"]})["effect"] == "none"


def test_close_during_chunk_waits_for_actual_io(files, monkeypatch):
    broker, _, storage, _, _ = files
    _, write_ref = begin(broker)
    entered, release = Event(), Event()
    real = FileHandle.write

    def blocked(self, offset, content):
        entered.set()
        assert release.wait(10)
        return real(self, offset, content)

    monkeypatch.setattr(FileHandle, "write", blocked)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(chunk, broker, write_ref)
        assert entered.wait(3)
        broker.close()
        assert not broker.wait_idle()
        assert storage.uploads(broker.session.run_id)
        release.set()
        with pytest.raises(ProtocolError):
            future.result(5)
    assert broker.wait_idle(1)


@pytest.mark.parametrize("kind", ["preparing", "corrupt", "lost", "identity", "duplicate"])
def test_damaged_space_never_reinitialized_or_adopted(files, tmp_path, kind):
    broker, _, _, _, _ = files
    target, write_ref = begin(broker)
    call(broker, "files.write_abort", {"write_ref": write_ref})
    path = tmp_path / "plugin_file_spaces" / f"{broker.session.view().installation_id}.json"
    record = json.loads(path.read_bytes())
    if kind == "lost":
        path.unlink()
    elif kind == "corrupt":
        path.write_text("broken", encoding="utf-8")
    elif kind == "duplicate":
        path.write_text('{"state":"preparing",' + json.dumps(record)[1:], encoding="utf-8")
    else:
        if kind == "preparing":
            record["state"] = "preparing"
        else:
            record["identity"]["index"] += 1
        path.write_text(json.dumps(record), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ProtocolError):
        begin(broker, reference=target["root_ref"])
    assert (path.read_bytes() if path.exists() else None) == before


def test_max_file_upload_uses_fewer_than_receipt_limit_and_exact_bytes(files, tmp_path):
    broker, manager, _, _, _ = files
    content = bytes(range(256)) * 32768
    target, write_ref = begin(broker, content=content)
    for offset in range(0, len(content), 32768):
        chunk(broker, write_ref, content[offset : offset + 32768], offset)
    view = finish(broker, manager, commit(broker, write_ref, content))
    assert view["state"] == "completed" and view["result"]["size"] == 8388608
    path = tmp_path / "plugin_data" / broker.session.view().installation_id / "files" / target["relative_path"]
    assert path.read_bytes() == content
    assert len(broker._receipts) == 258


def test_upload_count_disk_quota_expiry_and_foreign_ref(files, tmp_path):
    broker, _, storage, _, create = files
    references = [begin(broker, name=f"f{i}")[1] for i in range(4)]
    with pytest.raises(ProtocolError) as error:
        begin(broker, name="overflow")
    assert error.value.error_code == "PLUGIN_RATE_LIMITED"
    other = create()
    assert call(other, "files.write_abort", {"write_ref": references[0]}) == {"released": False}
    storage.find(references[0], broker.session.run_id).touched_at -= 61
    broker.refresh_permissions()
    assert len(storage.uploads(broker.session.run_id)) == 3
    for reference in references[1:]:
        call(broker, "files.write_abort", {"write_ref": reference})
    directory = tmp_path / "plugin_data" / broker.session.view().installation_id / "files"
    (directory / ".upload-crash-left").write_bytes(b"old bytes")
    storage.max_space_bytes = 8
    with pytest.raises(ProtocolError) as error:
        begin(broker, name="full", content=b"")
    assert error.value.error_code == "PLUGIN_STORAGE_QUOTA_EXCEEDED"
    assert (directory / ".upload-crash-left").read_bytes() == b"old bytes"


def test_publication_survives_close_failure_and_run_close(files, tmp_path, monkeypatch):
    broker, manager, storage, _, _ = files
    target, write_ref = begin(broker)
    chunk(broker, write_ref)
    real = FileHandle.close

    def fail_published(self):
        if self.is_published:
            raise FileBoundaryError("close pending", "PLUGIN_FILE_BUSY")
        return real(self)

    monkeypatch.setattr(FileHandle, "close", fail_published)
    view = finish(broker, manager, commit(broker, write_ref))
    assert view["state"] == "completed" and view["effect"] == "committed"
    assert not broker.wait_idle()
    broker.close()
    assert storage.uploads(broker.session.run_id)
    monkeypatch.setattr(FileHandle, "close", real)
    assert broker.wait_idle(1)
    exported = tmp_path / "plugin_data" / broker.session.view().installation_id / "files" / target["relative_path"]
    assert exported.read_bytes() == b"hello"


@pytest.mark.parametrize("phase", ["queued", "executing", "committing", "terminal"])
def test_file_task_journal_failure_never_republishes(files, tmp_path, monkeypatch, phase):
    import ECL.services.operations as module

    broker, manager, storage, _, _ = files
    target, write_ref = begin(broker)
    chunk(broker, write_ref)
    real = module.atomic_write_text

    def fail(path, content):
        if json.loads(content)["phase"] == phase:
            raise OSError("journal failure")
        return real(path, content)

    monkeypatch.setattr(module, "atomic_write_text", fail)
    receipt = commit(broker, write_ref, key="commit-journal")
    view = finish(broker, manager, receipt)
    exported = tmp_path / "plugin_data" / broker.session.view().installation_id / "files" / target["relative_path"]
    assert exported.exists() == (phase == "terminal")
    assert view["effect"] == ("committed" if phase == "terminal" else "none")
    assert not storage.uploads(broker.session.run_id)
    monkeypatch.setattr(module, "atomic_write_text", real)
    assert broker.wait_idle()
    assert commit(broker, write_ref, key="commit-journal") == receipt


def test_revoked_before_publication_removes_only_own_staging(files, tmp_path, monkeypatch):
    broker, manager, _, store, _ = files
    target, write_ref = begin(broker)
    chunk(broker, write_ref)
    entered, release = Event(), Event()
    real = FileHandle.flush

    def blocked(self):
        entered.set()
        assert release.wait(10)
        return real(self)

    monkeypatch.setattr(FileHandle, "flush", blocked)
    receipt = commit(broker, write_ref)
    try:
        assert entered.wait(3)
        invalidate_base(broker, store)
        broker.refresh_permissions()
    finally:
        release.set()
    view = finish(broker, manager, receipt)
    assert view["state"] in {"failed", "cancelled"} and view["effect"] == "none"
    exported = tmp_path / "plugin_data" / broker.session.view().installation_id / "files" / target["relative_path"]
    assert not exported.exists()


def test_executor_rejection_preserves_abortable_upload(files, monkeypatch):
    broker, manager, storage, _, _ = files
    _, write_ref = begin(broker)
    chunk(broker, write_ref)

    def reject(*args):
        raise RuntimeError("closed executor")

    monkeypatch.setattr(manager._executor, "submit", reject)
    with pytest.raises(ProtocolError) as error:
        commit(broker, write_ref)
    assert error.value.error_code == "OPERATION_MANAGER_CLOSED"
    assert not manager._operations
    assert storage.find(write_ref, broker.session.run_id).owner == "upload"
    assert call(broker, "files.write_abort", {"write_ref": write_ref}) == {"released": True}


def test_same_install_restart_preserves_data_reinstall_uses_new_space(files, tmp_path):
    broker, manager, _, _, create = files
    target, write_ref = begin(broker)
    chunk(broker, write_ref)
    finish(broker, manager, commit(broker, write_ref))
    installation = broker.session.view().installation_id
    broker.close()
    broker.session.close()
    assert broker.wait_idle(1)
    restarted = create(installation)
    with pytest.raises(ProtocolError) as error:
        call(restarted, "files.stat", {"target": target})
    assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"
    new_target = {**target, "root_ref": root(restarted, "plugin.base")}
    assert call(restarted, "files.stat", {"target": new_target})["size"] == 5
    reinstalled = create()
    assert (
        call(reinstalled, "files.list", {"directory": {"root_ref": root(reinstalled), "relative_path": ""}})["items"]
        == []
    )
    assert not (tmp_path / "plugin_data" / reinstalled.session.view().installation_id).exists()


def test_unrecorded_space_is_preserved_and_base_cannot_be_separately_granted(files, tmp_path):
    broker, _, _, store, _ = files
    identity = broker.session.view().installation_id
    (tmp_path / "plugin_data" / identity / "files").mkdir(parents=True)
    with pytest.raises(ProtocolError) as error:
        root(broker)
    assert error.value.error_code == "PLUGIN_FILE_INVALID"
    with pytest.raises(ValueError):
        store.update(
            broker.session,
            (PermissionDecision(capability="plugin.base", decision="allow"),),
            expected_revision=broker.session.view().revision,
            content_digest=broker.session.binding.content_digest,
            caller=PermissionManagementCaller.main_webview,
        )


def test_shared_quota_reservations_and_entry_limit(files, tmp_path):
    broker, _, storage, _, create = files
    target, write_ref = begin(broker)
    storage.max_run_bytes = 6
    with pytest.raises(ProtocolError) as error:
        begin(broker, name="second")
    assert error.value.error_code == "PLUGIN_STORAGE_QUOTA_EXCEEDED"
    storage.max_host_bytes = 6
    other = create()
    with pytest.raises(ProtocolError) as error:
        begin(other)
    assert error.value.error_code == "PLUGIN_STORAGE_QUOTA_EXCEEDED"
    call(broker, "files.write_abort", {"write_ref": write_ref})
    directory = tmp_path / "plugin_data" / broker.session.view().installation_id / "files"
    for index in range(1024):
        (directory / f"f{index}").touch()
    with pytest.raises(ProtocolError) as error:
        begin(broker, content=b"", reference=target["root_ref"])
    assert error.value.error_code == "PLUGIN_STORAGE_QUOTA_EXCEEDED"
    assert len(tuple(directory.iterdir())) == 1024


def test_global_close_cleans_task_cancelled_while_still_in_queue(files):
    broker, manager, storage, _, _ = files
    entered, release = Event(), Event()
    manager.submit("host_blocker", lambda context: (entered.set(), release.wait(10)))
    assert entered.wait(3)
    try:
        _, write_ref = begin(broker)
        chunk(broker, write_ref)
        receipt = commit(broker, write_ref)
        manager.close()
        view = call(broker, "operations.get", {"operation_ref": receipt["operation_ref"]})
        assert view["state"] == "cancelled" and view["effect"] == "none"
        assert not storage.uploads(broker.session.run_id)
    finally:
        release.set()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_created_handle_stays_owned_when_metadata_validation_fails(files, tmp_path, monkeypatch, cleanup_fails):
    broker, _, storage, _, _ = files
    # 先完成空间准备，故障只发生在新上传的首轮文件元数据检查。
    target, write_ref = begin(broker, content=b"")
    call(broker, "files.write_abort", {"write_ref": write_ref})
    directory = tmp_path / "plugin_data" / broker.session.view().installation_id / "files"
    root_identity = storage._spaces[broker.session.view().installation_id].identity
    original = FileHandle.information
    original_discard = FileHandle.discard

    def invalid(self):
        if self.directory.identity == root_identity:
            raise FileBoundaryError("metadata unavailable", "PLUGIN_FILE_IO_FAILED")
        return original(self)

    def busy(self):
        raise FileBoundaryError("cleanup pending", "PLUGIN_FILE_BUSY")

    monkeypatch.setattr(FileHandle, "information", invalid)
    if cleanup_fails:
        monkeypatch.setattr(FileHandle, "discard", busy)
    with pytest.raises(ProtocolError) as error:
        begin(broker, reference=target["root_ref"])
    assert error.value.error_code == "PLUGIN_FILE_IO_FAILED"
    assert bool(storage.uploads(broker.session.run_id)) == cleanup_fails
    assert bool(tuple(directory.iterdir())) == cleanup_fails
    monkeypatch.setattr(FileHandle, "information", original)
    monkeypatch.setattr(FileHandle, "discard", original_discard)
    assert broker.wait_idle()
    assert tuple(directory.iterdir()) == ()
