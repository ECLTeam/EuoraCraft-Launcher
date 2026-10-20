from __future__ import annotations

from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

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


@pytest.fixture
def broker_setup(tmp_path: Path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "main.py").write_text("value = 1", encoding="utf-8")
    declarations = tuple(PermissionDeclaration(capability=c) for c in sorted(PluginHostBroker.implemented_capabilities))
    binding = PluginContentBinding.capture(root, declarations, "windows-x86_64-cp311")
    store = PluginGrantStore(tmp_path, implemented_capabilities=PluginHostBroker.implemented_capabilities)
    session = store.open_session(store.new_installation_id(), binding)
    store.update(
        session,
        tuple(PermissionDecision(capability=c, decision="allow") for c in PluginHostBroker.implemented_capabilities),
        expected_revision=0,
        content_digest=binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    calls = []

    def invoke(method, params, timeout):
        calls.append(params)
        return {"ok": True, "data": {"value": params["args"]}}

    broker = PluginHostBroker(session, tmp_path, invoke, run_state=lambda: "enabled")
    yield broker, store, session, calls
    broker.close()
    session.close()


def request(broker, method, params, key=None):
    return broker.handle(RpcRequest(method, params, monotonic() + 5, key or uuid4().hex))


def define_setting(broker):
    request(broker, "settings.define", {"definitions": [{"key": "greeting", "type": "string", "default": "hello"}]})


def test_settings_revision_duplicate_mutation_and_restart(broker_setup):
    broker, _store, _session, _calls = broker_setup
    define_setting(broker)
    assert request(broker, "settings.get", {"keys": ["greeting"]}) == {"values": {"greeting": "hello"}, "revision": 0}
    params = {"values": {"greeting": "world"}, "expected_revision": 0}
    assert request(broker, "settings.set", params, "same") == {"revision": 1}
    assert request(broker, "settings.set", params, "same") == {"revision": 1}
    with pytest.raises(ProtocolError) as error:
        request(broker, "settings.set", params)
    assert error.value.error_code == "PLUGIN_REVISION_CONFLICT"
    with pytest.raises(ProtocolError) as error:
        request(broker, "settings.set", {"values": {"greeting": "other"}, "expected_revision": 1}, "same")
    assert error.value.error_code == "PLUGIN_MUTATION_CONFLICT"
    assert broker.settings_view()["revision"] == 1
    assert request(broker, "mutations.get", {"mutation_key": "same"}) == {
        "state": "succeeded",
        "result": {"revision": 1},
        "error_code": None,
    }


def test_command_schema_reverse_callback_and_owner_rejection(broker_setup):
    broker, _store, _session, calls = broker_setup
    fields = [{"name": "value", "type": "string"}]
    result = request(
        broker,
        "commands.register",
        {"name": "echo", "callback_ref": uuid4().hex, "input_fields": fields, "output_fields": fields},
    )
    assert broker.call_command(result["command_ref"], {"value": "hello"}) == {"value": "hello"}
    assert len(calls) == 1
    with pytest.raises(ProtocolError):
        broker.call_command(uuid4().hex, {})
    with pytest.raises(ProtocolError):
        request(
            broker, "commands.call", {"command_ref": result["command_ref"], "args": {"value": "x"}, "owner": "other"}
        )
    with pytest.raises(ProtocolError):
        broker.call_command("echo", {"value": True})
    assert len(calls) == 1


def test_revocation_rejects_write_and_old_command_results(broker_setup):
    broker, store, session, _calls = broker_setup
    define_setting(broker)
    store.update(
        session,
        (PermissionDecision(capability="settings.write", decision="deny"),),
        expected_revision=1,
        content_digest=session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    with pytest.raises(ProtocolError) as error:
        request(broker, "settings.set", {"values": {"greeting": "blocked"}, "expected_revision": 0})
    assert error.value.error_code == "PLUGIN_PERMISSION_DENIED"
    assert broker.settings_view()["values"] == {"greeting": "hello"}


def test_storage_failure_keeps_original_settings_and_same_key_never_replays(broker_setup, monkeypatch):
    broker, _store, _session, _calls = broker_setup
    define_setting(broker)
    import ECL.plugins.runtime.broker as module

    def fail(*args):
        raise OSError("test failure")

    monkeypatch.setattr(module, "atomic_write_text", fail)
    for _ in range(2):
        with pytest.raises(ProtocolError) as error:
            request(broker, "settings.set", {"values": {"greeting": "x"}, "expected_revision": 0}, "failed")
        assert error.value.error_code == "PLUGIN_STORAGE_FAILED"
    assert broker.settings_view()["revision"] == 0


def test_unknown_method_and_host_events_are_not_forwarded(broker_setup):
    broker, *_ = broker_setup
    with pytest.raises(ProtocolError) as error:
        request(broker, "account_list", {})
    assert error.value.error_code == "PLUGIN_API_METHOD_NOT_FOUND"
    with pytest.raises(ProtocolError) as error:
        request(broker, "events.subscribe", {"event": "plugin:html_injected", "callback_ref": uuid4().hex})
    assert error.value.error_code == "PLUGIN_EVENT_NOT_EXPOSED"


def test_cached_receipt_cannot_deliver_result_after_permission_epoch_changes(broker_setup):
    broker, store, session, _calls = broker_setup
    define_setting(broker)
    params = {"values": {"greeting": "saved"}, "expected_revision": 0}
    assert request(broker, "settings.set", params, "before-revoke") == {"revision": 1}
    store.update(
        session,
        (PermissionDecision(capability="settings.write", decision="deny"),),
        expected_revision=1,
        content_digest=session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    with pytest.raises(ProtocolError) as error:
        request(broker, "settings.set", params, "before-revoke")
    assert error.value.error_code == "PLUGIN_PERMISSION_REVOKED"


def test_unsubscribed_queued_event_is_not_delivered(broker_setup):
    from threading import Event
    from time import sleep

    broker, _store, _session, _calls = broker_setup
    started, release = Event(), Event()
    delivered = []

    def invoke(method, params, timeout):
        delivered.append(params)
        started.set()
        release.wait(2)
        return {"ok": True, "data": {"value": {}}}

    broker._invoke = invoke
    define_setting(broker)
    subscription = request(broker, "events.subscribe", {"event": "settings.changed", "callback_ref": uuid4().hex})
    try:
        request(broker, "settings.set", {"values": {"greeting": "first"}, "expected_revision": 0})
        assert started.wait(2)
        request(broker, "settings.set", {"values": {"greeting": "second"}, "expected_revision": 1})
        request(broker, "events.unsubscribe", subscription)
        release.set()
        sleep(0.2)
        assert len(delivered) == 1
    finally:
        release.set()


def test_optional_registration_revocation_invalidates_existing_command(broker_setup):
    broker, store, session, calls = broker_setup
    fields = [{"name": "value", "type": "string"}]
    registration = request(
        broker,
        "commands.register",
        {"name": "echo", "callback_ref": uuid4().hex, "input_fields": fields, "output_fields": fields},
    )
    store.update(
        session,
        (PermissionDecision(capability="commands.register", decision="deny"),),
        expected_revision=1,
        content_digest=session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    with pytest.raises(ProtocolError):
        broker.call_command(registration["command_ref"], {"value": "not allowed"})
    assert calls == []
