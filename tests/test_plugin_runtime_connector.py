from __future__ import annotations

import asyncio
import json
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ECL.foundation import EventBus
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.connector import ConnectorProtocolRequest
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantError, PluginGrantStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.services.connector import ConnectorRoomError, ConnectorService
from ECL.services.operations import OperationManager
from tests.test_plugin_host_files import call


def wait_for(predicate):
    end = monotonic() + 8
    while monotonic() < end:
        if predicate():
            return
        sleep(0.02)
    pytest.fail("联机会话未在截止前达到预期状态")


@pytest.fixture
def rooms(tmp_path, monkeypatch):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("pass", encoding="utf-8")
    binding = PluginContentBinding.capture(
        code, (PermissionDeclaration(capability="connector.manage"),), "windows-x86_64-cp311"
    )
    store = PluginGrantStore(tmp_path / "data", implemented_capabilities=PluginHostBroker.implemented_capabilities)
    session = store.open_session(store.new_installation_id(), binding)
    store.update(
        session,
        (PermissionDecision(capability="connector.manage", decision="allow"),),
        expected_revision=0,
        content_digest=binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    manager = OperationManager(tmp_path / "data", EventBus(), max_workers=2)
    service = ConnectorService()
    calls = []

    def host(port):
        service._mode = "host"
        service._room_code = "private-demo-room"
        service._mc_port = port
        calls.append(port)

    monkeypatch.setattr(service, "_host_port", host)
    callbacks = []

    def invoke(method, params, timeout):
        callbacks.append(params)
        return {"ok": True, "data": {"value": params["args"].get("payload")}}

    broker = PluginHostBroker(
        session,
        tmp_path / "data",
        invoke,
        run_state=lambda: "enabled",
        operations=manager,
        connector=service,
        plugin_name="room-demo",
    )
    yield SimpleNamespace(
        service=service, broker=broker, store=store, session=session, manager=manager, calls=calls, callbacks=callbacks
    )
    broker.close()
    manager.close()
    service.close()
    session.close()


def open_room(state):
    result = call(state.broker, "connector.open", {"port": 25565})
    wait_for(lambda: state.service._mode == "host")
    return result


def test_room_task_deduplication_leave_confirmation_and_new_user_room(rooms):
    state = rooms
    result = call(state.broker, "connector.open", {"port": 25565}, "open-room")
    assert call(state.broker, "connector.open", {"port": 25565}, "open-room") == result
    wait_for(lambda: state.service._mode == "host")
    assert state.calls == [25565]
    assert call(state.broker, "operations.get", {"operation_ref": result["operation_ref"]})["state"] == "running"
    view = call(state.broker, "connector.get", {"session_ref": result["session_ref"]})
    assert view["room_code"] == "private-demo-room" and "players" not in view and "nodes" not in view
    token = state.broker.connector._rooms[result["session_ref"]].token
    response = call(state.broker, "connector.leave", {"session_ref": result["session_ref"]})
    assert response["requested"] is True
    wait_for(lambda: state.service._mode == "idle")
    wait_for(lambda: state.manager.owner_idle(token.origin))
    state.service.host_port(25566)
    assert state.service.leave_plugin_room(token) is True
    assert state.service._mode == "host" and state.service._mc_port == 25566


def test_active_user_room_and_foreign_references_are_rejected(rooms):
    state = rooms
    state.service.host_port(25566)
    with pytest.raises(ProtocolError) as exc:
        call(state.broker, "connector.open", {"port": 25565})
    assert exc.value.error_code == "CONNECTOR_ROOM_BUSY"
    assert state.calls == [25566]
    with pytest.raises(ProtocolError):
        call(state.broker, "connector.get", {"session_ref": uuid4().hex})
    assert state.service._mc_port == 25566


def test_room_is_visible_and_cancellable_from_trusted_task_service(rooms):
    state = rooms
    result = open_room(state)
    task = state.broker.operations._references[result["operation_ref"]]
    view = state.manager.get(task.operation_id)
    assert view["kind"] == "plugin_connector_room"
    assert "plugin_connector_room" in state.manager.visible_plugin_kinds
    assert state.manager.cancel(task.operation_id)
    wait_for(lambda: state.service._mode == "idle")
    wait_for(lambda: state.manager.get(task.operation_id)["status"] == "cancelled")


def test_room_protocol_connection_failure_has_stable_error(rooms, monkeypatch):
    state = rooms
    result = open_room(state)
    token = state.broker.connector._rooms[result["session_ref"]].token
    state.service._mode = "guest"

    def disconnected(*_):
        raise ConnectionResetError("private network details")

    monkeypatch.setattr(state.service, "_send_extension_request", disconnected)
    with pytest.raises(ConnectorRoomError) as error:
        state.service.request_plugin_room(token, "demo:echo", b"{}")
    assert error.value.error_code == "PLUGIN_CONNECTOR_FAILED"
    assert "private" not in str(error.value)


def test_room_cleanup_failure_preserves_owner_handles_and_can_retry(rooms):
    state = rooms
    result = open_room(state)
    token = state.broker.connector._rooms[result["session_ref"]].token
    stops = []

    def stop():
        stops.append(True)
        if len(stops) == 1:
            raise RuntimeError("temporary stop failure")

    node = SimpleNamespace(stop=stop)
    state.service._easy_tier_node = node
    with pytest.raises(ConnectorRoomError):
        state.service.leave_plugin_room(token)
    assert state.service.owns_plugin_room(token)
    assert state.service._easy_tier_node is node
    assert state.service.plugin_room_status(token).cleanup_pending is True
    assert state.service.leave_plugin_room(token) is True
    assert not state.service.owns_plugin_room(token) and len(stops) == 2


def test_revoke_during_start_never_reports_ready_and_cleans_actual_room(rooms, monkeypatch):
    state = rooms
    entered, release = Event(), Event()

    def blocked_host(port):
        entered.set()
        assert release.wait(5)
        state.service._mode = "host"
        state.service._mc_port = port

    monkeypatch.setattr(state.service, "_host_port", blocked_host)
    result = call(state.broker, "connector.open", {"port": 25565})
    assert entered.wait(5)
    token = state.broker.connector._rooms[result["session_ref"]].token
    state.store.update(
        state.session,
        (PermissionDecision(capability="connector.manage", decision="deny"),),
        expected_revision=1,
        content_digest=state.session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )
    state.broker.refresh_permissions()
    release.set()
    wait_for(lambda: state.manager.owner_idle(token.origin))
    assert state.service._mode == "idle" and state.service._mc_port is None
    with pytest.raises((ProtocolError, PluginGrantError)):
        call(state.broker, "connector.get", {"session_ref": result["session_ref"]})


def test_protocol_is_scoped_json_only_and_old_callback_cannot_enter_new_room(rooms):
    state = rooms
    registered = call(state.broker, "connector.protocols.register", {"name": "echo", "callback_ref": uuid4().hex})
    protocol = registered["protocol"]
    assert state.service.extensions.protocol_names() == []
    state.service.host_port(25566)
    assert state.service.extensions.protocol_names() == []
    state.service.leave()
    result = open_room(state)
    assert state.service.extensions.protocol_names() == [protocol]
    request = ConnectorProtocolRequest(
        protocol, json.dumps({"hello": "world"}).encode(), "PRIVATE-MACHINE", {"private": "secret"}
    )
    status, response = asyncio.run(state.service.extensions.dispatch(request))
    assert status == 0 and json.loads(response) == {"hello": "world"}
    assert state.callbacks[-1]["args"] == {"payload": {"hello": "world"}}
    status, _ = asyncio.run(state.service.extensions.dispatch(ConnectorProtocolRequest(protocol, b"x" * 16385)))
    assert status == 255
    call(state.broker, "connector.status.set", {"session_ref": result["session_ref"], "text": "Demo status"})
    assert state.service.get_status()["pluginExtension"] == {"name": "room-demo", "text": "Demo status"}
    call(state.broker, "connector.leave", {"session_ref": result["session_ref"]})
    wait_for(lambda: state.service._mode == "idle")
    assert state.service.extensions.protocol_names() == []
    assert "pluginExtension" not in state.service.get_status()
