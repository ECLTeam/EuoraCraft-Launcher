from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.models import FieldSpec, VuePageDefinition
from ECL.plugins.worker_service import PluginWorkerService
from tests.test_plugin_panels import panel_setup  # noqa: F401 - pytest fixture
from tests.test_plugin_panels import request as send


def test_structured_command_preserves_types_and_bounds() -> None:
    field = FieldSpec(name="result", type="json", max_length=16384)
    assert field.accepts({"players": [{"name": "测试", "host": True}], "port": 25565, "error": None})
    assert not field.accepts({"name": "测" * 6000})
    assert not field.accepts({"value": float("inf")})
    assert not field.accepts({"value": Path("private")})
    nested: dict = {}
    for _index in range(18):
        nested = {"next": nested}
    assert not field.accepts(nested)


def test_query_channel_rejects_actions_and_preserves_registered_query(request) -> None:
    broker, _payload = request.getfixturevalue("panel_setup")
    common = {"input_fields": [], "output_fields": [], "callback_ref": "a" * 32}
    send(broker, "commands.register", {**common, "name": "status", "is_query": True})
    send(broker, "commands.register", {**common, "name": "start"})
    assert broker.call_query("status", {}) == {}
    with pytest.raises(ProtocolError, match="不是已登记查询"):
        broker.call_query("start", {})


@pytest.mark.parametrize("path", ["//other", "/../game", "/page?x=1", "/:path", "https://example.com", "page"])
def test_page_rejects_routes_outside_namespace(path: str) -> None:
    with pytest.raises(ValidationError):
        VuePageDefinition(path=path, title="Test", component_name="Page", template="<div />")


@pytest.mark.parametrize("allowed", [True, False])
def test_real_worker_page_requires_grant_and_clears_on_exit(tmp_path: Path, allowed: bool) -> None:
    received: list[tuple[str, dict]] = []
    service = PluginWorkerService(tmp_path / "data", slot_sink=lambda name, payload: received.append((name, payload)))
    code = tmp_path / "plugin"
    code.mkdir()
    metadata = {
        "name": "page-test",
        "version": "1.0.0",
        "api_version": 2,
        "entry_point": "main:Plugin",
        "permissions": [{"capability": "ui.inject", "required": False}],
    }
    (code / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    (code / "main.py").write_text(
        "from ECL.plugin_api import Plugin as Base\n"
        "class Plugin(Base):\n"
        "    def on_load(self):\n"
        '        self.define_vue_page("/room", "联机", "Room", "<div>Room</div>")\n',
        encoding="utf-8",
    )
    service.register(metadata, code, "directory")
    view = service.permissions(metadata["name"])
    service.update_permissions(
        metadata["name"],
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="ui.inject", decision="allow" if allowed else "deny"),),
        PermissionManagementCaller.main_webview,
    )
    try:
        if allowed:
            service.enable(metadata["name"])
            assert received[0][1]["kind"] == "page"
            assert received[0][1]["path"] == "/room"
        else:
            with pytest.raises(ProtocolError):
                service.enable(metadata["name"])
            assert all(payload["kind"] == "clear" for _, payload in received)
    finally:
        service.close()
    assert received[-1][1] == {"kind": "clear"}


def test_page_defaults_to_top_route_without_nav() -> None:
    page = VuePageDefinition(path="/room", title="联机", component_name="Room", template="<div />")

    dumped = page.model_dump(mode="json")
    assert dumped["container"] == "top"
    assert dumped["nav"] is None


def test_host_container_page_requires_nav_and_rejects_top_nav() -> None:
    with pytest.raises(ValidationError):
        VuePageDefinition(path="/room", title="联机", container="more", component_name="Room", template="<div />")
    with pytest.raises(ValidationError):
        VuePageDefinition(
            path="/room",
            title="联机",
            component_name="Room",
            template="<div />",
            nav={"path": "room", "label_key": "more.nav.room", "icon": "wifi"},
        )


@pytest.mark.parametrize(
    "nav",
    [
        {"path": "plugins", "label_key": "more.nav.room", "icon": "wifi"},
        {"path": "../room", "label_key": "more.nav.room", "icon": "wifi"},
        {"path": "room", "label_key": "settings.title", "icon": "wifi"},
        {"path": "room", "label_key": "more.nav.room", "icon": "wifi/evil"},
        {"path": "room", "label_key": "more.nav.room", "icon": "wifi", "order": 999},
    ],
)
def test_host_container_nav_rejects_out_of_bounds(nav: dict[str, str | int]) -> None:
    with pytest.raises(ValidationError):
        VuePageDefinition(
            path="/room",
            title="联机",
            container="more",
            component_name="Room",
            template="<div />",
            nav=nav,
        )


def test_host_container_page_keeps_nav_declaration() -> None:
    page = VuePageDefinition(
        path="/room",
        title="联机",
        container="more",
        component_name="Room",
        template="<div />",
        nav={"path": "room", "label_key": "more.nav.room", "icon": "wifi", "order": -10},
    )

    dumped = page.model_dump(mode="json")
    assert dumped["container"] == "more"
    assert dumped["nav"] == {
        "path": "room",
        "label_key": "more.nav.room",
        "icon": "wifi",
        "order": -10,
    }
