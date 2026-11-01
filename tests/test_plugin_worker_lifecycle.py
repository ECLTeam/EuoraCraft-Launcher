from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ECL.plugin_api._errors import PluginRuntimeError
from ECL.plugin_api._rpc import RpcRequest
from ECL.plugin_api.contracts import ProtocolError
from ECL.plugins.grant_models import (
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantError, PluginGrantStore
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.supervisor import WorkerSupervisor


def _prepare_session(supervisor, tmp_path, code_path, declarations=()):
    binding = PluginContentBinding.capture(code_path, declarations, "windows-x86_64-cp311")
    store = PluginGrantStore(tmp_path / "data", implemented_capabilities=frozenset(set()))
    session = store.open_session(store.new_installation_id(), binding, supervisor.run_id)
    return store, session


def _review(store, session, decisions=()):
    view = session.view()
    store.update(
        session,
        decisions,
        expected_revision=view.revision,
        content_digest=view.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


def _phase(supervisor, phase, previous):
    reply = supervisor.request("lifecycle.invoke", {"phase": phase, "run_state": previous})
    assert reply["ok"] is True, reply
    return reply["data"]["run_state"]


def test_production_loader_requires_explicit_review_even_with_no_business_capabilities(tmp_path: Path) -> None:
    code_path = tmp_path / "code"
    code_path.mkdir()
    marker = tmp_path / "executed.txt"
    (code_path / "main.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('entered')\n", encoding="utf-8"
    )
    with WorkerSupervisor() as supervisor:
        store, session = _prepare_session(supervisor, tmp_path, code_path)
        with pytest.raises(PluginGrantError) as error:
            supervisor.authorize_load(session)
        assert error.value.error_code == "PLUGIN_GRANT_REVIEW_REQUIRED"
        assert not marker.exists()
        _review(store, session)
        supervisor.authorize_load(session)
        assert marker.read_text() == "entered"


def test_missing_required_permission_prevents_first_plugin_statement(tmp_path: Path) -> None:
    code_path = tmp_path / "code"
    code_path.mkdir()
    marker = tmp_path / "executed.txt"
    (code_path / "main.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('entered')\n", encoding="utf-8"
    )
    with WorkerSupervisor() as supervisor:
        store, session = _prepare_session(
            supervisor, tmp_path, code_path, (PermissionDeclaration(capability="instances.read", required=True),)
        )
        _review(store, session)
        with pytest.raises(PluginGrantError) as error:
            supervisor.authorize_load(session)
        assert error.value.error_code == "PLUGIN_REQUIRED_PERMISSION_DENIED"
        assert not marker.exists()


def test_different_run_cannot_reuse_reviewed_grants(tmp_path: Path) -> None:
    code_path = tmp_path / "code"
    code_path.mkdir()
    (code_path / "main.py").write_text("value = 1", encoding="utf-8")
    with WorkerSupervisor() as supervisor:
        store, session = _prepare_session(WorkerSupervisor(), tmp_path, code_path)
        _review(store, session)
        with pytest.raises(PluginGrantError) as error:
            supervisor.authorize_load(session)
        assert error.value.error_code == "PLUGIN_SESSION_EXPIRED"


@pytest.mark.parametrize("use_frozen", [False, True])
def test_real_worker_sync_async_lifecycle_relative_import_resources_and_reverse_call(
    tmp_path: Path, use_frozen: bool
) -> None:
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if use_frozen and not artifact:
        pytest.skip("冻结验收需要提供实际发行物")
    executable = (
        ExecutableLaunchSpec(Path(artifact).resolve(), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if use_frozen
        else None
    )
    code_path = tmp_path / "code"
    code_path.mkdir()
    report_path = tmp_path / "lifecycle.json"
    (code_path / "helper.py").write_text("prefix = 'relative'\n", encoding="utf-8")
    (code_path / "value.txt").write_text("resource", encoding="utf-8")
    (code_path / "main.py").write_text(
        "import json, os, importlib.resources\nfrom pathlib import Path\n"
        "from ECL.plugin_api import Plugin as BasePlugin\nfrom .helper import prefix\n"
        f"report = Path({str(report_path)!r})\n"
        "class Plugin(BasePlugin):\n"
        "    async def on_load(self):\n"
        "        resource = importlib.resources.files(__package__).joinpath('value.txt').read_text()\n"
        "        reply = await self.host.call_async('test.echo', {'value':prefix+'-'+resource})\n"
        "        reference = self.register_callback(self.echo)\n"
        "        self.host.call('test.register', {'callback_ref':reference})\n"
        "        report.write_text(json.dumps({'pid':os.getpid(),'value':reply['value']}))\n"
        "    def on_enable(self):\n"
        "        self.host.call('test.echo', {'value':'enabled'})\n"
        "    async def echo(self, value):\n"
        "        return await self.host.call_async('test.echo', {'value':value})\n",
        encoding="utf-8",
    )
    registrations = []
    session = None

    def host_handler(request: RpcRequest):
        try:
            session.authorize("plugin.base")
        except PluginGrantError as exc:
            raise ProtocolError("宿主授权拒绝", exc.error_code) from exc
        if request.method == "test.echo":
            return {"value": request.params["value"]}
        if request.method == "test.register":
            registrations.append(request.params["callback_ref"])
            return {"accepted": True}
        raise ProtocolError("方法未定义", "PLUGIN_API_METHOD_NOT_FOUND")

    supervisor = WorkerSupervisor(executable, host_handler=host_handler)
    try:
        supervisor.start()
        store, session = _prepare_session(supervisor, tmp_path, code_path, ())
        _review(store, session, ())
        supervisor.authorize_load(session)
        assert _phase(supervisor, "load", "loaded") == "loaded"
        report = json.loads(report_path.read_bytes())
        assert report == {"pid": supervisor.pid, "value": "relative-resource"}
        assert supervisor.pid != os.getpid()
        assert _phase(supervisor, "enable", "loaded") == "enabled"
        callback = {"callback_ref": registrations[0], "args": {"value": "callback-reverse"}}
        assert supervisor.request("callbacks.invoke", callback)["data"]["value"] == {"value": "callback-reverse"}
        assert _phase(supervisor, "frontend_ready", "enabled") == "enabled"
        assert _phase(supervisor, "disable", "enabled") == "disabled"
        assert supervisor.request("callbacks.invoke", callback)["error"]["code"] == "PLUGIN_CALLBACK_EXPIRED"
        assert _phase(supervisor, "unload", "disabled") == "unloaded"
    finally:
        supervisor.close()
    assert supervisor.active_count() == 0


def test_invalid_lifecycle_order_and_legacy_class_do_not_get_adapted(tmp_path: Path) -> None:
    code_path = tmp_path / "code"
    code_path.mkdir()
    (code_path / "main.py").write_text("class Plugin:\n    pass\n", encoding="utf-8")
    with WorkerSupervisor() as supervisor:
        store, session = _prepare_session(supervisor, tmp_path, code_path)
        _review(store, session)
        supervisor.authorize_load(session)
        assert (
            supervisor.request("lifecycle.invoke", {"phase": "enable", "run_state": "loaded"})["error"]["code"]
            == "PLUGIN_SESSION_EXPIRED"
        )
        assert (
            supervisor.request("lifecycle.invoke", {"phase": "load", "run_state": "loaded"})["error"]["code"]
            == "PLUGIN_API_VERSION_UNSUPPORTED"
        )
        assert supervisor.request("runtime.info")["data"]["run_state"] == "failed"


def test_same_supervisor_cannot_restart_old_run_identity() -> None:
    supervisor = WorkerSupervisor()
    supervisor.start()
    supervisor.close()
    with pytest.raises(PluginRuntimeError) as error:
        supervisor.start()
    assert error.value.error_code == "PLUGIN_SESSION_EXPIRED"
