from __future__ import annotations

import sys
from time import monotonic
from uuid import uuid4

import pytest
from plugin_network_helpers import MappedDialer, trusted_ssl_context

from ECL.foundation import EventBus
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.runtime.broker import PluginHostBroker
from ECL.plugins.runtime.file_storage import PluginFileStorage
from ECL.plugins.runtime.network_api import PluginNetworkService
from ECL.services.operations import OperationManager


def call(broker, method, params=None, key=None, seconds=5):
    return broker.handle(RpcRequest(method, params or {}, monotonic() + seconds, key or uuid4().hex))


def target(broker, name="download.bin"):
    root = call(broker, "resources.list", {"capability": "plugin.base"})["items"][0]["root_ref"]
    return {"root_ref": root, "relative_path": name}


def submit(broker, url, name="download.bin", key=None, **kwargs):
    return call(broker, "downloads.submit", dict(url=url, target=target(broker, name), overwrite=False, **kwargs), key)


def finish(broker, manager, receipt):
    reference = receipt["operation_ref"]
    identity = broker.operations._references[reference].operation_id
    future = manager._operations[identity].future
    if not future.cancelled():
        future.result(60)
    return call(broker, "operations.get", {"operation_ref": reference})


def decide(broker, store, capability, decision="deny"):
    return store.update(
        broker.session,
        (PermissionDecision(capability=capability, decision=decision),) if capability != "plugin.base" else (),
        expected_revision=broker.session.view().revision,
        content_digest=broker.session.binding.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


@pytest.fixture
def downloads(tmp_path):
    if sys.platform != "win32":
        pytest.skip("真实 Windows NTFS 下载事务")
    manager = OperationManager(tmp_path, EventBus(), max_workers=1)
    storage = PluginFileStorage(tmp_path)
    capabilities = frozenset({"network.read"})
    store = PluginGrantStore(tmp_path, implemented_capabilities=capabilities)
    brokers, services = [], []

    def create(references, *, allowed=None, resolver=None, dialer=None):
        code = tmp_path / f"code-{len(brokers)}"
        code.mkdir()
        (code / "main.py").write_text("value = 1", encoding="utf-8")
        binding = PluginContentBinding.capture(
            code,
            tuple(
                PermissionDeclaration(capability=c, resource_refs=tuple(references) if c == "network.read" else ())
                for c in sorted(capabilities)
            ),
            "windows-x86_64-cp311",
        )
        session = store.open_session(uuid4().hex, binding)
        store.update(
            session,
            tuple(
                PermissionDecision(
                    capability=c, decision="allow", resource_refs=tuple(references) if c == "network.read" else ()
                )
                for c in (capabilities if allowed is None else allowed)
            ),
            expected_revision=session.view().revision,
            content_digest=binding.content_digest,
            caller=PermissionManagementCaller.main_webview,
        )
        service = PluginNetworkService(
            resolver=resolver or (lambda *_: ("1.1.1.1",)),
            dialer=dialer or MappedDialer(),
            ssl_context=trusted_ssl_context(),
        )
        broker = PluginHostBroker(
            session,
            tmp_path,
            lambda *args: {},
            run_state=lambda: "enabled",
            operations=manager,
            file_storage=storage,
            network_service=service,
        )
        brokers.append(broker)
        services.append(service)
        return broker

    yield create, manager, storage, store
    for broker in brokers:
        broker.close()
    manager.close()
    for operation in tuple(manager._operations.values()):
        if operation.future is not None and not operation.future.cancelled():
            operation.future.result(60)
    for broker in brokers:
        assert broker.wait_idle(1)
        broker.session.close()
    for service in services:
        assert service.close()
    assert storage.close()
