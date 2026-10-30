from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
from time import monotonic

import pytest

from ECL.foundation import EventBus
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.resources import RuntimeResources
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.game import GameService
from ECL.services.game.instance_profiles import InstanceProfileStore
from ECL.services.game.version_stats import VersionStatsStore
from ECL.services.operations import OperationManager
from ECL.utils.instance_reading import InstanceReadError
from ECL.utils.windows_file_handles import FileHandle
from tests.test_plugin_host_files import call, deny, finish


@pytest.fixture
def profiles(tmp_path):
    if sys.platform != "win32":
        pytest.skip("真实Windows NTFS资料提交")
    root = tmp_path / "game"
    for name in ("one", "two"):
        directory = root / "versions" / name
        (directory / ".ecl").mkdir(parents=True)
        (directory / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
        (directory / ".ecl/instance.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "alias": "Original",
                    "favorite": True,
                    "pinned": True,
                    "custom": {"private": "hidden"},
                }
            ),
            encoding="utf-8",
        )
    data_path = tmp_path / "data"
    notified = []
    store = InstanceProfileStore(data_path, VersionStatsStore(), on_changed=notified.append)
    game = GameService.__new__(GameService)
    game._lock = RLock()
    game._running_games = {}
    game._instance_profiles = store
    manager = OperationManager(data_path, EventBus(), max_workers=1)
    service = PluginWorkerService(
        data_path,
        instance_queries=game,
        instance_roots=lambda: (root,),
        instance_profiles=store.transactions,
        operations=manager,
    )
    source = tmp_path / "source"
    source.mkdir()
    metadata = {
        "name": "writer",
        "api_version": 2,
        "permissions": [{"capability": "instances.read"}, {"capability": "instances.manage"}],
    }
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (source / "main.py").write_text("value=1", encoding="utf-8")
    service.register(metadata, source, "directory")
    view = service.permissions("writer")
    roots = service.permission_resources("writer", view["content_digest"], None, "", None, 100)
    root_id = roots["items"][0]["resource_id"]
    children = roots
    selected = root_id
    service.update_permissions(
        "writer",
        view["content_digest"],
        view["revision"],
        (
            PermissionDecision(capability="instances.read", decision="allow", resource_refs=(root_id,)),
            PermissionDecision(capability="instances.manage", decision="allow", resource_refs=(selected,)),
        ),
        PermissionManagementCaller.main_webview,
        children["catalog_revision"],
    )
    entry = service._entry("writer")
    resources = RuntimeResources(entry.session, service._catalog, game, service.refresh_resources)
    broker = PluginHostBroker(
        entry.session,
        data_path,
        lambda *args: {},
        run_state=lambda: "enabled",
        operations=manager,
        resources=resources,
        instance_profiles=store.transactions,
    )
    resources_page = call(broker, "resources.list", {"capability": "instances.read"})
    instances = call(broker, "instances.scan", {"root_ref": resources_page["items"][0]["root_ref"]})["items"]
    first = next(item for item in instances if item["version_id"] == "one")
    yield broker, manager, store, service, root, first, notified, instances
    broker.close()
    manager.close()
    for operation in tuple(manager._operations.values()):
        if operation.future and not operation.future.cancelled():
            operation.future.result(10)
    assert broker.wait_idle(2)
    assert store.transactions.close()
    service.close()


def patch(broker, before, values, key=None):
    return call(
        broker,
        "instances.profile_patch",
        {"instance_ref": before["instance_ref"], "expected_profile_etag": before["profile_etag"], "patch": values},
        key,
    )


def test_profile_patch_preserves_unrelated_fields_and_returns_current_version(profiles):
    broker, manager, store, _, root, before, notified, _ = profiles
    result = finish(broker, manager, patch(broker, before, {"description": "New", "favorite": False, "tags": []}))
    assert result["state"] == "completed", result
    assert result["effect"] == "committed" and not result["cleanup_pending"]
    profile = json.loads((root / "versions/one/.ecl/instance.json").read_bytes())
    assert profile["pinned"] is True and profile["custom"] == {"private": "hidden"}
    assert profile["favorite"] is False and profile["tags"] == []
    assert "hidden" not in json.dumps(result["result"])
    current = call(broker, "instances.get", {"instance_ref": before["instance_ref"]})
    assert current["profile_etag"] == result["result"]["profile_etag"] != before["profile_etag"]
    assert notified == [root] and not store.transactions.has_pending()


def test_full_version_includes_unexposed_fields_and_same_content_new_object(profiles):
    broker, manager, _, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    old = path.read_bytes()
    path.unlink()
    path.write_bytes(old)
    result = finish(broker, manager, patch(broker, before, {"favorite": False}))
    assert result["effect"] == "none" and result["error_code"] == "PLUGIN_INSTANCE_PROFILE_CONFLICT"
    assert path.read_bytes() == old


def test_management_root_covers_other_existing_instances(profiles):
    broker, manager, _, _, _, _, _, instances = profiles
    second = next(item for item in instances if item["version_id"] == "two")
    result = finish(broker, manager, patch(broker, second, {"alias": "Managed"}))
    assert result["state"] == "completed" and result["result"]["alias"] == "Managed"


def test_cancel_queued_profile_releases_real_occupancy_without_writing(profiles):
    broker, manager, store, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    original = path.read_bytes()
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 5)
    try:
        receipt = patch(broker, before, {"alias": "Cancelled"})
        assert store.transactions.has_pending(root, "one")
        call(broker, "operations.cancel", {"operation_ref": receipt["operation_ref"]})
    finally:
        gate.set()
        blocker.result(5)
    result = finish(broker, manager, receipt)
    assert result["state"] == "cancelled" and not result["cleanup_pending"]
    assert path.read_bytes() == original and not store.transactions.has_pending()


@pytest.mark.parametrize("reason", ["instances.read", "instances.manage", "deadline"])
def test_queued_profile_rechecks_original_dual_permission_and_total_deadline(profiles, monkeypatch, reason):
    import ECL.plugins.runtime.instance_profiles as api_module
    import ECL.plugins.runtime.operation_adapters as operation_module

    broker, manager, store, service, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    original = path.read_bytes()
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 5)
    try:
        receipt = patch(broker, before, {"alias": "Expired"})
        with pytest.raises(InstanceReadError):
            store.patch_profile(root, "one", {"alias": "Desktop"})
        if reason == "deadline":
            now = monotonic() + 31
            monkeypatch.setattr(api_module, "monotonic", lambda: now)
            monkeypatch.setattr(operation_module, "monotonic", lambda: now)
        else:
            deny(broker, service.grants, reason)
    finally:
        gate.set()
        blocker.result(5)
    result = finish(broker, manager, receipt)
    assert result["state"] in {"failed", "cancelled"} and result["effect"] == "none"
    assert not result["cleanup_pending"] and path.read_bytes() == original
    assert not store.transactions.has_pending()


def test_same_profile_mutation_key_returns_one_task_and_read_revocation_hides_result(profiles):
    broker, manager, _, service, root, before, notified, _ = profiles
    with ThreadPoolExecutor(4) as callers:
        receipts = list(callers.map(lambda _: patch(broker, before, {"alias": "Once"}, "same-patch"), range(4)))
    assert all(receipt == receipts[0] for receipt in receipts)
    result = finish(broker, manager, receipts[0])
    assert result["effect"] == "committed" and result["result"]["alias"] == "Once"
    assert notified == [root]
    deny(broker, service.grants, "instances.read")
    redacted = call(broker, "operations.get", {"operation_ref": receipts[0]["operation_ref"]})
    assert redacted["effect"] == "committed" and redacted["result"] is None


@pytest.mark.parametrize("kind", ["readonly", "hardlink"])
def test_profile_write_rejects_nonwritable_or_shared_file_without_data_loss(profiles, kind):
    import stat

    broker, manager, _, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    original = path.read_bytes()
    if kind == "readonly":
        path.chmod(stat.S_IREAD)
    else:
        path.with_name("another.json").hardlink_to(path)
    try:
        result = finish(broker, manager, patch(broker, before, {"alias": "Forbidden"}))
        assert result["state"] == "failed" and result["effect"] == "none"
        assert not result["cleanup_pending"] and path.read_bytes() == original
    finally:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


@pytest.mark.parametrize("action", ["save_as", "discard", "restore"])
def test_foreign_profile_is_retained_and_only_trusted_recovery_finishes_original(profiles, monkeypatch, action):
    broker, manager, store, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    original = path.read_bytes()
    publish = FileHandle.publish

    def compete(stream, name):
        if name == "instance.json":
            path.write_text('{"alias":"External"}', encoding="utf-8")
        return publish(stream, name)

    with monkeypatch.context() as change:
        change.setattr(FileHandle, "publish", compete)
        result = finish(broker, manager, patch(broker, before, {"alias": "New"}))
    assert result["effect"] == "unknown" and result["cleanup_pending"]
    assert path.read_text(encoding="utf-8") == '{"alias":"External"}'
    with pytest.raises(InstanceReadError):
        store.patch_profile(root, "one", {"alias": "Desktop"})
    if action == "restore":
        path.unlink()
        with monkeypatch.context() as change:
            change.setattr(store.transactions, "refresh", lambda *args: None)
            row = store.transactions.page()["items"][0]
    else:
        row = store.transactions.page()["items"][0]
    receipt = store.transactions.resolve(
        row["recovery_ref"], row["revision"], row["etag"], action, "original" if action == "save_as" else None, manager
    )
    manager._operations[receipt["operationId"]].future.result(5)
    assert manager.get(receipt["operationId"])["status"] == "completed"
    if action == "restore":
        assert path.read_bytes() == original
    else:
        assert path.read_text(encoding="utf-8") == '{"alias":"External"}'
    if action == "save_as":
        assert (path.parent / "recovered-profile-original.json").read_bytes() == original
    assert not store.transactions.has_pending()


@pytest.mark.parametrize("phase", ["prepared", "old_saved", "published"])
def test_profile_record_failure_preserves_original_or_actual_publication(profiles, monkeypatch, phase):
    broker, manager, store, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    original = path.read_bytes()
    save = store.transactions._save

    def fail(item):
        if item.record.phase == phase:
            raise OSError("record unavailable")
        return save(item)

    with monkeypatch.context() as change:
        change.setattr(store.transactions, "_save", fail)
        result = finish(broker, manager, patch(broker, before, {"alias": "Published"}))
    assert result["state"] == "failed"
    assert result["effect"] == ("unknown" if phase == "published" else "none")
    assert not result["cleanup_pending"]
    assert (
        (json.loads(path.read_bytes())["alias"] == "Published")
        if phase == "published"
        else path.read_bytes() == original
    )


@pytest.mark.parametrize("has_native_flag", [False, True])
def test_native_publication_confirmation_loss_never_reports_zero_effect(profiles, monkeypatch, has_native_flag):
    broker, manager, store, _, root, before, notified, _ = profiles
    publish = FileHandle.publish

    def lost(stream, name):
        publish(stream, name)
        if name == "instance.json":
            stream.is_published = has_native_flag
            raise OSError("publication confirmation unavailable")

    with monkeypatch.context() as change:
        change.setattr(FileHandle, "publish", lost)
        result = finish(broker, manager, patch(broker, before, {"alias": "Published"}))
    assert result["effect"] == "unknown" and not result["cleanup_pending"]
    assert json.loads((root / "versions/one/.ecl/instance.json").read_bytes())["alias"] == "Published"
    assert notified == [root] and not store.transactions.has_pending()


def test_notification_failure_retries_only_notification_without_repeating_write(profiles, monkeypatch):
    broker, manager, store, _, root, before, notified, _ = profiles
    with monkeypatch.context() as change:
        change.setattr(
            store.transactions, "_on_changed", lambda *args: (_ for _ in ()).throw(RuntimeError("notification"))
        )
        result = finish(broker, manager, patch(broker, before, {"alias": "Published"}))
        assert result["effect"] == "committed" and result["cleanup_pending"]
        assert store.transactions.has_pending(root, "one")
    path = root / "versions/one/.ecl/instance.json"
    # 用外部后来版本证明恢复通知不会再写业务。
    path.write_text('{"alias":"Later"}', encoding="utf-8")
    assert broker.operations.is_idle()
    assert json.loads(path.read_bytes())["alias"] == "Later" and notified == [root]


def test_management_permission_view_exposes_only_runtime_root_refs(profiles):
    broker, _, _, service, _, _, _, _ = profiles
    view = service.permissions("writer")
    persistent = next(
        item["selected_resource_refs"] for item in view["items"] if item["capability"] == "instances.manage"
    )
    public = call(broker, "permissions.list")
    management = next(item for item in public["items"] if item["capability"] == "instances.manage")
    root_ref = call(broker, "resources.list", {"capability": "instances.manage"})["items"][0]["root_ref"]
    assert management["resource_refs"] == [root_ref] and management["resource_refs"] != persistent


def test_profile_metadata_handle_keeps_reservation_until_actual_backup_disappearance(profiles):
    import ctypes
    from ctypes import wintypes

    broker, manager, store, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
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
    handle = create(str(path), 0x100080, 7, None, 3, 0x200000, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        result = finish(broker, manager, patch(broker, before, {"alias": "Published"}))
        assert result["effect"] == "committed" and result["cleanup_pending"]
        assert store.transactions.has_pending(root, "one")
        with pytest.raises(InstanceReadError):
            store.patch_profile(root, "one", {"alias": "Desktop"})
        assert json.loads(path.read_bytes())["alias"] == "Published"
    finally:
        close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close.argtypes = [wintypes.HANDLE]
        assert close(handle)
    assert broker.operations.is_idle()
    assert not store.transactions.has_pending()
    assert json.loads(path.read_bytes())["alias"] == "Published"


def test_recovery_rechecks_foreign_version_and_failed_choice_has_stable_receipt(profiles, monkeypatch):
    broker, manager, store, _, root, before, _, _ = profiles
    path = root / "versions/one/.ecl/instance.json"
    original = path.read_bytes()
    publish = FileHandle.publish

    def compete(stream, name):
        if name == "instance.json":
            path.write_text('{"alias":"External"}', encoding="utf-8")
        return publish(stream, name)

    with monkeypatch.context() as change:
        change.setattr(FileHandle, "publish", compete)
        result = finish(broker, manager, patch(broker, before, {"alias": "New"}))
    assert result["cleanup_pending"]
    row = store.transactions.page()["items"][0]
    gate = Event()
    blocker = manager._executor.submit(gate.wait, 5)
    arguments = (row["recovery_ref"], row["revision"], row["etag"], "save_as", "saved", manager)
    try:
        receipt = store.transactions.resolve(*arguments)
        path.write_text('{"alias":"Changed"}', encoding="utf-8")
    finally:
        gate.set()
        blocker.result(5)
    manager._operations[receipt["operationId"]].future.result(5)
    assert manager.get(receipt["operationId"])["status"] == "failed"
    assert store.transactions.resolve(*arguments) == receipt
    refreshed = store.transactions.page()["items"][0]
    assert refreshed["revision"] > row["revision"]
    retried = store.transactions.resolve(
        refreshed["recovery_ref"], refreshed["revision"], refreshed["etag"], "save_as", "saved", manager
    )
    manager._operations[retried["operationId"]].future.result(5)
    assert manager.get(retried["operationId"])["status"] == "completed"
    assert path.read_text(encoding="utf-8") == '{"alias":"Changed"}'
    assert (path.parent / "recovered-profile-saved.json").read_bytes() == original
