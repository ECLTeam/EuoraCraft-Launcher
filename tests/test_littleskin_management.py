from __future__ import annotations

import asyncio
import json
from time import time
from urllib.parse import parse_qs

import httpx
import pytest

from ECL.api.models import LittleSkinStartRequest, LittleSkinTextureRequest, WardrobeSyncTextureRequest
from ECL.services.account.authlib import AuthlibAccountManager
from ECL.services.account.littleskin import (
    LittleSkinClient,
    LittleSkinCredentials,
    LittleSkinLogin,
    LittleSkinMinecraftToken,
)
from ECL.utils import AccountError

profile_id = "0123456789abcdef0123456789abcdef"
profile = {"id": profile_id, "name": "ManagedPlayer"}
texture_hash = "a" * 64


def credentials(expires_at=None):
    return LittleSkinCredentials(
        access_token="oauth",
        refresh_token="refresh",
        client_id="app",
        method="device_code",
        expires_at=expires_at if expires_at is not None else time() + 3600,
    )


def game_token():
    return LittleSkinMinecraftToken.model_validate(
        {"accessToken": "game", "clientToken": "client", "selectedProfile": profile}
    )


@pytest.fixture
def provider():
    state = {"pid": 73, "name": profile["name"], "tid_skin": 10, "tid_cape": 20}
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/minecraft/profile"):
            return httpx.Response(200, json=[profile])
        if request.url.path == "/api/players":
            return httpx.Response(200, json=[state])
        if request.url.path == "/api/closet":
            kind = request.url.params["category"]
            return httpx.Response(
                200,
                json={
                    "current_page": int(request.url.params["page"]),
                    "last_page": 1,
                    "total": 1,
                    "data": [
                        {
                            "tid": 11 if kind == "skin" else 21,
                            "name": "Texture",
                            "type": "alex" if kind == "skin" else "cape",
                            "hash": texture_hash,
                        }
                    ],
                },
            )
        if request.url.path == "/api/players/73/textures":
            body = json.loads(request.content)
            if request.method == "DELETE":
                state[f"tid_{body['type'][0]}"] = 0
            else:
                for key, value in body.items():
                    state[f"tid_{key}"] = value
            return httpx.Response(200, json={"code": 0, "data": state})
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated",
                    "refresh_token": "rotated-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        raise AssertionError(f"Unexpected endpoint: {request.method} {request.url.path}")

    oauth = LittleSkinClient("app", "secret", httpx.Client(transport=httpx.MockTransport(respond)))
    yield oauth, state, requests
    oauth.close()


@pytest.mark.parametrize(
    "kind,tid,other", [("skin", 11, "cape"), ("cape", 21, "skin"), ("skin", None, "cape"), ("cape", None, "skin")]
)
def test_texture_write_uses_owned_pid_and_only_changes_selected_kind(provider, kind, tid, other):
    oauth, state, requests = provider
    before = state[f"tid_{other}"]
    result = oauth.set_texture(credentials(), profile_id, kind, tid)
    assert result.pid == 73
    assert getattr(result, f"tid_{kind}") == (tid or 0)
    assert state[f"tid_{other}"] == before
    writes = [request for request in requests if request.method in {"PUT", "DELETE"}]
    assert len(writes) == 1
    assert json.loads(writes[0].content) == ({"type": [kind]} if tid is None else {kind: tid})


def test_foreign_texture_is_rejected_before_any_write(provider):
    oauth, state, requests = provider
    with pytest.raises(AccountError, match="云端衣柜"):
        oauth.set_texture(credentials(), profile_id, "skin", 777)
    assert not any(request.method in {"PUT", "DELETE"} for request in requests)
    assert state["tid_skin"] == 10


def test_foreign_uuid_never_turns_into_a_pid(provider):
    oauth, _, requests = provider
    with pytest.raises(AccountError) as error:
        oauth.set_texture(credentials(), "f" * 32, "skin", None)
    assert error.value.error_code == "LITTLESKIN_ACCOUNT_MISMATCH"
    assert not any(request.method in {"PUT", "DELETE"} for request in requests)


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"code": 403}, "LITTLESKIN_PERMISSION_REQUIRED"),
        (403, {}, "LITTLESKIN_PERMISSION_REQUIRED"),
        (401, {}, "LITTLESKIN_AUTH_REQUIRED"),
        (429, {}, "LITTLESKIN_RATE_LIMITED"),
        (500, {}, "LITTLESKIN_SERVER_FAILED"),
    ],
)
def test_management_failures_are_distinguished(status, body, expected):
    oauth = LittleSkinClient(
        "app", http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    )
    try:
        with pytest.raises(AccountError) as error:
            oauth.closet(credentials(), "skin", 1)
        assert error.value.error_code == expected
    finally:
        oauth.close()


@pytest.mark.parametrize("mutation", [{"hash": "https://evil.invalid"}, {"type": "cape"}, {"tid": True}, {"tid": 0}])
def test_closet_rejects_malformed_or_wrong_category_textures(mutation):
    item = {"tid": 1, "name": "Texture", "type": "steve", "hash": texture_hash, **mutation}
    oauth = LittleSkinClient(
        "app",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"current_page": 1, "last_page": 1, "total": 1, "data": [item]}
                )
            )
        ),
    )
    try:
        with pytest.raises(AccountError) as error:
            oauth.closet(credentials(), "skin", 1)
        assert error.value.error_code == "LITTLESKIN_INVALID_RESPONSE"
    finally:
        oauth.close()


def test_management_refresh_persists_rotation_without_exchanging_game_token(tmp_path, provider):
    oauth, _, requests = provider
    manager = AuthlibAccountManager(tmp_path)
    account_id, _ = manager.store_littleskin(credentials(expires_at=1.0), game_token(), lambda saved_id: None)
    manager.littleskin_client = oauth
    try:
        result = manager.manage_littleskin(account_id, lambda client, value, uuid: client.player(value, uuid))
        assert result.pid == 73
        assert manager.tokens[account_id]["OAuth"]["access_token"] == "rotated"
        assert (
            json.loads((manager.token_path / f"{account_id}.json").read_text())["OAuth"]["refresh_token"]
            == "rotated-refresh"
        )
        assert len([request for request in requests if request.url.path == "/oauth/token"]) == 1
        manager.manage_littleskin(account_id, lambda client, value, uuid: client.player(value, uuid))
        assert len([request for request in requests if request.url.path == "/oauth/token"]) == 1
    finally:
        manager.close()


def test_management_reauthorization_is_in_place_and_rejects_other_profile(tmp_path):
    manager = AuthlibAccountManager(tmp_path)
    try:
        account_id, _ = manager.store_littleskin(credentials(), game_token(), lambda saved_id: None)
        manager.accounts[account_id]["LocalNote"] = "preserved"
        account_id_after, _ = manager.store_littleskin(
            credentials(), game_token(), lambda saved_id: None, target_account_id=account_id
        )
        assert account_id_after == account_id
        assert len(manager.accounts) == 1
        assert manager.accounts[account_id]["LocalNote"] == "preserved"
        before = (manager.token_path / f"{account_id}.json").read_bytes()
        changed = game_token().model_copy(
            update={"selected_profile": game_token().selected_profile.model_copy(update={"id": "f" * 32})}
        )
        with pytest.raises(AccountError):
            manager.store_littleskin(credentials(), changed, lambda saved_id: None, target_account_id=account_id)
        assert (manager.token_path / f"{account_id}.json").read_bytes() == before
    finally:
        manager.close()


@pytest.mark.parametrize(
    "payload",
    [{"method": "device_code", "purpose": "management"}, {"method": "authorization_code", "account_id": "account"}],
)
def test_login_purpose_and_target_must_match(payload):
    with pytest.raises(ValueError):
        LittleSkinStartRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"account_id": "account", "kind": "cape", "tid": True},
        {"account_id": "account", "kind": "skin", "tid": 0},
        {"account_id": "account", "kind": "skin", "tid": 1, "pid": 73},
    ],
)
def test_ipc_rejects_illegal_texture_requests(payload):
    with pytest.raises(ValueError):
        LittleSkinTextureRequest.model_validate(payload)


def test_texture_sync_requires_explicit_kind():
    with pytest.raises(ValueError):
        WardrobeSyncTextureRequest.model_validate({"account_id": "account"})


@pytest.mark.asyncio
@pytest.mark.parametrize("target_deleted", [False, True])
async def test_management_login_requests_scopes_and_binds_existing_profile(provider, monkeypatch, target_deleted):
    oauth, _, requests = provider
    commits = []
    oauth.request_device_code = lambda **kwargs: None  # Browser flow avoids mocked device polling.

    def commit(*args):
        if target_deleted:
            raise AccountError("目标账户已被删除", "LITTLESKIN_ACCOUNT_MISMATCH")
        commits.append(args)
        return {"id": args[0]}

    login = LittleSkinLogin(
        oauth,
        "http://127.0.0.1:34127/oauth/littleskin/callback",
        lambda *args: {},
        lambda event: None,
        commit,
    )

    async def authorize(session):
        session.credentials = credentials()

    async def exchange(session, uuid):
        assert uuid == profile_id
        session.minecraft_token = game_token()
        session.status = "ready"

    monkeypatch.setattr(login, "_authorize_browser", authorize)
    monkeypatch.setattr(login, "_exchange", exchange)
    initial = await login.start("authorization_code", target_account_id="existing", target_profile_id=profile_id)
    for _ in range(100):
        if login.snapshot(initial["session_id"])["status"] == "ready":
            break
        await asyncio.sleep(0.01)
    assert login.snapshot(initial["session_id"])["account_id"] == "existing"
    assert login.snapshot(initial["session_id"])["status"] == "ready"
    if target_deleted:
        with pytest.raises(AccountError) as error:
            await login.complete(initial["session_id"])
        assert error.value.error_code == "LITTLESKIN_ACCOUNT_MISMATCH"
        assert login.snapshot(initial["session_id"])["status"] == "error"
        assert login._session.credentials is None
        assert commits == []
    else:
        assert await login.complete(initial["session_id"]) == {"id": "existing"}
        assert commits[0][0] == "existing"
    assert not any(request.url.path.endswith("/authserver/oauth") for request in requests)
    await login.close()


def test_management_device_scope_is_separate_from_regular_login():
    scopes = []

    def respond(request):
        scopes.append(parse_qs(request.content.decode())["scope"][0].split())
        return httpx.Response(
            200,
            json={
                "user_code": "code",
                "device_code": "private",
                "verification_uri": "https://open.littleskin.cn/oauth/link",
                "expires_in": 120,
                "interval": 1,
            },
        )

    oauth = LittleSkinClient("app", http=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        oauth.request_device_code()
        oauth.request_device_code(management=True)
        assert "Player.ReadWrite" not in scopes[0] and "Closet.ReadWrite" not in scopes[0]
        assert {"Player.ReadWrite", "Closet.ReadWrite", "offline_access"}.issubset(scopes[1])
        assert "Yggdrasil.PlayerProfiles.Select" not in scopes[1]
    finally:
        oauth.close()


def test_write_timeout_is_uncertain_and_is_not_replayed():
    writes = []

    def respond(request):
        if request.method == "DELETE":
            writes.append(request)
            raise httpx.ReadTimeout("Response lost", request=request)
        if request.url.path == "/api/players":
            return httpx.Response(200, json=[{"pid": 73, "name": profile["name"], "tid_skin": 10, "tid_cape": 20}])
        return httpx.Response(200, json=[profile])

    oauth = LittleSkinClient("app", http=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        with pytest.raises(AccountError) as error:
            oauth.set_texture(credentials(), profile_id, "cape", None)
        assert error.value.error_code == "LITTLESKIN_WRITE_UNCONFIRMED"
        assert len(writes) == 1
    finally:
        oauth.close()
