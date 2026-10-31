from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantError, PluginGrantStore


def create_session(tmp_path: Path, declarations=()):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("value = 1", encoding="utf-8")
    store = PluginGrantStore(
        tmp_path / "data", implemented_capabilities=frozenset({"instances.read", "instances.manage", "network.read"})
    )
    store.update_resource_scope(frozenset({"a" * 32}))
    binding = PluginContentBinding.capture(code, declarations, "windows-x86_64-cp311")
    return store, store.open_session(store.new_installation_id(), binding)


def review(store, session, decisions=()):
    view = session.view()
    return store.update(
        session,
        decisions,
        expected_revision=view.revision,
        content_digest=view.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


@pytest.mark.parametrize(
    "capability",
    [
        "commands.register",
        "commands.execute",
        "settings.read",
        "settings.write",
        "events.subscribe",
        "filesystem.read",
        "filesystem.write",
        "filesystem.delete",
        "filesystem.replace",
        "instances.write",
        "instances.install",
        "instances.launch",
        "instances.stop",
        "game.catalog.read",
        "plugin.base",
        "unknown.permission",
    ],
)
def test_manifest_accepts_only_three_business_permissions(capability):
    with pytest.raises(ValidationError):
        PermissionDeclaration(capability=capability)


def test_basic_access_requires_review_and_dies_with_original_run(tmp_path):
    store, session = create_session(tmp_path)
    with pytest.raises(PluginGrantError):
        session.authorize("plugin.base")
    review(store, session)
    lease = session.authorize("plugin.base")
    assert session.view().items == ()
    session.check(lease)
    session.close()
    with pytest.raises(PluginGrantError):
        session.check(lease)


def test_management_satisfies_required_read_and_uses_one_original_lease(tmp_path):
    store, session = create_session(
        tmp_path,
        (
            PermissionDeclaration(capability="instances.read", required=True),
            PermissionDeclaration(capability="instances.manage"),
        ),
    )
    view = review(
        store,
        session,
        (PermissionDecision(capability="instances.manage", decision="allow", resource_refs=("a" * 32,)),),
    )
    assert view.required_satisfied
    lease = session.authorize("instances.read", ("a" * 32,))
    assert lease.capability == "instances.manage"
    review(
        store,
        session,
        (
            PermissionDecision(capability="instances.manage", decision="deny"),
            PermissionDecision(capability="instances.read", decision="allow", resource_refs=("a" * 32,)),
        ),
    )
    with pytest.raises(PluginGrantError):
        session.check(lease)
    assert session.authorize("instances.read", ("a" * 32,)).capability == "instances.read"
    with pytest.raises(PluginGrantError):
        session.authorize("instances.manage", ("a" * 32,))


def test_read_only_grant_never_allows_management(tmp_path):
    store, session = create_session(tmp_path, (PermissionDeclaration(capability="instances.read", required=True),))
    review(
        store, session, (PermissionDecision(capability="instances.read", decision="allow", resource_refs=("a" * 32,)),)
    )
    session.authorize("instances.read", ("a" * 32,))
    with pytest.raises(PluginGrantError):
        session.authorize("instances.manage", ("a" * 32,))


def test_management_cannot_grant_network_or_another_root(tmp_path):
    store, session = create_session(tmp_path, (PermissionDeclaration(capability="instances.manage", required=True),))
    review(
        store,
        session,
        (PermissionDecision(capability="instances.manage", decision="allow", resource_refs=("a" * 32,)),),
    )
    with pytest.raises(PluginGrantError):
        session.authorize("network.read", ("https:example.com:443",))
    with pytest.raises(PluginGrantError):
        session.authorize("instances.manage", ("b" * 32,))
