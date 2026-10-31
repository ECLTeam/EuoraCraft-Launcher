from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from pydantic import ValidationError

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.runtime.models import FileReplaceBeginRequest
from ECL.utils.windows_file_handles import FileBoundaryError, FileHandle
from tests.test_plugin_file_delete import published
from tests.test_plugin_host_files import call, chunk, commit, files, finish, invalidate_base

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


def test_replace_real_version_publishes_and_cleans_staging(files, tmp_path):
    broker, manager, _, _, _ = files
    target, etag = published(broker, manager)
    write_ref = replace_begin(broker, target, etag)
    chunk(broker, write_ref, b"new bytes")
    view = finish(broker, manager, commit(broker, write_ref, b"new bytes"))
    assert view["state"] == "completed"
    assert view["effect"] == "committed"
    assert not view["cleanup_pending"]
    assert view["result"]["etag"] != etag
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"new bytes"
    # 降级后不再保留原文件备份，也不再登记恢复目录
    assert not list(tmp_path.glob("plugin_data/*/files/.replace-*"))
    assert not (tmp_path / "plugin_file_replacements").exists()


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


def test_basic_replacement_returns_version_and_does_not_rebind_old_reference(files, tmp_path):
    broker, manager, _, store, _ = files
    target, etag = published(broker, manager)
    upload = replace_begin(broker, target, etag)
    chunk(broker, upload, b"new bytes")
    result = finish(broker, manager, commit(broker, upload, b"new bytes"))
    assert result["state"] == "completed" and result["result"]["etag"] != etag
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"new bytes"
    invalidate_base(broker, store)
    with pytest.raises(ProtocolError) as error:
        replace_begin(broker, target, etag)
    assert error.value.error_code == "PLUGIN_RESOURCE_NOT_FOUND"


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
            invalidate_base(broker, store)
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


@pytest.mark.parametrize("failure", ["version", "replace"])
def test_replace_failure_preserves_original_and_cleans_staging(files, tmp_path, monkeypatch, failure):
    broker, manager, storage, _, _ = files
    target, etag = published(broker, manager)
    write_ref = replace_begin(broker, target, "0" * 64 if failure == "version" else etag)
    chunk(broker, write_ref, b"new bytes")

    def fail_replace(stream, name):
        raise FileBoundaryError("测试替换失败", "PLUGIN_STORAGE_FAILED")

    if failure != "version":
        monkeypatch.setattr(FileHandle, "replace", fail_replace)
    view = finish(broker, manager, commit(broker, write_ref, b"new bytes"))
    assert view["state"] == "failed"
    assert view["effect"] == "none"
    assert not view["cleanup_pending"]
    # 替换未发生时旧内容必须完整保留
    assert next(tmp_path.glob("plugin_data/*/files/target.bin")).read_bytes() == b"hello"
    assert not list(tmp_path.glob("plugin_data/*/files/.upload-*"))
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
            invalidate_base(broker, grants)
        return result

    monkeypatch.setattr(FileHandle, "digest", revoke_on_original)
    view = finish(broker, manager, commit(broker, upload, b"new bytes"))
    assert view["effect"] == "none" and not view["cleanup_pending"]
    assert target_path.read_bytes() == b"hello"
