from __future__ import annotations

import asyncio
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

import ECL.services.account.authlib as authlib_module
import ECL.services.account.littleskin as littleskin_module
from ECL.services.account.accounts import AccountManager
from ECL.services.account.authlib import AuthlibAccountManager
from ECL.services.account.littleskin import (
    LittleSkinCallback,
    LittleSkinClient,
    LittleSkinCredentials,
    LittleSkinLogin,
    LittleSkinMinecraftToken,
)
from ECL.utils import AccountError
from ECL.utils.environment import Environment

profile_id = "0123456789abcdef0123456789abcdef"
second_profile_id = "f123456789abcdef0123456789abcdef"
profile = {"id": profile_id, "name": "Player"}


def credentials(*, method="device_code", expires_at=None) -> LittleSkinCredentials:
    return LittleSkinCredentials(
        access_token="opaque-oauth",
        refresh_token="opaque-refresh",
        expires_at=expires_at if expires_at is not None else time() + 3600,
        client_id="app-id",
        method=method,
    )


def minecraft_token() -> LittleSkinMinecraftToken:
    return LittleSkinMinecraftToken.model_validate(
        {
            "accessToken": "game-token",
            "clientToken": "server-client-token",
            "selectedProfile": profile,
        }
    )


def client(handler) -> LittleSkinClient:
    return LittleSkinClient("app-id", "private-secret", httpx.Client(transport=httpx.MockTransport(handler)))


def free_callback_uri() -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return f"http://127.0.0.1:{port}/custom/littleskin/callback"


async def callback_request(callback: LittleSkinCallback, query: dict[str, str], *, host=None, path=None) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", callback.uri.port)
    target = f"{path or callback.uri.path}?{urlencode(query)}"
    writer.write(f"GET {target} HTTP/1.1\r\nHost: {host or callback.uri.netloc}\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.parametrize(
    "method,host,has_secret",
    [
        ("device_code", "open.littleskin.cn", False),
        ("authorization_code", "littleskin.cn", True),
    ],
)
def test_oauth_token_endpoint_and_secret_are_specific_to_original_flow(method, host, has_secret):
    def respond(request):
        assert request.url.host == host
        assert request.headers["Accept"] == "application/json"
        values = parse_qs(request.content.decode())
        assert ("client_secret" in values) is has_secret
        assert values["client_id"] == ["app-id"]
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 60,
                "access_token": "any-opaque-format",
                "refresh_token": "new-refresh",
            },
        )

    oauth = client(respond)
    try:
        updated = oauth.refresh(credentials(method=method))
        assert updated.method == method
        assert updated.access_token == "any-opaque-format"
        assert updated.expires_at > time()
        assert "private-secret" not in repr(updated)
    finally:
        oauth.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"code": 403, "message": "private-secret"}),
        httpx.Response(302, headers={"Location": "https://attacker.invalid"}, json={}),
        httpx.Response(200, json={"token_type": "Bearer", "expires_in": "60", "access_token": "secret"}),
        httpx.Response(200, text="private-secret"),
    ],
)
def test_invalid_or_denied_token_response_is_never_success_or_raw_error(response):
    oauth = client(lambda request: response)
    try:
        with pytest.raises(AccountError) as error:
            oauth.refresh(credentials())
        assert "private-secret" not in str(error.value)
    finally:
        oauth.close()


@pytest.mark.parametrize("url", ["https://attacker.invalid/oauth/link", "http://open.littleskin.cn/oauth/link"])
def test_device_authorization_rejects_untrusted_browser_destination(url):
    oauth = client(
        lambda request: httpx.Response(
            200,
            json={
                "user_code": "ABCD",
                "device_code": "hidden-device",
                "verification_uri": url,
                "expires_in": 60,
                "interval": 1,
            },
        )
    )
    try:
        with pytest.raises(AccountError, match="响应无效"):
            oauth.request_device_code()
    finally:
        oauth.close()


def test_profile_token_must_match_selected_authorized_role():
    oauth = client(
        lambda request: httpx.Response(
            200,
            json={
                "accessToken": "game-token",
                "clientToken": "server-token",
                "selectedProfile": {"id": second_profile_id, "name": "Other"},
            },
        )
    )
    try:
        with pytest.raises(AccountError) as error:
            oauth.minecraft_token(credentials(), profile_id)
        assert error.value.error_code == "LITTLESKIN_PROFILE_MISMATCH"
    finally:
        oauth.close()


@pytest.mark.asyncio
async def test_callback_rejects_foreign_state_host_path_and_replay_then_releases_port():
    redirect_uri = free_callback_uri()
    callback = LittleSkinCallback(redirect_uri)
    await callback.start()
    try:
        assert b"400 Bad Request" in await callback_request(callback, {"state": "wrong", "code": "secret"})
        assert b"400 Bad Request" in await callback_request(
            callback,
            {"state": callback.state, "code": "secret"},
            host="attacker.invalid",
        )
        assert b"400 Bad Request" in await callback_request(
            callback,
            {"state": callback.state, "code": "secret"},
            path="/wrong",
        )
        assert not callback.code.done()
        response = await callback_request(callback, {"state": callback.state, "code": "auth-code"})
        assert b"200 OK" in response and b"auth-code" not in response
        assert b"Cache-Control: no-store" in response
        assert await callback.code == "auth-code"
        assert b"400 Bad Request" in await callback_request(callback, {"state": callback.state, "code": "again"})
    finally:
        await callback.close()
    # 用实际回调入口验证端口能再次监听，沿用该入口的地址复用行为。
    next_callback = LittleSkinCallback(redirect_uri)
    try:
        await next_callback.start()
        response = await callback_request(next_callback, {"state": next_callback.state, "code": "next-code"})
        assert b"200 OK" in response
        assert await next_callback.code == "next-code"
    finally:
        await next_callback.close()


@pytest.mark.asyncio
async def test_callback_duplicate_parameters_and_denial_are_validated_without_leaks():
    callback = LittleSkinCallback(free_callback_uri())
    await callback.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", callback.uri.port)
        writer.write(
            (
                f"GET {callback.uri.path}?state={callback.state}&code=one&code=two HTTP/1.1\r\n"
                f"Host: {callback.uri.netloc}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        assert b"400 Bad Request" in await reader.read()
        writer.close()
        await writer.wait_closed()
        assert not callback.code.done()
        await callback_request(callback, {"state": callback.state, "error": "access_denied"})
        with pytest.raises(AccountError) as error:
            await callback.code
        assert error.value.error_code == "LITTLESKIN_ACCESS_DENIED"
    finally:
        await callback.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "https://remote.invalid/callback",
        "http://0.0.0.0:34127/callback",
        "http://127.0.0.1/callback",
        "http://127.0.0.1:34127/callback?code=bad",
        "http://127.0.0.1:99999/callback",
    ],
)
async def test_callback_configuration_rejects_non_loopback_or_ambiguous_addresses(uri):
    with pytest.raises(AccountError):
        LittleSkinCallback(uri)


@pytest.mark.asyncio
async def test_occupied_callback_port_fails_without_fallback():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        callback = LittleSkinCallback(f"http://127.0.0.1:{listener.getsockname()[1]}/callback")
        try:
            with pytest.raises(AccountError) as error:
                await callback.start()
            assert error.value.error_code == "LITTLESKIN_CALLBACK_UNAVAILABLE"
        finally:
            await callback.close()


def mock_service(profiles=None, token_errors=None):
    requests = []
    remaining_errors = list(token_errors or [])

    def respond(request):
        requests.append(request)
        if request.url.path == "/oauth/device_code":
            return httpx.Response(
                200,
                json={
                    "user_code": "ABCD-EFGH",
                    "device_code": "private-device-code",
                    "verification_uri": "https://open.littleskin.cn/oauth/link",
                    "expires_in": 120,
                    "interval": 1,
                },
            )
        if request.url.path == "/oauth/token":
            if remaining_errors:
                return httpx.Response(400, json={"error": remaining_errors.pop(0), "error_description": "secret"})
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "access_token": "opaque-oauth",
                    "refresh_token": "opaque-refresh",
                },
            )
        if request.url.path.endswith("/minecraft/profile"):
            return httpx.Response(200, json=[profile] if profiles is None else profiles)
        if request.url.path.endswith("/authserver/oauth"):
            selected_id = json.loads(request.content)["uuid"]
            return httpx.Response(
                200,
                json={
                    "accessToken": "game-token",
                    "clientToken": "server-client-token",
                    "selectedProfile": {"id": selected_id, "name": "Player"},
                },
            )
        raise AssertionError(request.url)

    return client(respond), requests


async def wait_for_status(login, session_id, status):
    async def wait():
        while login.snapshot(session_id)["status"] not in status:
            await asyncio.sleep(0.01)
        return login.snapshot(session_id)

    return await asyncio.wait_for(wait(), timeout=5)


@pytest.fixture
def immediate_device_sleep(monkeypatch):
    waits = []
    original_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        waits.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr(littleskin_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": fast_sleep}))
    return waits


@pytest.mark.asyncio
async def test_device_polling_honors_pending_slowdown_and_saves_only_on_complete(immediate_device_sleep):
    oauth, requests = mock_service(token_errors=["authorization_pending", "slow_down"])
    commits = []
    events = []
    login = LittleSkinLogin(
        oauth, LittleSkinLogin.default_redirect_uri, lambda *args: commits.append(args) or {}, events.append
    )
    try:
        snapshot = await login.start("device_code")
        ready = await wait_for_status(login, snapshot["session_id"], {"ready", "error"})
        assert ready["status"] == "ready"
        assert immediate_device_sleep == [1, 1, 6]
        assert commits == []
        assert "private-device-code" not in json.dumps(events)
        assert "opaque-oauth" not in json.dumps(events)
        assert "private-secret" not in json.dumps(events)
        await login.complete(snapshot["session_id"])
        assert len(commits) == 1
        assert commits[0][1].access_token == "game-token"
        with pytest.raises(AccountError):
            await login.complete(snapshot["session_id"])
        device_request = requests[0]
        assert "Yggdrasil.PlayerProfiles.Select" not in device_request.content.decode()
        assert "offline_access" in device_request.content.decode()
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_multi_profile_requires_valid_selection_and_stale_session_cannot_cancel(immediate_device_sleep):
    oauth, _ = mock_service(profiles=[profile, {"id": second_profile_id, "name": "Other"}])
    login = LittleSkinLogin(oauth, LittleSkinLogin.default_redirect_uri, lambda *args: {}, lambda event: None)
    try:
        initial = await login.start("device_code")
        selected = await wait_for_status(login, initial["session_id"], {"selecting", "error"})
        assert selected["status"] == "selecting"
        with pytest.raises(AccountError):
            await login.select(initial["session_id"], "0" * 32)
        await login.select(initial["session_id"], second_profile_id)
        await wait_for_status(login, initial["session_id"], {"ready"})
        await login.cancel(initial["session_id"])
        newer = await login.start("device_code")
        with pytest.raises(AccountError):
            await login.cancel(initial["session_id"])
        assert login.snapshot(newer["session_id"])["status"] != "cancelled"
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_browser_flow_uses_configured_callback_and_state_before_account_commit():
    oauth, requests = mock_service()
    commits = []
    login = LittleSkinLogin(oauth, free_callback_uri(), lambda *args: commits.append(args) or {}, lambda event: None)
    try:
        initial = await login.start("authorization_code")
        pending = await wait_for_status(login, initial["session_id"], {"pending", "error"})
        query = parse_qs(httpx.URL(pending["verification_uri"]).query.decode())
        assert query["redirect_uri"] == [login.redirect_uri]
        assert set(query["scope"][0].split()) == {
            "Yggdrasil.PlayerProfiles.Read",
            "Yggdrasil.MinecraftToken.Create",
        }
        callback = login._session.callback
        assert query["state"] == [callback.state]
        await callback_request(callback, {"state": callback.state, "code": "auth-code"})
        await wait_for_status(login, initial["session_id"], {"ready", "error"})
        assert login.snapshot(initial["session_id"])["status"] == "ready"
        assert commits == []
        token_request = next(request for request in requests if request.url.path == "/oauth/token")
        values = parse_qs(token_request.content.decode())
        assert values["redirect_uri"] == [login.redirect_uri]
        assert values["client_secret"] == ["private-secret"]
        await login.complete(initial["session_id"])
        assert len(commits) == 1
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_inflight_network_and_prevents_commit():
    entered = threading.Event()
    release = threading.Event()
    oauth, _ = mock_service()
    original_request = oauth.request_device_code

    def blocked_request():
        entered.set()
        assert release.wait(3)
        return original_request()

    oauth.request_device_code = blocked_request
    commits = []
    login = LittleSkinLogin(
        oauth, LittleSkinLogin.default_redirect_uri, lambda *args: commits.append(args) or {}, lambda e: None
    )
    try:
        initial = await login.start("device_code")
        assert await asyncio.to_thread(entered.wait, 2)
        cancel = asyncio.create_task(login.cancel(initial["session_id"]))
        duplicate_cancel = asyncio.create_task(login.cancel(initial["session_id"]))
        await asyncio.sleep(0.02)
        assert not cancel.done()
        assert not duplicate_cancel.done()
        release.set()
        assert (await cancel)["status"] == "cancelled"
        assert (await duplicate_cancel)["status"] == "cancelled"
        assert commits == []
        assert login._session.credentials is None
    finally:
        release.set()
        await login.close()


@pytest.mark.asyncio
async def test_cancelled_ipc_wait_does_not_cancel_actual_account_save(immediate_device_sleep):
    entered = threading.Event()
    release = threading.Event()
    oauth, _ = mock_service()
    commits = []

    def commit(*args):
        entered.set()
        assert release.wait(3)
        commits.append(args)
        return {"id": "saved-account"}

    login = LittleSkinLogin(oauth, LittleSkinLogin.default_redirect_uri, commit, lambda event: None)
    try:
        initial = await login.start("device_code")
        await wait_for_status(login, initial["session_id"], {"ready"})
        waiting = asyncio.create_task(login.complete(initial["session_id"]))
        assert await asyncio.to_thread(entered.wait, 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        with pytest.raises(AccountError):
            await login.cancel(initial["session_id"])
        release.set()
        await wait_for_status(login, initial["session_id"], {"completed"})
        assert len(commits) == 1
    finally:
        release.set()
        await login.close()


def test_persistence_reuses_profile_id_and_rolls_back_failed_aggregate_save(tmp_path):
    manager = AuthlibAccountManager(tmp_path)
    try:
        account_id, _ = manager.store_littleskin(credentials(), minecraft_token(), lambda key: None)
        files_before = {path: path.read_bytes() for path in manager.account_path.rglob("*.json")}
        tokens_before = json.loads(json.dumps(manager.tokens))
        new_credentials = credentials().model_copy(update={"access_token": "changed-oauth"})

        def fail_selection(key):
            raise OSError("disk unavailable")

        with pytest.raises(OSError):
            manager.store_littleskin(new_credentials, minecraft_token(), fail_selection)
        assert manager.tokens == tokens_before
        assert {path: path.read_bytes() for path in manager.account_path.rglob("*.json")} == files_before
        repeated_id, info = manager.store_littleskin(new_credentials, minecraft_token(), lambda key: None)
        assert repeated_id == account_id
        assert len(manager.accounts) == 1
        assert "OAuth" not in json.dumps(info)
        reloaded = AuthlibAccountManager(tmp_path)
        try:
            assert reloaded.tokens[account_id]["OAuth"]["access_token"] == "changed-oauth"
        finally:
            reloaded.close()
    finally:
        manager.close()


def test_new_account_disk_failure_has_no_partial_files_or_memory(tmp_path, monkeypatch):
    manager = AuthlibAccountManager(tmp_path)
    original_write = authlib_module.atomic_write_text

    def fail_metadata(target, content, **kwargs):
        if target == manager.account_file:
            raise OSError("disk unavailable")
        return original_write(target, content, **kwargs)

    monkeypatch.setattr(authlib_module, "atomic_write_text", fail_metadata)
    try:
        with pytest.raises(OSError):
            manager.store_littleskin(credentials(), minecraft_token(), lambda key: None)
        assert manager.accounts == manager.tokens == {}
        assert list(manager.account_path.rglob("*.json")) == []
    finally:
        manager.close()


def test_rotated_oauth_credentials_are_saved_before_game_exchange_failure(tmp_path):
    manager = AuthlibAccountManager(tmp_path)
    oauth, _ = mock_service()
    manager.littleskin_client = oauth
    try:
        account_id, _ = manager.store_littleskin(credentials(expires_at=1.0), minecraft_token(), lambda key: None)
        rotated = credentials().model_copy(update={"refresh_token": "rotated-refresh"})
        oauth.refresh = lambda previous: rotated

        def fail_game(*args):
            raise AccountError("server unavailable", "LITTLESKIN_NETWORK_FAILED")

        oauth.minecraft_token = fail_game
        with pytest.raises(AccountError):
            manager.refresh_account(account_id)
        stored = json.loads((manager.token_path / f"{account_id}.json").read_text(encoding="utf-8"))
        assert stored["OAuth"]["refresh_token"] == "rotated-refresh"
        assert manager.tokens[account_id]["OAuth"]["refresh_token"] == "rotated-refresh"
        assert stored["AccessToken"] == "game-token"
    finally:
        manager.close()
        oauth.close()


@pytest.mark.asyncio
async def test_account_service_integration_persists_launches_and_removes_without_exposing_oauth(
    tmp_path, immediate_device_sleep
):
    authlib = AuthlibAccountManager(tmp_path)
    manager = AccountManager(tmp_path, authlib_manager=authlib, littleskin_client_id="app-id")
    previous_client = manager.littleskin.client
    previous_client.close()
    oauth, _ = mock_service()
    manager.littleskin.client = authlib.littleskin_client = oauth
    authlib.client.validate = lambda *args, **kwargs: True
    try:
        initial = await manager.littleskin.start("device_code")
        await wait_for_status(manager.littleskin, initial["session_id"], {"ready"})
        account = await manager.littleskin.complete(initial["session_id"])
        assert account["type"] == "authlib"
        assert account["auth_provider"] == "littleskin"
        assert account["uuid"] == profile_id
        assert "opaque-oauth" not in json.dumps(manager.list_accounts())
        launch = await manager.get_launch_credentials()
        assert launch["access_token"] == "game-token"
        assert launch["auth_server"] == LittleSkinClient.server_url
        assert launch["user_type"] == "yggdrasil"
        manager.remove_account(account["id"])
        assert not authlib.tokens
        assert list(authlib.token_path.glob("*.json")) == []
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profiles,error,expected",
    [
        ([], None, "LITTLESKIN_NO_PROFILES"),
        (None, "access_denied", "LITTLESKIN_ACCESS_DENIED"),
        (None, "expired_token", "LITTLESKIN_EXPIRED_TOKEN"),
        (None, "invalid_scope", "LITTLESKIN_INVALID_SCOPE"),
    ],
)
async def test_authorization_failure_stops_without_committing(
    profiles, error, expected, immediate_device_sleep, caplog
):
    oauth, requests = mock_service(profiles=profiles, token_errors=[error] if error else [])
    commits = []
    login = LittleSkinLogin(
        oauth, LittleSkinLogin.default_redirect_uri, lambda *args: commits.append(args) or {}, lambda e: None
    )
    try:
        initial = await login.start("device_code")
        failed = await wait_for_status(login, initial["session_id"], {"error"})
        assert failed["error_code"] == expected
        assert commits == []
        assert len([request for request in requests if request.url.path == "/oauth/token"]) == 1
        assert login._session.credentials is None
        if error == "invalid_scope":
            assert "Minecraft" in caplog.text
            assert "授权码登录" in caplog.text
            assert "private-secret" not in caplog.text
    finally:
        await login.close()


def test_rotated_credentials_survive_disk_failure_and_are_retried_without_second_rotation(tmp_path, monkeypatch):
    manager = AuthlibAccountManager(tmp_path)
    oauth, _ = mock_service()
    manager.littleskin_client = oauth
    manager.client.validate = lambda *args, **kwargs: True
    rotations = []
    original_write = authlib_module.atomic_write_text
    should_fail = True

    def rotate(previous):
        rotations.append(previous)
        return credentials().model_copy(update={"refresh_token": "rotated-refresh"})

    def write(target, content, **kwargs):
        nonlocal should_fail
        if "rotated-refresh" in content and should_fail:
            should_fail = False
            raise OSError("disk unavailable")
        return original_write(target, content, **kwargs)

    oauth.refresh = rotate
    try:
        account_id, _ = manager.store_littleskin(credentials(expires_at=1.0), minecraft_token(), lambda key: None)
        monkeypatch.setattr(authlib_module, "atomic_write_text", write)
        with pytest.raises(OSError):
            manager.refresh_account(account_id)
        assert len(rotations) == 1
        assert manager._pending_oauth[account_id].refresh_token == "rotated-refresh"
        assert manager.get_token(account_id)["AccessToken"] == "game-token"
        assert len(rotations) == 1
        assert manager.tokens[account_id]["OAuth"]["refresh_token"] == "rotated-refresh"
        assert manager._pending_oauth == {}
    finally:
        manager.close()
        oauth.close()


def test_concurrent_refresh_rotates_once_and_delete_cannot_revive_account(tmp_path):
    manager = AuthlibAccountManager(tmp_path)
    oauth, _ = mock_service()
    manager.littleskin_client = oauth
    rotations = []
    entered = threading.Event()
    release = threading.Event()

    def rotate(previous):
        rotations.append(previous)
        entered.set()
        assert release.wait(3)
        return credentials()

    oauth.refresh = rotate
    try:
        account_id, _ = manager.store_littleskin(credentials(expires_at=1.0), minecraft_token(), lambda key: None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(manager.refresh_account, account_id)
            assert entered.wait(2)
            second = pool.submit(manager.refresh_account, account_id)
            release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        assert len(rotations) == 1
        manager.delete_account(account_id)
        with pytest.raises(KeyError):
            manager.refresh_account(account_id)
        assert not manager.tokens and not manager.accounts
    finally:
        release.set()
        manager.close()
        oauth.close()


@pytest.mark.asyncio
async def test_aggregate_save_failure_restores_selection_files_and_emits_no_change(tmp_path, monkeypatch):
    authlib = AuthlibAccountManager(tmp_path)
    manager = AccountManager(tmp_path, authlib_manager=authlib)
    previous = manager.add_offline("Existing")
    before = manager.state_path.read_bytes()
    events = []
    manager.events.subscribe("accounts:changed", events.append)

    def fail_save():
        raise AccountError("disk unavailable", "ACCOUNT_SAVE_FAILED")

    monkeypatch.setattr(manager, "_save_state", fail_save)
    try:
        with pytest.raises(AccountError):
            manager._save_littleskin_account(credentials(), minecraft_token())
        assert manager.current_account()["id"] == previous["id"]
        assert manager.state_path.read_bytes() == before
        assert not authlib.accounts and not authlib.tokens
        assert events == []
        assert not list(authlib.token_path.glob("*.json"))
    finally:
        await manager.close()


@pytest.mark.parametrize("key", ["LITTLESKIN_CLIENT_ID", "LITTLESKIN_CLIENT_SECRET", "LITTLESKIN_REDIRECT_URI"])
def test_littleskin_environment_values_override_dotenv_without_aliases(tmp_path, monkeypatch, key):
    (tmp_path / ".env").write_text(f"{key}=dotenv-value\n", encoding="utf-8")
    monkeypatch.delenv(key, raising=False)
    assert Environment(tmp_path).get_value(key) == "dotenv-value"
    monkeypatch.setenv(key, "system-value")
    assert Environment(tmp_path).get_value(key) == "system-value"
    monkeypatch.setenv(key, "  ")
    assert Environment(tmp_path).get_value(key) == "dotenv-value"


@pytest.mark.asyncio
async def test_build_configuration_fallback_and_default_or_custom_redirect(tmp_path, monkeypatch):
    from ECL.utils.build_env import BuildEnvironment

    monkeypatch.setattr(BuildEnvironment, "littleskin_client_id", "build-id")
    monkeypatch.setattr(BuildEnvironment, "littleskin_client_secret", "")
    monkeypatch.setattr(BuildEnvironment, "littleskin_redirect_uri", "")
    manager = AccountManager(tmp_path, authlib_manager=AuthlibAccountManager(tmp_path))
    try:
        assert manager.littleskin.config() == {
            "device_code": True,
            "authorization_code": False,
            "needs_client_id": False,
        }
        assert manager.littleskin.redirect_uri == LittleSkinLogin.default_redirect_uri
    finally:
        await manager.close()
    custom = free_callback_uri()
    manager = AccountManager(
        tmp_path,
        authlib_manager=AuthlibAccountManager(tmp_path),
        littleskin_client_id="runtime-id",
        littleskin_client_secret="runtime-secret",
        littleskin_redirect_uri=custom,
    )
    try:
        assert manager.littleskin.client.client_id == "runtime-id"
        assert manager.littleskin.config()["authorization_code"] is True
        assert manager.littleskin.redirect_uri == custom
        assert "runtime-secret" not in json.dumps(manager.littleskin.config())
    finally:
        await manager.close()


def test_both_build_injection_paths_serialize_littleskin_values_without_executing_them(tmp_path, monkeypatch):
    import ast
    import re
    import textwrap

    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
    blocks = re.findall(r"- name: Inject build secrets\n.*?run: \|\n(.*?)(?=\n      - name:)", workflow, re.S)
    assert len(blocks) == 2
    source = Path("ECL/utils/build_env.py").read_text(encoding="utf-8")
    target = tmp_path / "ECL" / "utils" / "build_env.py"
    target.parent.mkdir(parents=True)
    tricky_value = 'quote"slash\\\\\n$(not-a-command)'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITTLESKIN_CLIENT_ID", "test-id")
    monkeypatch.setenv("LITTLESKIN_CLIENT_SECRET", tricky_value)
    monkeypatch.setenv("LITTLESKIN_REDIRECT_URI", LittleSkinLogin.default_redirect_uri)
    for block in blocks:
        target.write_text(source, encoding="utf-8")
        script = textwrap.dedent(block).split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        exec(compile(script, "<build-injection>", "exec"), {})
        parsed = ast.parse(target.read_text(encoding="utf-8"))
        environment_class = next(
            node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == "BuildEnvironment"
        )
        assignments = {
            statement.targets[0].id: ast.literal_eval(statement.value)
            for statement in environment_class.body
            if isinstance(statement, ast.Assign)
        }
        assert assignments["littleskin_client_secret"] == tricky_value
        assert assignments["littleskin_client_id"] == "test-id"
        assert assignments["littleskin_redirect_uri"] == LittleSkinLogin.default_redirect_uri
