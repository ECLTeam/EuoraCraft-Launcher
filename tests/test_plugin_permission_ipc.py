from __future__ import annotations

import sys
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


@pytest.mark.parametrize("label", [None, "plugin-v2-forged", "unknown"])
async def test_resource_candidates_require_native_registered_main(api, label):
    window = SimpleNamespace(label=lambda: label) if label else None
    response = await api.plugin_permission_resources(
        {
            "installation_id": "a" * 32,
            "content_digest": "b" * 64,
            "capability": "instances.read",
            "caller": "main",
            "window_label": "main",
        },
        window,
    )
    assert response["errorCode"] == "PLUGIN_MANAGEMENT_CALLER_DENIED"


@pytest.mark.parametrize("method", ["plugin_file_recoveries_list", "plugin_file_recovery_resolve"])
@pytest.mark.parametrize("window", [None, SimpleNamespace(label=lambda: "plugin-v2-forged")])
async def test_file_recovery_requires_native_registered_main_even_for_queries(api, method, window):
    response = await getattr(api, method)({"caller": "main", "window_label": "main"}, window)
    assert response["errorCode"] == "PLUGIN_MANAGEMENT_CALLER_DENIED"


@pytest.mark.skipif(sys.platform != "win32", reason="原生私有文件恢复仅支持Windows")
async def test_main_file_recovery_does_not_require_an_installed_or_enabled_plugin(api):
    window = SimpleNamespace(label=lambda: "main")
    response = await api.plugin_file_recoveries_list({}, window)
    assert response["success"]
    assert response["data"] == {"items": [], "next_cursor": None}
