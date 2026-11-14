from __future__ import annotations

import json
from time import monotonic

import pytest
from test_plugin_worker_manager import make_plugin

from ECL.plugins.api._rpc import RpcRequest
from ECL.plugins.api.contracts import ProtocolError
from ECL.plugins.framework import PluginManager
from ECL.plugins.installation_store import WorkerManifest


@pytest.fixture
def development(tmp_path):
    source = tmp_path / "data/plugins/sources/example"
    make_plugin(source, tmp_path / "marker")
    manager = PluginManager()
    manager.initialize(tmp_path / "data", tmp_path / "resources")
    assert manager.enable("example").success
    service = manager.worker_service
    service.development._interval = 3600
    try:
        yield manager, service, source
    finally:
        manager.close()


def flush(service):
    now = monotonic()
    service.development.poll(now)
    service.development.poll(now + 2)


def edit(source, replacement="updated"):
    main = source / "main.py"
    content = main.read_text(encoding="utf-8")
    main.write_text(content.replace("return {'value':value}", f"return {{'value':{replacement!r}}}"), encoding="utf-8")


def test_development_default_off_and_real_worker_reload_preserves_settings(development):
    manager, service, source = development
    assert service.development.view() == {"enabled": False, "items": []}
    settings = service.settings_view("example")
    service.update_settings("example", settings["content_digest"], settings["schema_revision"], 0, {"greeting": "kept"})
    service.development.set_enabled(True)
    old_run = service.runtime_info("example")["run_id"]
    edit(source)
    flush(service)
    assert service.runtime_info("example")["run_id"] != old_run
    assert manager.call_command("example:echo", {"value": "input"}) == {"value": "updated"}
    assert manager.get_settings("example")["values"] == {"greeting": "kept"}
    # 授权存储已随权限系统移除，不再写入任何授权记录。
    assert not (service.data_path / "plugin_grants").exists()
    service.development.set_enabled(False)
    assert service.runtime_info("example")["run_state"] != "enabled"


def test_opt_out_and_byte_identical_saves_do_not_restart(development):
    _manager, service, source = development
    service.development.set_enabled(True)
    run = service.runtime_info("example")["run_id"]
    main = source / "main.py"
    main.write_bytes(main.read_bytes())
    flush(service)
    assert service.runtime_info("example")["run_id"] == run
    manifest_path = source / "plugin.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["supports_hot_reload"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    edit(source)
    flush(service)
    assert service.runtime_info("example")["run_id"] == run
    assert service.development.view()["items"][0]["status"] == "opted_out"
    manifest["supports_hot_reload"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    flush(service)
    assert service.runtime_info("example")["run_id"] != run


def test_development_uses_physical_source_for_noncanonical_host_path(development):
    manager, service, source = development
    service._entry("example").source_path = source.parent / ".." / "sources" / "example"
    state = service.development.set_enabled(True)["items"][0]
    assert state["status"] == "watching"
    assert state["reload_count"] == 1
    edit(source)
    flush(service)
    assert manager.call_command("example:echo", {"value": "input"}) == {"value": "updated"}


def test_syntax_error_keeps_old_copy_and_next_valid_save_recovers(development):
    manager, service, source = development
    service.development.set_enabled(True)
    run = service.runtime_info("example")["run_id"]
    main = source / "main.py"
    original = main.read_text(encoding="utf-8")
    main.write_text("def broken(:", encoding="utf-8")
    flush(service)
    assert service.development.view()["items"][0]["status"] == "failed"
    assert service.runtime_info("example")["run_id"] == run
    assert manager.call_command("example:echo", {"value": "old"}) == {"value": "old"}
    main.write_text(original, encoding="utf-8")
    edit(source, "recovered")
    flush(service)
    assert manager.call_command("example:echo", {"value": "old"}) == {"value": "recovered"}


def test_manual_disable_is_not_undone_by_late_changes(development):
    manager, service, source = development
    service.development.set_enabled(True)
    assert manager.disable("example").success
    edit(source)
    flush(service)
    assert service.runtime_info("example")["run_state"] == "disabled"
    assert service.development.view()["items"][0]["status"] == "disabled"


def test_ignored_manifest_fields_do_not_block_development(development):
    _manager, service, source = development
    service.development.set_enabled(True)
    run = service.runtime_info("example")["run_id"]
    manifest = json.loads((source / "plugin.json").read_bytes())
    # 权限声明不再是清单字段；改动属于普通内容变化，按热重载处理。
    manifest["permissions"] = [{"capability": "ui.inject", "required": True}]
    (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    flush(service)
    assert service.runtime_info("example")["run_id"] != run
    assert service.runtime_info("example")["run_state"] == "enabled"
    assert service.development.view()["items"][0]["status"] == "watching"


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_manifest_rejects_non_boolean_hot_reload(value):
    with pytest.raises(ValueError):
        WorkerManifest.model_validate({"api_version": 2, "name": "sample", "supports_hot_reload": value})


def test_ignored_build_inputs_do_not_reload(development):
    _manager, service, source = development
    service.development.set_enabled(True)
    run = service.runtime_info("example")["run_id"]
    ignored = source / "node_modules/package"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("changing build dependency", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/main.pyc").write_bytes(b"cache")
    flush(service)
    assert service.runtime_info("example")["run_id"] == run


def test_disabled_plugin_can_be_enabled_with_latest_source(development):
    manager, service, source = development
    service.development.set_enabled(True)
    assert manager.disable("example").success
    edit(source, "after manual enable")
    manager.list_plugins()
    assert manager.enable("example").success
    assert manager.call_command("example:echo", {"value": "input"}) == {"value": "after manual enable"}


def test_manual_reload_restarts_even_without_source_changes_but_enable_is_idempotent(development):
    manager, service, _source = development
    service.development.set_enabled(True)
    run = service.runtime_info("example")["run_id"]
    assert manager.enable("example").success
    assert service.runtime_info("example")["run_id"] == run
    assert manager.reload("example").success
    assert service.runtime_info("example")["run_id"] != run


def test_uninstall_drops_development_registration_and_does_not_recreate_code(development):
    manager, service, source = development
    service.development.set_enabled(True)
    assert manager.uninstall("example").success
    flush(service)
    assert service.development.view()["items"] == []
    assert not source.exists()
    assert service.development.set_enabled(False)["enabled"] is False


def test_directory_replacement_ends_trust_even_with_identical_bytes(development):
    import shutil

    _manager, service, source = development
    service.development.set_enabled(True)
    previous = source.with_name("previous-source")
    assert previous.resolve().parent == source.resolve().parent
    source.rename(previous)
    shutil.copytree(previous, source)
    flush(service)
    assert service.development.view()["items"][0]["status"] == "review_required"
    assert service.runtime_info("example")["run_state"] == "disabled"


def test_source_removal_releases_listener_without_removing_private_configuration(development):
    import shutil

    _manager, service, source = development
    service.development.set_enabled(True)
    settings = service.settings_view("example")
    service.update_settings(
        "example", settings["content_digest"], settings["schema_revision"], 0, {"greeting": "preserved"}
    )
    assert source.resolve().is_relative_to(service.data_path.resolve())
    shutil.rmtree(source)
    flush(service)
    assert service.development.view()["items"] == []
    assert service.development.set_enabled(False)["enabled"] is False
    target = service.data_path / "plugins" / "settings" / f"{settings['installation_id']}.json"
    assert json.loads(target.read_bytes())["values"]["greeting"] == "preserved"


def test_reload_waits_for_accepted_write_then_recovers_without_second_worker(development, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import ECL.plugins.settings as settings_module

    manager, service, source = development
    service.development.set_enabled(True)
    original_run = service.runtime_info("example")["run_id"]
    broker = service.broker("example")
    started, release = Event(), Event()
    original_write = settings_module.atomic_write_text

    def write(target, content):
        if target.parent.name == "settings" and target.parent.parent.name == "plugins":
            started.set()
            assert release.wait(10)
        return original_write(target, content)

    monkeypatch.setattr(settings_module, "atomic_write_text", write)
    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(
                broker.handle,
                RpcRequest(
                    "settings.set",
                    {"values": {"greeting": "kept"}, "expected_revision": 0},
                    monotonic() + 10,
                    "development-accepted-write",
                ),
            )
            assert started.wait(2)
            edit(source)
            with pytest.raises(ProtocolError) as error:
                service.development.retry("example")
            assert error.value.error_code == "PLUGIN_HOST_CLEANUP_PENDING"
            assert service.runtime_info("example")["run_id"] == original_run
            assert service.runtime_info("example")["cleanup_state"] == "pending"
            release.set()
            assert future.result(5) == {"revision": 1}
        flush(service)
        assert service.runtime_info("example")["run_id"] != original_run
        assert manager.call_command("example:echo", {"value": "x"}) == {"value": "updated"}
        assert service.settings_view("example")["values"] == {"greeting": "kept"}
    finally:
        release.set()
