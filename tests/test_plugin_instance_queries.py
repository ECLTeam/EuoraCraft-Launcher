from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from types import SimpleNamespace

import pytest

from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.runtime.resources import ResourcePages
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services.game import GameService
from ECL.services.game.instance_profiles import InstanceProfileStore
from ECL.services.game.version_stats import VersionStatsStore
from ECL.utils.instance_reading import InstanceReadError


def instance(root: Path, name: str, **profile):
    target = root / "versions" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.json").write_text(json.dumps({"id": name}), encoding="utf-8")
    if profile:
        (target / ".ecl").mkdir(exist_ok=True)
        (target / ".ecl/instance.json").write_text(json.dumps(profile), encoding="utf-8")
    return target


@pytest.fixture
def queries(tmp_path):
    service = GameService.__new__(GameService)
    service._lock = RLock()
    service._running_games = {}
    service._instance_profiles = InstanceProfileStore(tmp_path / "data", VersionStatsStore())
    return service


def tree(root: Path):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_query_has_no_game_data_writes_and_filters_private_fields(tmp_path, queries):
    root = tmp_path / "minecraft"
    instance(root, "1.21", alias="My game", favorite=True, tags=["A", "a"], path="secret", token="secret")
    before = tree(root)
    assert queries.query_instance_names(root, monotonic() + 5) == ("1.21",)
    summary = queries.query_instance_summary(root, "1.21", monotonic() + 5)
    value = summary.to_protocol("runtime-ref")
    assert value["alias"] == "My game"
    assert value["profile"] == {"favorite": True, "description": "", "tags": ["A"]}
    assert value["game_version"] == "1.21"
    assert "secret" not in json.dumps(value)
    assert str(root) not in json.dumps(value)
    assert tree(root) == before
    assert not (root / "ecl.json").exists()


def test_query_inheritance_and_etag_are_real_without_stats_initialization(tmp_path, queries):
    root = tmp_path / "game"
    target = instance(root, "fabric", alias="First")
    instance(root, "1.21")
    (target / "fabric.json").write_text(
        json.dumps(
            {
                "id": "fabric",
                "inheritsFrom": "1.21",
                "libraries": [{"name": "net.fabricmc:fabric-loader:0.16"}],
            }
        ),
        encoding="utf-8",
    )
    first = queries.query_instance_summary(root, "fabric", monotonic() + 5)
    assert (first.loader, first.game_version) == ("Fabric", "1.21")
    (target / ".ecl/instance.json").write_text('{"alias":"Second"}', encoding="utf-8")
    second = queries.query_instance_summary(root, "fabric", monotonic() + 5)
    assert second.profile_etag != first.profile_etag
    assert len(second.profile_etag) == 64
    assert not (target / ".ecl/stats.json").exists()


@pytest.mark.parametrize(
    "payload", ["[]", '{"id":"x","inheritsFrom":"../secret"}', '{"id":"x","inheritsFrom":"x"}', '{"id":NaN}']
)
def test_invalid_metadata_is_an_explicit_failure(tmp_path, queries, payload):
    root = tmp_path / "game"
    target = instance(root, "x")
    (target / "x.json").write_text(payload, encoding="utf-8")
    with pytest.raises(InstanceReadError):
        queries.query_instance_summary(root, "x", monotonic() + 5)


def test_running_count_does_not_finalize_or_stop_games(tmp_path, queries):
    root = tmp_path / "game"
    instance(root, "x")
    run = SimpleNamespace(pending=False, exited=False, exit_code=None, game_path=root, version_id="x")
    queries._running_games = {"one": run}
    assert queries.query_instance_summary(root, "x", monotonic() + 5).running_count == 1
    assert queries._running_games == {"one": run}


@pytest.fixture
def shared(tmp_path, queries):
    root = tmp_path / "game"
    instance(root, "one")
    instance(root, "two")
    roots = [root]
    service = PluginWorkerService(tmp_path / "data", instance_queries=queries, instance_roots=lambda: tuple(roots))
    source = tmp_path / "plugin"
    source.mkdir()
    (source / "main.py").write_text("value=1", encoding="utf-8")
    metadata = {"name": "reader", "api_version": 2, "permissions": [{"capability": "instances.read", "required": True}]}
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    service.register(metadata, source, "directory")
    yield service, roots, root
    service.close()


def candidates(service, parent=None):
    view = service.permissions("reader")
    return service.permission_resources("reader", view["content_digest"], parent, "", None, 100)


def allow(service, selected, revision):
    view = service.permissions("reader")
    return service.update_permissions(
        "reader",
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="instances.read", decision="allow", resource_refs=tuple(selected)),),
        PermissionManagementCaller.main_webview,
        revision,
    )


def runtime(service):
    from ECL.plugins.runtime.resources import RuntimeResources

    entry = service._entry("reader")
    return RuntimeResources(entry.session, service._catalog, service._instance_queries, service.refresh_resources)


def invoke(resources, method, **params):
    return resources.handle(method, params, monotonic() + 5)


def test_scoped_resources_do_not_expose_persistent_ids_and_cannot_scan_parent(shared):
    service, _, _ = shared
    root_page = candidates(service)
    root_id = root_page["items"][0]["resource_id"]
    page = candidates(service, root_id)
    selected = page["items"][0]["resource_id"]
    allow(service, [selected], page["catalog_revision"])
    resources = runtime(service)
    listed = invoke(resources, "resources.list", capability="instances.read")["items"]
    ref = listed[0]["instance_ref"]
    assert ref != selected
    assert invoke(resources, "instances.get", instance_ref=ref)["version_id"] == "one"
    with pytest.raises(ProtocolError, match="类别"):
        invoke(resources, "instances.scan", root_ref=ref)
    with pytest.raises(ProtocolError):
        invoke(runtime(service), "instances.get", instance_ref=ref)


def test_invalid_query_is_rejected_before_reading_any_resource(shared, monkeypatch):
    service, _, _ = shared
    resources = runtime(service)

    def forbidden():
        pytest.fail("invalid input must not refresh or read resources")

    monkeypatch.setattr(resources, "_refresh", forbidden)
    with pytest.raises(ValueError):
        invoke(resources, "instances.get", instance_ref="bad", path="untrusted")


def test_root_grant_includes_new_instances_and_revoke_invalidates_old_refs(shared):
    service, _, root = shared
    page = candidates(service)
    root_id = page["items"][0]["resource_id"]
    allow(service, [root_id], page["catalog_revision"])
    resources = runtime(service)
    root_ref = invoke(resources, "resources.list", capability="instances.read")["items"][0]["root_ref"]
    instance(root, "three")
    result = invoke(resources, "instances.scan", root_ref=root_ref)
    assert {item["version_id"] for item in result["items"]} == {"one", "two", "three"}
    view = service.permissions("reader")
    service.update_permissions(
        "reader",
        view["content_digest"],
        view["revision"],
        (PermissionDecision(capability="instances.read", decision="deny"),),
        PermissionManagementCaller.main_webview,
    )
    assert not service.permissions("reader")["required_satisfied"]
    with pytest.raises(ValueError):
        invoke(resources, "instances.scan", root_ref=root_ref)
    with pytest.raises(ValueError):
        invoke(resources, "resources.list", capability="instances.read")


def test_removed_root_stays_retired_after_readding_same_path(shared):
    service, roots, root = shared
    page = candidates(service)
    selected = page["items"][0]["resource_id"]
    allow(service, [selected], page["catalog_revision"])
    roots.clear()
    service.refresh_resources()
    roots.append(root)
    new_page = candidates(service)
    assert not service.permissions("reader")["required_satisfied"]
    active = [item for item in new_page["items"] if item["available"]]
    assert active[0]["resource_id"] != selected
    assert any(not item["available"] and item["selected"] for item in new_page["items"])


def test_scan_page_rejects_instance_replaced_after_snapshot(shared):
    service, _, root = shared
    page = candidates(service)
    allow(service, [page["items"][0]["resource_id"]], page["catalog_revision"])
    resources = runtime(service)
    root_ref = invoke(resources, "resources.list", capability="instances.read")["items"][0]["root_ref"]
    first = invoke(resources, "instances.scan", root_ref=root_ref, limit=1)
    remaining = "two" if first["items"][0]["version_id"] == "one" else "one"
    (root / "versions" / remaining).rename(root / "versions/preserved")
    instance(root, remaining)
    with pytest.raises(InstanceReadError):
        invoke(resources, "instances.scan", root_ref=root_ref, cursor=first["next_cursor"], limit=1)


def test_catalog_corruption_preserves_file_and_denies_instances(shared):
    service, _, _ = shared
    page = candidates(service)
    allow(service, [page["items"][0]["resource_id"]], page["catalog_revision"])
    file = service.data_path / "plugin_resources.json"
    file.write_text("broken", encoding="utf-8")
    with pytest.raises(InstanceReadError):
        service.refresh_resources()
    assert file.read_text(encoding="utf-8") == "broken"
    assert not service.permissions("reader")["required_satisfied"]


def test_instance_recreated_at_same_name_does_not_inherit_grant(shared):
    service, _, root = shared
    root_page = candidates(service)
    root_id = root_page["items"][0]["resource_id"]
    children = candidates(service, root_id)
    original_id = children["items"][0]["resource_id"]
    allow(service, [original_id], children["catalog_revision"])
    (root / "versions/one").rename(root / "versions/preserved-old")
    instance(root, "one")
    refreshed = candidates(service, root_id)
    assert not service.permissions("reader")["required_satisfied"]
    assert any(item["resource_id"] == original_id and not item["available"] for item in refreshed["items"])
    assert (
        next(item for item in refreshed["items"] if item["available"] and item["version_id"] == "one")["resource_id"]
        != original_id
    )


def test_revocation_during_actual_query_discards_late_result(shared, monkeypatch):
    service, _, _ = shared
    root_page = candidates(service)
    root_id = root_page["items"][0]["resource_id"]
    page = candidates(service, root_id)
    allow(service, [page["items"][0]["resource_id"]], page["catalog_revision"])
    resources = runtime(service)
    ref = invoke(resources, "resources.list", capability="instances.read")["items"][0]["instance_ref"]
    started, release = Event(), Event()
    original = service._instance_queries.query_instance_summary

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._instance_queries, "query_instance_summary", slow)
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(invoke, resources, "instances.get", instance_ref=ref)
        try:
            assert started.wait(3)
            view = service.permissions("reader")
            service.update_permissions(
                "reader",
                view["content_digest"],
                view["revision"],
                (PermissionDecision(capability="instances.read", decision="deny"),),
                PermissionManagementCaller.main_webview,
            )
        finally:
            release.set()
        with pytest.raises(ValueError):
            pending.result(5)
    assert not service.permissions("reader")["required_satisfied"]
    with pytest.raises(ValueError):
        invoke(resources, "resources.list", capability="instances.read")


def test_catalog_does_not_hold_resource_lock_while_scanning_instances(shared, monkeypatch):
    service, roots, _ = shared
    root_id = candidates(service)["items"][0]["resource_id"]
    started, release = Event(), Event()
    original = service._catalog._names

    def slow(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(service._catalog, "_names", slow)
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(candidates, service, root_id)
        try:
            assert started.wait(3)
            roots.clear()
            service.refresh_resources()
        finally:
            release.set()
        with pytest.raises(InstanceReadError):
            pending.result(5)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 实测")
def test_windows_junction_cannot_expose_external_instance(tmp_path, queries):
    root = tmp_path / "game"
    (root / "versions").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.json").write_text('{"id":"secret"}', encoding="utf-8")
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(root / "versions/x"), str(outside)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    with pytest.raises(InstanceReadError):
        queries.query_instance_summary(root, "x", monotonic() + 5)
    assert (outside / "x.json").read_text(encoding="utf-8") == '{"id":"secret"}'


def test_posix_opened_descriptor_branch_and_deadline_guard(tmp_path, queries, monkeypatch):
    root = tmp_path / "game"
    target = instance(root, "x")
    if sys.platform != "linux":
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if str(path).replace("\\", "/").startswith("/proc/self/fd/"):
                return target / "x.json"
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        monkeypatch.setattr("ECL.utils.instance_reading.sys.platform", "linux")
    assert queries.query_instance_summary(root, "x", monotonic() + 5).version_id == "x"
    with pytest.raises(InstanceReadError) as error:
        queries.query_instance_summary(root, "x", monotonic() - 1)
    assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"


def test_page_cursor_cannot_change_owner_epoch_or_offset():
    pages = ResourcePages()

    def load():
        return ({"value": "a"}, {"value": "b"})

    first = pages.page("owner-query", "epoch", None, 1, load)
    cursor = first["next_cursor"]
    for signature, generation, changed in [
        ("other", "epoch", cursor),
        ("owner-query", "new", cursor),
        ("owner-query", "epoch", cursor.replace(".1.", ".0.")),
    ]:
        with pytest.raises(ProtocolError):
            pages.page(signature, generation, changed, 1, load)
    assert pages.page("owner-query", "epoch", cursor, 1, load)["items"] == [{"value": "b"}]


def test_completed_single_pages_do_not_exhaust_snapshot_capacity():
    pages = ResourcePages()
    for _ in range(30):
        assert pages.page("query", "epoch", None, 50, lambda: ({"name": "one"},))["next_cursor"] is None
    assert not pages._snapshots


def test_overlapping_root_and_instance_permissions_share_one_runtime_reference(shared):
    service, _, _ = shared
    page = candidates(service)
    root_id = page["items"][0]["resource_id"]
    children = candidates(service, root_id)
    allow(service, [root_id, children["items"][0]["resource_id"]], children["catalog_revision"])
    resources = runtime(service)
    listed = invoke(resources, "resources.list", capability="instances.read")["items"]
    root_ref = next(item["root_ref"] for item in listed if item["kind"] == "instance_root")
    direct_ref = next(item["instance_ref"] for item in listed if item["kind"] == "instance")
    scanned = invoke(resources, "instances.scan", root_ref=root_ref)["items"]
    assert next(item["instance_ref"] for item in scanned if item["version_id"] == "one") == direct_ref


def test_static_symlink_metadata_is_never_read(tmp_path, queries):
    root = tmp_path / "game"
    target = instance(root, "x")
    secret = tmp_path / "outside.json"
    secret.write_text('{"id":"secret"}', encoding="utf-8")
    file = target / "x.json"
    file.unlink()
    try:
        file.symlink_to(secret)
    except OSError:
        pytest.skip("当前 Windows 用户没有创建文件符号链接的权限")
    with pytest.raises(InstanceReadError):
        queries.query_instance_summary(root, "x", monotonic() + 5)


@pytest.mark.skipif(os.name != "nt", reason="真实 Worker 仅支持 Windows")
def test_real_worker_can_query_only_reviewed_instance(shared):

    service, _, _ = shared
    page = candidates(service)
    root_id = page["items"][0]["resource_id"]
    children = candidates(service, root_id)
    allow(service, [children["items"][0]["resource_id"]], children["catalog_revision"])
    entry = service._entry("reader")
    # 宿主侧验证真实 API 通道；测试载荷不取得宿主 Python 对象。
    code = """from ECL.plugin_api import Plugin as BasePlugin
class Plugin(BasePlugin):
    def on_load(self):
        resources = self.host.call('resources.list', {'capability':'instances.read'})
        ref = resources['items'][0]['instance_ref']
        value = self.host.call('instances.get', {'instance_ref':ref})
        self.define_command('result', lambda: {'value':value['version_id']}, output_fields=({'name':'value','type':'string'},))
"""
    metadata_path = entry.root_path / "plugin.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["permissions"].append({"capability": "commands.register", "required": True})
    metadata["permissions"].append({"capability": "commands.execute", "required": True})
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (entry.root_path / "main.py").write_text(code, encoding="utf-8")
    service.register(metadata, entry.root_path, "directory")
    view = service.permissions("reader")
    service.update_permissions(
        "reader",
        view["content_digest"],
        view["revision"],
        (
            PermissionDecision(
                capability="instances.read", decision="allow", resource_refs=(children["items"][0]["resource_id"],)
            ),
            PermissionDecision(capability="commands.register", decision="allow"),
            PermissionDecision(capability="commands.execute", decision="allow"),
        ),
        PermissionManagementCaller.main_webview,
        service.refresh_resources(),
    )
    service.enable("reader")
    assert service.broker("reader").call_command("result", {}, 5) == {"value": "one"}
