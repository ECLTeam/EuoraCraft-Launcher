from __future__ import annotations

import pytest

from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantError, PluginGrantStore


@pytest.fixture(params=["windows-x86_64-cp311", "linux-x86_64-cp311"])
def grants(tmp_path, request):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("value = 1", encoding="utf-8")
    declared = (
        PermissionDeclaration(capability="network.read", required=True, resource_refs=("https:example.com:443",)),
    )
    binding = PluginContentBinding.capture(source, declared, request.param)
    store = PluginGrantStore(tmp_path, implemented_capabilities=frozenset({"network.read"}))
    session = store.open_session(store.new_installation_id(), binding)
    store.update(
        session,
        (PermissionDecision(capability="network.read", decision="allow", resource_refs=("https:example.com:443",)),),
        expected_revision=0,
        content_digest=binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    yield store, session, source
    session.close()


def new_binding(session, source):
    execution = source.parent / "execution"
    execution.mkdir()
    (execution / "main.py").write_text("value = 2", encoding="utf-8")
    return PluginContentBinding.capture(execution, session.binding.declarations, session.binding.target_tag)


def test_development_allows_new_bytes_without_permanent_authorization(grants):
    store, session, source = grants
    token = store.begin_development(session)
    binding = new_binding(session, source)
    identity = session.view().installation_id
    normal = store.open_session(identity, binding)
    assert normal.view().revision == 0
    normal.close()
    development = store.open_session(identity, binding, development_token=token)
    try:
        assert (
            development.authorize("network.read", ("https:example.com:443",)).content_digest == binding.content_digest
        )
        assert not (store.data_path / "plugin_grants" / identity / f"{binding.content_digest}.json").exists()
        store.end_development(token)
        assert development.view().error_code == "PLUGIN_DEVELOPMENT_SESSION_EXPIRED"
    finally:
        development.close()


def test_changed_capabilities_abi_and_dependency_lock_are_not_inherited(grants):
    store, session, source = grants
    token = store.begin_development(session)
    binding = new_binding(session, source)
    from dataclasses import replace

    candidates = [
        replace(binding, declarations=()),
        replace(binding, target_tag="changed-cp999"),
        replace(binding, lock_sha256="f" * 64),
    ]
    for candidate in candidates:
        with pytest.raises(PluginGrantError) as error:
            store.open_session(session.view().installation_id, candidate, development_token=token)
        assert error.value.error_code == "PLUGIN_DEVELOPMENT_REVIEW_REQUIRED"
    store.end_development(token)


def test_revoking_original_record_invalidates_development_and_old_leases(grants):
    store, session, source = grants
    token = store.begin_development(session)
    development = store.open_session(
        session.view().installation_id, new_binding(session, source), development_token=token
    )
    lease = development.authorize("network.read", ("https:example.com:443",))
    store.update(
        session,
        (PermissionDecision(capability="network.read", decision="deny"),),
        expected_revision=1,
        content_digest=session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    with pytest.raises(PluginGrantError):
        development.check(lease)
    assert development.view().items[0].decision == "deny"
    development.close()
    store.end_development(token)
