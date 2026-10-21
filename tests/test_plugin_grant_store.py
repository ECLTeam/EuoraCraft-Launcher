from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from plugin_wheel_helpers import make_package
from pydantic import ValidationError

from ECL.plugins.environment_pool import _exclusive_lock
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantError, PluginGrantStore
from ECL.plugins.package_preparation import PluginPackagePreparer


@pytest.fixture
def setup_grants(tmp_path: Path):
    code_path = tmp_path / "code"
    code_path.mkdir()
    (code_path / "main.py").write_text("value = 1", encoding="utf-8")
    declarations = (
        PermissionDeclaration(capability="settings.read", required=True),
        # 通用范围集合测试使用自定义测试能力；文件能力现仅允许固定私有空间。
        PermissionDeclaration(capability="test.resources", resource_refs=("root-a", "root-b")),
        PermissionDeclaration(capability="ui.custom.execute"),
    )
    binding = PluginContentBinding.capture(code_path, declarations, "windows-x86_64-cp311")
    store = PluginGrantStore(tmp_path / "data", implemented_capabilities=frozenset({"settings.read", "test.resources"}))
    session = store.open_session(store.new_installation_id(), binding)
    return store, session, binding


def grant(store, session, **kwargs):
    decisions = kwargs.pop("decisions", (PermissionDecision(capability="settings.read", decision="allow"),))
    view = session.view()
    return store.update(
        session,
        decisions,
        expected_revision=kwargs.pop("expected_revision", view.revision),
        content_digest=kwargs.pop("content_digest", view.content_digest),
        caller=kwargs.pop("caller", PermissionManagementCaller.main_webview),
        **kwargs,
    )


def record_file(store, session) -> Path:
    return store._record_path(session._state)


def test_declarations_never_grant_permission(setup_grants) -> None:
    store, session, _binding = setup_grants
    assert not session.view().required_satisfied
    assert all(item.decision == "deny" for item in session.view().items)
    with pytest.raises(PluginGrantError, match="必要授权"):
        session.authorize("settings.read")
    assert not (store.data_path / "plugin_grants").exists()


def test_permanent_grants_are_bound_to_installation_and_content(setup_grants) -> None:
    store, session, binding = setup_grants
    view = grant(store, session)
    assert view.revision == 1 and view.required_satisfied
    session.authorize("settings.read")
    reopened = PluginGrantStore(store.data_path, implemented_capabilities=store.implemented_capabilities)
    same_install = reopened.open_session(view.installation_id, binding)
    assert same_install.view().required_satisfied
    reinstall = reopened.open_session(reopened.new_installation_id(), binding)
    assert not reinstall.view().required_satisfied
    changed_permissions = PluginContentBinding.capture(
        binding.snapshot.root_path,
        (*binding.declarations, PermissionDeclaration(capability="network.http")),
        binding.target_tag,
    )
    new_content = reopened.open_session(view.installation_id, changed_permissions)
    assert not new_content.view().required_satisfied


def test_session_grant_does_not_survive_new_worker_or_host(setup_grants) -> None:
    store, session, binding = setup_grants
    decision = PermissionDecision(capability="settings.read", decision="allow", lifetime="session")
    grant(store, session, decisions=(decision,))
    assert session.view().required_satisfied
    assert json.loads(record_file(store, session).read_text(encoding="utf-8"))["grants"] == []
    assert not store.open_session(session.view().installation_id, binding).view().required_satisfied
    restarted = PluginGrantStore(store.data_path, implemented_capabilities=store.implemented_capabilities)
    assert not restarted.open_session(session.view().installation_id, binding).view().required_satisfied
    assert session.view().required_satisfied
    session.close()
    session.close()
    assert session.view().error_code == "PLUGIN_SESSION_EXPIRED"


def test_switching_permanent_to_session_removes_persisted_allow(setup_grants) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    grant(
        store,
        session,
        decisions=(PermissionDecision(capability="settings.read", decision="allow", lifetime="session"),),
    )
    session.close()
    assert not store.open_session(session.view().installation_id, binding).view().required_satisfied


@pytest.mark.parametrize(
    "caller",
    [
        PermissionManagementCaller.worker,
        PermissionManagementCaller.dev_channel,
        PermissionManagementCaller.unknown,
        "main-webview",
    ],
)
def test_only_trusted_main_context_can_update(setup_grants, caller) -> None:
    store, session, _binding = setup_grants
    with pytest.raises(PluginGrantError) as error:
        grant(store, session, caller=caller)
    assert error.value.error_code == "PLUGIN_MANAGEMENT_CALLER_DENIED"
    assert not session.view().required_satisfied


def test_resource_grants_are_exact_subsets_and_required_scopes_are_complete(setup_grants) -> None:
    store, session, _binding = setup_grants
    decisions = (
        PermissionDecision(capability="settings.read", decision="allow"),
        PermissionDecision(capability="test.resources", decision="allow", resource_refs=("root-a",)),
    )
    grant(store, session, decisions=decisions)
    session.authorize("test.resources", ("root-a",))
    for references in ((), ("root-a-subdir",), ("root-b",), ("root-a", "root-a")):
        with pytest.raises(PluginGrantError) as error:
            session.authorize("test.resources", references)
        assert error.value.error_code == "PLUGIN_RESOURCE_DENIED"
    with pytest.raises(PluginGrantError):
        grant(
            store,
            session,
            decisions=(PermissionDecision(capability="test.resources", decision="allow", resource_refs=("other",)),),
        )


def test_unsupported_custom_frontend_stays_denied_even_after_approval(setup_grants) -> None:
    store, session, _binding = setup_grants
    grant(
        store,
        session,
        decisions=(
            PermissionDecision(capability="settings.read", decision="allow"),
            PermissionDecision(capability="ui.custom.execute", decision="allow"),
        ),
    )
    with pytest.raises(PluginGrantError) as error:
        session.authorize("ui.custom.execute")
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"


def test_every_epoch_change_invalidates_existing_leases(setup_grants) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    lease = session.authorize("settings.read")
    session.check(lease)
    other = store.open_session(session.view().installation_id, binding)
    with pytest.raises(PluginGrantError):
        other.check(lease)
    with pytest.raises(PluginGrantError):
        session.check(replace(lease, installation_id="f" * 32))
    grant(
        store,
        session,
        decisions=(PermissionDecision(capability="test.resources", decision="allow", resource_refs=("root-a",)),),
    )
    with pytest.raises(PluginGrantError) as error:
        session.check(lease)
    assert error.value.error_code == "PLUGIN_GRANT_EXPIRED"
    lease = session.authorize("settings.read")
    grant(
        store, session, decisions=(PermissionDecision(capability="settings.read", decision="deny", lifetime="session"),)
    )
    with pytest.raises(PluginGrantError):
        session.check(lease)
    assert not other.view().required_satisfied
    assert not store.open_session(session.view().installation_id, binding).view().required_satisfied


def test_atomic_failure_preserves_disk_but_blocks_all_matching_runs(setup_grants, monkeypatch) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    target_path = record_file(store, session)
    original = target_path.read_bytes()
    other = store.open_session(session.view().installation_id, binding)
    lease = other.authorize("settings.read")

    def failed_write(*_args):
        assert not session.view().required_satisfied
        assert not other.view().required_satisfied
        raise OSError("disk unavailable")

    monkeypatch.setattr("ECL.plugins.grant_store.atomic_write_text", failed_write)
    with pytest.raises(PluginGrantError) as error:
        grant(store, session, decisions=(PermissionDecision(capability="settings.read", decision="deny"),))
    assert error.value.error_code == "PLUGIN_GRANT_PERSIST_FAILED"
    assert target_path.read_bytes() == original
    assert session.view().error_code == "PLUGIN_GRANT_PERSIST_FAILED"
    with pytest.raises(PluginGrantError):
        other.check(lease)


@pytest.mark.parametrize("raw", [b"not json", b"{}", b'{"schema_version":2,"schema_version":2}', b"[]", b"\xff"])
def test_corrupted_files_deny_without_silently_overwriting(setup_grants, raw) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    target_path = record_file(store, session)
    target_path.write_bytes(raw)
    assert session.view().error_code == "PLUGIN_GRANT_STORE_CORRUPTED"
    restarted = PluginGrantStore(store.data_path, implemented_capabilities=store.implemented_capabilities)
    blocked = restarted.open_session(session.view().installation_id, binding)
    assert not blocked.view().required_satisfied
    with pytest.raises(PluginGrantError):
        grant(restarted, blocked)
    assert target_path.read_bytes() == raw


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 1},
        {"revision": True},
        {"installation_id": "f" * 32},
        {"content_digest": "f" * 64},
        {"unknown": "allow"},
        {"grants": [{"capability": "settings.read", "decision": "allow", "lifetime": "session"}]},
    ],
)
def test_untrusted_record_shapes_are_rejected(setup_grants, change) -> None:
    store, session, _binding = setup_grants
    grant(store, session)
    target_path = record_file(store, session)
    payload = json.loads(target_path.read_bytes())
    payload.update(change)
    target_path.write_text(json.dumps(payload), encoding="utf-8")
    assert session.view().error_code == "PLUGIN_GRANT_STORE_CORRUPTED"


def test_revision_conflict_and_external_revoke_do_not_get_overwritten(setup_grants) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    lease = session.authorize("settings.read")
    independent = PluginGrantStore(store.data_path, implemented_capabilities=store.implemented_capabilities)
    other = independent.open_session(session.view().installation_id, binding)
    grant(independent, other, decisions=(PermissionDecision(capability="settings.read", decision="deny"),))
    with pytest.raises(PluginGrantError) as error:
        grant(store, session, expected_revision=1)
    assert error.value.error_code == "PLUGIN_GRANT_REVISION_CONFLICT"
    with pytest.raises(PluginGrantError):
        session.check(lease)
    assert not session.view().required_satisfied


def test_runtime_rejects_record_revision_rollback(setup_grants) -> None:
    store, session, _binding = setup_grants
    grant(store, session)
    target_path = record_file(store, session)
    old = target_path.read_bytes()
    grant(store, session, decisions=(PermissionDecision(capability="settings.read", decision="deny"),))
    target_path.write_bytes(old)
    assert session.view().error_code == "PLUGIN_GRANT_STORE_CORRUPTED"
    assert not session.view().required_satisfied


def test_expiry_invalidates_once_and_never_restores_on_clock_rollback(setup_grants) -> None:
    _store, session, binding = setup_grants
    clock_value = [100.0]
    store = PluginGrantStore(
        _store.data_path, implemented_capabilities=_store.implemented_capabilities, clock=lambda: clock_value[0]
    )
    session = store.open_session(session.view().installation_id, binding)
    grant(
        store, session, decisions=(PermissionDecision(capability="settings.read", decision="allow", expires_at=150.0),)
    )
    lease = session.authorize("settings.read")
    clock_value[0] = 150.0
    view = session.view()
    assert not view.required_satisfied
    assert session.view().grant_epoch == view.grant_epoch
    with pytest.raises(PluginGrantError):
        session.check(lease)
    clock_value[0] = 120.0
    assert not session.view().required_satisfied


def test_content_lock_permissions_and_abi_all_contribute_to_identity(setup_grants) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    lease = session.authorize("settings.read")
    changed_target = PluginContentBinding.capture(
        binding.snapshot.root_path, binding.declarations, "linux-x86_64-cp311"
    )
    assert binding.content_digest != changed_target.content_digest
    reversed_declarations = PluginContentBinding.capture(
        binding.snapshot.root_path, binding.declarations[::-1], binding.target_tag
    )
    assert binding.content_digest == reversed_declarations.content_digest
    (binding.snapshot.root_path / "main.py").write_text("value = 2", encoding="utf-8")
    with pytest.raises(PluginGrantError) as error:
        session.check(lease)
    assert error.value.error_code == "PLUGIN_CONTENT_CHANGED"
    assert not session.view().required_satisfied
    new = PluginContentBinding.capture(binding.snapshot.root_path, binding.declarations, binding.target_tag)
    assert not store.open_session(session.view().installation_id, new).view().required_satisfied


def test_cross_process_file_lock_is_bounded_and_leaves_saved_grants_untouched(setup_grants) -> None:
    store, session, binding = setup_grants
    grant(store, session)
    target_path = record_file(store, session)
    original = target_path.read_bytes()
    script = """
import sys
from pathlib import Path
from ECL.plugins.grant_models import PermissionDeclaration, PermissionDecision, PermissionManagementCaller, PluginContentBinding
from ECL.plugins.grant_store import PluginGrantStore, PluginGrantError
binding = PluginContentBinding.capture(Path(sys.argv[1]), (PermissionDeclaration(capability='settings.read',required=True),PermissionDeclaration(capability='test.resources',resource_refs=('root-a','root-b')),PermissionDeclaration(capability='ui.custom.execute')), 'windows-x86_64-cp311')
store = PluginGrantStore(Path(sys.argv[2]), implemented_capabilities=frozenset({'settings.read','test.resources'}))
session = store.open_session(sys.argv[3], binding)
try:
    store.update(session,(PermissionDecision(capability='settings.read',decision='deny'),),expected_revision=1,content_digest=binding.content_digest,caller=PermissionManagementCaller.main_webview)
except PluginGrantError as error:
    print(error.error_code)
    print(session.view().required_satisfied)
else:
    raise RuntimeError('lock unexpectedly bypassed')
"""
    with _exclusive_lock(target_path.with_suffix(".lock")):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(binding.snapshot.root_path),
                str(store.data_path),
                session.view().installation_id,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["PLUGIN_GRANT_PERSIST_FAILED", "False"]
    assert target_path.read_bytes() == original


@pytest.mark.parametrize(
    "fields", [{"required": 1}, {"resource_refs": ["*"]}, {"resource_refs": ["root", "root"]}, {"unknown": True}]
)
def test_declaration_validation_is_strict(fields) -> None:
    with pytest.raises(ValidationError):
        PermissionDeclaration(capability="settings.read", **fields)


def test_same_revision_cannot_silently_replace_permission_content(setup_grants) -> None:
    store, session, _binding = setup_grants
    grant(store, session)
    target_path = record_file(store, session)
    payload = json.loads(target_path.read_bytes())
    payload["grants"][0]["decision"] = "deny"
    target_path.write_text(json.dumps(payload), encoding="utf-8")
    assert session.view().error_code == "PLUGIN_GRANT_STORE_CORRUPTED"


def test_full_lock_bytes_and_archive_preparation_are_bound(tmp_path: Path) -> None:
    archive_path = make_package(tmp_path, "grant-package", "1.0.0", code="value = 1\n")
    prepared = PluginPackagePreparer(tmp_path / "data").prepare(archive_path, confirm_unverified_source=True)
    declarations = (PermissionDeclaration(capability="settings.read"),)
    binding = PluginContentBinding.from_prepared(prepared, declarations)
    binding.verify()
    assert binding.lock_file is None
    code_path = tmp_path / "lock-code"
    code_path.mkdir()
    (code_path / "main.py").write_text("value = 1", encoding="utf-8")
    lock_file = code_path / "requirements.lock"
    lock_file.write_bytes(b"# original full lock\n")
    original = PluginContentBinding.capture(code_path, declarations, "windows-x86_64-cp311", lock_file)
    lock_file.write_bytes(b"# changed comment still requires review\n")
    changed = PluginContentBinding.capture(code_path, declarations, "windows-x86_64-cp311", lock_file)
    assert original.lock_sha256 != changed.lock_sha256
    assert original.content_digest != changed.content_digest
    with pytest.raises(ValueError):
        original.verify()


def test_symlinked_grant_parent_fails_closed(setup_grants, tmp_path: Path) -> None:
    store, session, _binding = setup_grants
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    store.data_path.mkdir()
    try:
        (store.data_path / "plugin_grants").symlink_to(outside_path, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 用户不能创建符号链接；POSIX CI 验证真实路径分支")
    assert session.view().error_code == "PLUGIN_GRANT_STORE_CORRUPTED"
    assert list(outside_path.iterdir()) == []


def test_failed_write_can_only_be_recovered_by_explicit_review(setup_grants, monkeypatch) -> None:
    store, session, _binding = setup_grants
    grant(store, session)
    import ECL.plugins.grant_store as grant_module

    original_writer = grant_module.atomic_write_text

    def reject_write(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(grant_module, "atomic_write_text", reject_write)
    with pytest.raises(PluginGrantError):
        grant(store, session)
    monkeypatch.setattr(grant_module, "atomic_write_text", original_writer)
    assert not session.view().required_satisfied
    assert grant(store, session).required_satisfied


@pytest.mark.parametrize("wait_seconds", [0, -1, 121, float("nan"), float("inf")])
def test_file_lock_rejects_unbounded_wait(tmp_path: Path, wait_seconds: float) -> None:
    with pytest.raises(ValueError), _exclusive_lock(tmp_path / "lock", wait_seconds=wait_seconds):
        pytest.fail("invalid lock timeout was accepted")
