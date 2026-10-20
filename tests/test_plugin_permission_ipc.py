from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_plugin_worker_manager import make_plugin

from ECL.api.plugins import PluginHandlers
from ECL.plugins import PluginManager


@pytest.fixture
def api(tmp_path):
    make_plugin(tmp_path / "data/plugins/example", tmp_path / "marker")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    handler = PluginHandlers.__new__(PluginHandlers)
    handler.plugins = manager
    handler._window_metadata = {"main": {"windowType": "main"}}
    yield handler
    manager.close()


@pytest.mark.parametrize("label", [None, "plugin-v2-forged", "unknown"])
async def test_permission_management_rejects_missing_native_main_context(api, label):
    view = api.plugins.worker_service.permissions("example")
    window = SimpleNamespace(label=lambda: label) if label else None
    body = {"installation_id": view["installation_id"], "caller": "main", "window_label": "main"}
    response = await api.plugin_permissions_get(body, window)
    assert response["success"] is False
    assert response["errorCode"] == "PLUGIN_MANAGEMENT_CALLER_DENIED"
    assert api.plugins.worker_service.permissions("example")["revision"] == 0


async def test_registered_main_can_review_without_executing_entry(api):
    view = api.plugins.worker_service.permissions("example")
    window = SimpleNamespace(label=lambda: "main")
    response = await api.plugin_permissions_get({"installation_id": view["installation_id"]}, window)
    assert response["success"] is True
    response = await api.plugin_permissions_update(
        {
            "installation_id": view["installation_id"],
            "content_digest": view["content_digest"],
            "expected_revision": 0,
            "decisions": [],
        },
        window,
    )
    assert response["success"] is True
    assert response["data"]["persisted"] is True
    assert api.plugins.worker_service.runtime_info("example")["pid"] is None


async def test_main_label_without_host_registration_does_not_authorize(api):
    api._window_metadata.clear()
    response = await api.plugin_runtime_info({"installation_id": "a" * 32}, SimpleNamespace(label=lambda: "main"))
    assert response["errorCode"] == "PLUGIN_MANAGEMENT_CALLER_DENIED"
