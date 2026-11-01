from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_accounts import FakeMicrosoftManager

from ECL.plugins import PluginManager
from ECL.plugins.grant_models import PermissionDecision, PermissionManagementCaller
from ECL.plugins.grant_store import PluginGrantError
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.worker_service import PluginWorkerService
from ECL.services import AccountManager
from ECL.utils import AccountError


@pytest.mark.skipif(sys.platform != "win32", reason="第三方 Worker 当前只支持 Windows x64")
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_real_auth_worker_persists_only_own_data_and_restarts(tmp_path: Path, frozen: bool) -> None:
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if frozen and not artifact:
        pytest.skip("需要显式指定真实冻结发行物")
    executable = (
        ExecutableLaunchSpec(Path(artifact), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"))
        if frozen
        else None
    )
    source = tmp_path / "code"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples/plugins/worker_auth", source)
    metadata = json.loads((source / "plugin.json").read_bytes())
    metadata["permissions"][0]["required"] = False
    (source / "plugin.json").write_text(json.dumps(metadata), encoding="utf-8")
    name = metadata["name"]
    data_path = tmp_path / "data"
    provider_id = ""
    for restart in (False, True):
        accounts = AccountManager(data_path, microsoft_manager=FakeMicrosoftManager())
        framework = PluginManager(auth_providers=accounts.plugin_auth_providers)
        service = PluginWorkerService(data_path, executable, auth_sink=framework._on_worker_auth)
        framework.worker_service = service
        try:
            service.register(metadata, source, "directory")
            view = service.permissions(name)
            if not restart:
                service.update_permissions(
                    name,
                    view["content_digest"],
                    view["revision"],
                    (PermissionDecision(capability="auth.provider", decision="allow"),),
                    PermissionManagementCaller.main_webview,
                )
            service.enable(name)
            providers = accounts.list_auth_providers()
            assert len(providers) == 1
            if not restart:
                provider_id = providers[0]["id"]
                for invalid in ({"token": 42}, {"token": "t" * 1025}, {"token": "test", "extra": "unexpected"}):
                    with pytest.raises(AccountError) as invalid_login:
                        accounts.add_plugin_account(provider_id, invalid)
                    assert invalid_login.value.error_code == "AUTH_PROVIDER_LOGIN_FAILED"
                    assert accounts.list_accounts()["accounts"] == []
                accounts.add_plugin_account(provider_id, {"token": "synthetic-test-token"})
            assert providers[0]["id"] == provider_id
            assert asyncio.run(accounts.get_launch_credentials())["access_token"] == "synthetic-test-token"
            assert all("data" not in item for item in accounts.list_accounts()["accounts"])
            if not restart:
                old_provider = accounts.plugin_auth_providers.get(provider_id)
                for decision in ("deny", "allow"):
                    current = service.permissions(name)
                    service.update_permissions(
                        name,
                        current["content_digest"],
                        current["revision"],
                        (PermissionDecision(capability="auth.provider", decision=decision),),
                        PermissionManagementCaller.main_webview,
                    )
                    assert service._entry(name).supervisor.is_alive()
                    assert accounts.list_auth_providers() == []
                    with pytest.raises(PluginGrantError):
                        old_provider.resolve_credentials(
                            {
                                "id": "demo",
                                "alias": "DemoPlayer",
                                "uuid": "0" * 31 + "1",
                                "data": {"token": "synthetic-test-token"},
                            }
                        )
                service.stop(name)
                service.enable(name)
                assert asyncio.run(accounts.get_launch_credentials())["access_token"] == "synthetic-test-token"
            service.stop(name)
            with pytest.raises(AccountError) as error:
                asyncio.run(accounts.get_launch_credentials())
            assert error.value.error_code == "AUTH_PROVIDER_UNAVAILABLE"
            assert len(accounts.list_accounts()["accounts"]) == 1
        finally:
            service.close()
            framework._command_executor.shutdown(wait=True)
