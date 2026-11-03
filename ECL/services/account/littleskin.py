# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：LittleSkin OAuth 协议客户端、本机授权回调及可取消的登录会话。
#           凭据持久化通过账户领域提供的提交函数完成，不向界面交付令牌。
#
# 公开接口：
#   - class LittleSkinCredentials — 持久化的 OAuth 凭据及所属授权方式。
#   - class LittleSkinProfile — 已校验的游戏角色。
#   - class LittleSkinMinecraftToken — 绑定角色的游戏令牌。
#   - class LittleSkinClient — 请求 LittleSkin 官方 OAuth 与角色接口。
#   - class LittleSkinCallback — 管理一次授权代码登录的回环监听器。
#   - class LittleSkinLogin — 管理登录任务、角色选择、取消和账户提交。
#   - class LittleSkinLoginSnapshot — 不含设备代码或令牌的登录进度。
# ============================================================

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic, time
from typing import Literal, ParamSpec, TypedDict, TypeVar
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from ECL.utils import AccountError, get_logger

call_parameters = ParamSpec("call_parameters")
call_result = TypeVar("call_result")


class LittleSkinProfile(BaseModel):
    """
    保存 LittleSkin 授权结果中可用于游戏的单个角色。
    """

    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    name: str = Field(min_length=1, max_length=256)


class LittleSkinCredentials(BaseModel):
    """
    保存刷新所需的 OAuth 凭据，不包含客户端密钥。
    """

    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    expires_at: float = Field(gt=0)
    client_id: str = Field(min_length=1)
    method: Literal["device_code", "authorization_code"]


class LittleSkinMinecraftToken(BaseModel):
    """
    校验可直接交给外置账户领域保存的游戏令牌和选中角色。
    """

    model_config = ConfigDict(strict=True)
    access_token: str = Field(alias="accessToken", min_length=1, repr=False)
    client_token: str = Field(alias="clientToken", min_length=1, repr=False)
    selected_profile: LittleSkinProfile = Field(alias="selectedProfile")


class _OAuthToken(BaseModel):
    model_config = ConfigDict(strict=True)
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0, le=31536000)


class _DeviceCode(BaseModel):
    model_config = ConfigDict(strict=True)
    user_code: str = Field(min_length=1, max_length=256)
    device_code: str = Field(min_length=1, repr=False)
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int = Field(gt=0, le=3600)
    interval: int = Field(gt=0, le=300)

    @field_validator("verification_uri", "verification_uri_complete")
    @classmethod
    def _official_page(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "open.littleskin.cn"
            or parsed.path != "/oauth/link"
            or parsed.fragment
        ):
            raise ValueError("授权页面不是 LittleSkin 官方地址")
        return value


class _OAuthError(AccountError):
    """
    保留轮询需要识别的 OAuth 错误类型，消息只使用安全的本地说明。
    """

    def __init__(self, oauth_error: str) -> None:
        self.oauth_error = oauth_error
        if oauth_error == "access_denied":
            message = "用户拒绝了 LittleSkin 授权"
        elif oauth_error in {"expired_token", "invalid_grant"}:
            message = "LittleSkin 授权已失效，请重新登录"
        elif oauth_error == "invalid_client":
            message = "LittleSkin 应用配置无效或尚未获得准入"
        elif oauth_error == "invalid_scope":
            message = "LittleSkin 拒绝所申请的权限，请确认应用已获角色读取和 Minecraft 令牌创建权限"
        else:
            message = "LittleSkin 授权失败，请重试"
        super().__init__(message, f"LITTLESKIN_{oauth_error.upper()}")


class LittleSkinClient:
    """
    通过固定的官方端点读取角色并兑换或刷新令牌。

    同步网络方法由登录服务在线程中调用；外置账户的同步刷新复用同一客户端。
    客户端不记录请求内容、不跟随重定向，关闭由所属账户服务负责。
    """

    authorization_scopes: str = "Yggdrasil.PlayerProfiles.Read Yggdrasil.MinecraftToken.Create"
    device_scopes: str = authorization_scopes + " offline_access"
    server_url: str = "https://littleskin.cn/api/yggdrasil"

    def __init__(self, client_id: str, client_secret: str = "", http: httpx.Client | None = None) -> None:
        self._request_lock = RLock()
        self._is_closed = False
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(20, connect=10),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "EuoraCraft-Launcher"},
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        """
        在不输出响应原文的前提下校验网络与 OAuth 业务失败。

        只向固定端点发送凭据，外部错误说明可能包含认证材料，不能直接呈现。
        """
        try:
            with self._request_lock:
                if self._is_closed:
                    raise AccountError("LittleSkin 认证服务已关闭", "LITTLESKIN_CLOSED")
                response = self.http.request(
                    method,
                    url,
                    data=data,
                    json=json,
                    headers={"Accept": "application/json", **(headers or {})},
                    follow_redirects=False,
                )
        except httpx.HTTPError as exc:
            raise AccountError("连接 LittleSkin 失败，请检查网络后重试", "LITTLESKIN_NETWORK_FAILED") from exc
        try:
            if len(response.content) > 1024 * 1024:
                raise ValueError("响应过大")
            result = response.json()
        except ValueError as exc:
            raise AccountError("LittleSkin 返回的数据无效", "LITTLESKIN_INVALID_RESPONSE") from exc
        if isinstance(result, dict) and isinstance(result.get("error"), str):
            error = result["error"]
            if error not in {
                "authorization_pending",
                "slow_down",
                "access_denied",
                "expired_token",
                "invalid_grant",
                "invalid_client",
                "invalid_scope",
                "invalid_request",
                "server_error",
            }:
                error = "server_error"
            raise _OAuthError(error)
        if not response.is_success or (isinstance(result, dict) and result.get("code", 0) not in (0, None)):
            raise AccountError("LittleSkin 拒绝了请求，请检查授权权限", "LITTLESKIN_REQUEST_REJECTED")
        return result

    def request_device_code(self) -> _DeviceCode:
        """
        请求设备代码对，结果只用于当前登录会话。

        :return: 经过类型和官方授权地址校验的设备代码对
        """
        try:
            return _DeviceCode.model_validate(
                self._request(
                    "POST",
                    "https://open.littleskin.cn/oauth/device_code",
                    data={"client_id": self.client_id, "scope": self.device_scopes},
                )
            )
        except ValidationError as exc:
            raise AccountError("LittleSkin 设备授权响应无效", "LITTLESKIN_INVALID_RESPONSE") from exc

    def token(
        self, method: Literal["device_code", "authorization_code"], values: dict[str, str]
    ) -> LittleSkinCredentials:
        """
        兑换或刷新 OAuth 令牌，密钥仅用于授权代码流。

        :param method: 原始授权方式，决定端点及客户端认证
        :param values: 当前操作所需的授权代码、设备代码或刷新令牌
        :return: 可由账户领域原子保存的新凭据
        """
        payload = {**values, "client_id": self.client_id}
        host = "open.littleskin.cn" if method == "device_code" else "littleskin.cn"
        if method == "authorization_code":
            if not self.client_secret:
                raise AccountError("LittleSkin 客户端密钥未配置", "LITTLESKIN_CONFIG_REQUIRED")
            payload["client_secret"] = self.client_secret
        try:
            result = _OAuthToken.model_validate(self._request("POST", f"https://{host}/oauth/token", data=payload))
        except ValidationError as exc:
            raise AccountError("LittleSkin 令牌响应无效", "LITTLESKIN_INVALID_RESPONSE") from exc
        return LittleSkinCredentials(
            access_token=result.access_token,
            refresh_token=result.refresh_token,
            expires_at=time() + result.expires_in,
            client_id=self.client_id,
            method=method,
        )

    def profiles(self, credentials: LittleSkinCredentials) -> list[LittleSkinProfile]:
        """
        读取本次授权允许使用的角色，不解码访问令牌。

        :param credentials: 已获得用户授权的访问凭据
        :return: 已校验的角色列表
        """
        result = self._request(
            "GET",
            f"{self.server_url}/sessionserver/session/minecraft/profile",
            headers={"Authorization": f"Bearer {credentials.access_token}", "Accept": "application/json"},
        )
        try:
            profiles = TypeAdapter(list[LittleSkinProfile]).validate_python(result, strict=True)
            if len({profile.id.lower() for profile in profiles}) != len(profiles):
                raise ValueError("重复角色")
            return profiles
        except (ValidationError, ValueError) as exc:
            raise AccountError("LittleSkin 角色列表无效", "LITTLESKIN_INVALID_RESPONSE") from exc

    def minecraft_token(self, credentials: LittleSkinCredentials, profile_id: str) -> LittleSkinMinecraftToken:
        """
        为已授权角色兑换独立的游戏令牌并核对角色归属。

        :param credentials: 当前有效的 OAuth 凭据
        :param profile_id: 调用方已从授权角色列表中选出的 UUID
        :return: 绑定指定角色的游戏令牌
        """
        try:
            result = LittleSkinMinecraftToken.model_validate(
                self._request(
                    "POST",
                    f"{self.server_url}/authserver/oauth",
                    json={"uuid": profile_id},
                    headers={"Authorization": f"Bearer {credentials.access_token}", "Accept": "application/json"},
                )
            )
        except ValidationError as exc:
            raise AccountError("LittleSkin 游戏令牌响应无效", "LITTLESKIN_INVALID_RESPONSE") from exc
        if result.selected_profile.id.lower() != profile_id.lower():
            raise AccountError("LittleSkin 返回的角色与所选角色不一致", "LITTLESKIN_PROFILE_MISMATCH")
        return result

    def refresh(self, credentials: LittleSkinCredentials) -> LittleSkinCredentials:
        """
        使用原授权方式刷新令牌，拒绝应用 ID 不匹配的凭据。

        :param credentials: 需要刷新的账户凭据
        :return: 轮换后的新凭据，旧凭据在服务端立即失效
        """
        if credentials.client_id != self.client_id:
            raise AccountError("LittleSkin 应用配置已改变，请重新登录", "LITTLESKIN_CONFIG_CHANGED")
        return self.token(
            credentials.method, {"grant_type": "refresh_token", "refresh_token": credentials.refresh_token}
        )

    def close(self) -> None:
        """
        在所属登录任务停止后关闭 HTTP 连接池。
        """
        with self._request_lock:
            self._is_closed = True
            self.http.close()


class LittleSkinCallback:
    """
    为一次授权代码流程提供受 state 保护的临时回环监听。

    只绑定配置的 IPv4 回环地址；请求有大小和时间限制，不生成访问日志。
    """

    def __init__(self, redirect_uri: str) -> None:
        self.uri = urlsplit(redirect_uri)
        try:
            is_valid = (
                self.uri.scheme == "http"
                and self.uri.hostname == "127.0.0.1"
                and self.uri.port is not None
                and self.uri.port > 0
                and self.uri.path.startswith("/")
                and not self.uri.username
                and not self.uri.password
                and not self.uri.query
                and not self.uri.fragment
                and not any(character.isspace() for character in redirect_uri)
            )
        except ValueError:
            is_valid = False
        if not is_valid:
            raise AccountError(
                "LittleSkin 回调地址必须是包含端口的本机回环 HTTP 地址", "LITTLESKIN_INVALID_REDIRECT_URI"
            )
        self.state = secrets.token_urlsafe(32)
        self.code: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.server: asyncio.Server | None = None
        self._connections: set[asyncio.Task] = set()

    async def start(self) -> None:
        """
        绑定指定地址，端口占用时明确失败且不自动更换端口。
        """
        try:
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.uri.port, limit=8192)
        except OSError as exc:
            raise AccountError(
                "LittleSkin 回调端口无法使用，请检查配置或端口占用", "LITTLESKIN_CALLBACK_UNAVAILABLE"
            ) from exc

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        校验来源、路径与一次性 state，非法请求不消费授权结果。
        """
        task = asyncio.current_task()
        self._connections.add(task)
        status = "400 Bad Request"
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = request.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            headers: dict[str, list[str]] = {}
            for line in lines[1:]:
                if line:
                    key, value = line.split(":", 1)
                    headers.setdefault(key.lower(), []).append(value.strip())
            parsed = urlsplit(target)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            if (
                method != "GET"
                or version != "HTTP/1.1"
                or parsed.scheme
                or parsed.netloc
                or parsed.fragment
                or parsed.path != self.uri.path
                or headers.get("host") != [self.uri.netloc]
                or headers.get("transfer-encoding")
                or headers.get("content-length", ["0"]) != ["0"]
                or query.get("state") != [self.state]
                or self.code.done()
            ):
                raise ValueError("回调请求无效")
            if "error" in query and len(query["error"]) == 1 and "code" not in query:
                error = query["error"][0]
                self.code.set_exception(_OAuthError("access_denied" if error == "access_denied" else "server_error"))
            elif len(query.get("code", [])) == 1 and query["code"][0] and "error" not in query:
                self.code.set_result(query["code"][0])
            else:
                raise ValueError("缺少授权结果")
            status = "200 OK"
        except (ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, UnicodeError):
            pass  # 非法外部探测只返回固定失败页，不记录可能包含授权代码的请求。
        finally:
            body = b"Return to EuoraCraft Launcher." if status == "200 OK" else b"Invalid authorization callback."
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                    "Content-Security-Policy: default-src 'none'\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
                + body
            )
            with suppress(ConnectionError, OSError):
                await writer.drain()
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
            self._connections.discard(task)

    async def close(self) -> None:
        """
        停止接收回调并等待已有连接结束，释放未消费的授权结果。
        """
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        if not self.code.done():
            self.code.cancel()
        elif not self.code.cancelled():
            self.code.exception()  # 提前拒绝后取消登录时也消费 Future 异常，避免后台警告。


class LittleSkinLoginSnapshot(TypedDict):
    """
    只包含界面需要的授权进度，不包含设备代码或账户凭据。
    """

    session_id: str
    status: Literal["starting", "pending", "selecting", "ready", "saving", "completed", "error", "cancelled"]
    method: Literal["device_code", "authorization_code"]
    user_code: str
    verification_uri: str
    expires_at: float
    profiles: list[dict[str, str]]
    error_code: str


@dataclass(slots=True)
class _LoginSession:
    session_id: str
    method: Literal["device_code", "authorization_code"]
    status: Literal["starting", "pending", "selecting", "ready", "saving", "completed", "error", "cancelled"] = (
        "starting"
    )
    user_code: str = ""
    verification_uri: str = ""
    expires_at: float = 0.0
    profiles: list[LittleSkinProfile] = field(default_factory=list)
    credentials: LittleSkinCredentials | None = field(default=None, repr=False)
    minecraft_token: LittleSkinMinecraftToken | None = field(default=None, repr=False)
    task: asyncio.Task | None = None
    cancellation: asyncio.Task | None = None
    callback: LittleSkinCallback | None = None
    error_code: str = ""


class LittleSkinLogin:
    """
    为账户服务维护唯一的 LittleSkin 登录任务和无凭据进度。

    所有状态变更在宿主 asyncio 循环内完成；同步网络通过受控线程执行，取消时等待
    实际请求结束。只有 complete 才调用账户领域提供的提交函数，取消不会保存账户。
    """

    default_redirect_uri: str = "http://127.0.0.1:34127/oauth/littleskin/callback"

    def __init__(
        self,
        client: LittleSkinClient,
        redirect_uri: str,
        commit: Callable[[LittleSkinCredentials, LittleSkinMinecraftToken], dict[str, object]],
        emit: Callable[[LittleSkinLoginSnapshot], None],
    ) -> None:
        self.client = client
        self.redirect_uri = redirect_uri
        self._commit = commit
        self._emit = emit
        self._session: _LoginSession | None = None
        self._completion: asyncio.Task[dict[str, object]] | None = None
        self.logger = get_logger("LittleSkinLogin")

    def config(self) -> dict[str, bool]:
        """
        返回各授权方式的可用性，不交付应用 ID、密钥或回调配置。

        :return: 客户端配置可用状态
        """
        return {
            "device_code": bool(self.client.client_id),
            "authorization_code": bool(self.client.client_id and self.client.client_secret),
            "needs_client_id": not bool(self.client.client_id),
        }

    def snapshot(self, session_id: str) -> LittleSkinLoginSnapshot:
        """
        读取指定会话进度，拒绝旧页面对新会话的访问。

        :param session_id: 创建登录时获得的会话标识
        :return: 无凭据的界面快照
        """
        session = self._require(session_id)
        return {
            "session_id": session.session_id,
            "status": session.status,
            "method": session.method,
            "user_code": session.user_code,
            "verification_uri": session.verification_uri,
            "expires_at": session.expires_at,
            "profiles": [profile.model_dump() for profile in session.profiles],
            "error_code": session.error_code,
        }

    def _require(self, session_id: str) -> _LoginSession:
        if self._session is None or self._session.session_id != session_id:
            raise AccountError("LittleSkin 登录会话已失效", "LITTLESKIN_SESSION_EXPIRED")
        return self._session

    def _notify(self, session: _LoginSession) -> None:
        if self._session is session:
            self._emit(self.snapshot(session.session_id))

    async def _call(
        self,
        function: Callable[call_parameters, call_result],
        *args: call_parameters.args,
        **kwargs: call_parameters.kwargs,
    ) -> call_result:
        """
        将同步外部操作移到线程，取消时仍等待实际请求结束再清理连接。
        """
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise

    async def start(self, method: Literal["device_code", "authorization_code"]) -> LittleSkinLoginSnapshot:
        """
        创建有唯一身份的登录任务，重复启动在当前任务结束前明确拒绝。

        :param method: 用户选择的 OAuth 授权方式
        :return: 初始会话快照，后续通过事件和轮询更新
        """
        if not self.config()[method]:
            raise AccountError("LittleSkin 登录配置不完整", "LITTLESKIN_CONFIG_REQUIRED")
        if self._session and self._session.status not in {"completed", "cancelled", "error"}:
            raise AccountError("LittleSkin 登录正在进行", "LITTLESKIN_LOGIN_BUSY")
        if self._session and self._session.task and not self._session.task.done():
            raise AccountError("LittleSkin 登录正在结束，请稍后重试", "LITTLESKIN_LOGIN_BUSY")
        session = _LoginSession(uuid4().hex, method)
        self._session = session
        session.task = asyncio.create_task(self._run(session))
        return self.snapshot(session.session_id)

    async def _run(self, session: _LoginSession) -> None:
        """
        执行实际授权任务，任何失败都结束等待并清除临时认证材料。
        """
        self.logger.info("LittleSkin 登录开始")
        try:
            if session.method == "device_code":
                await self._authorize_device(session)
            else:
                await self._authorize_browser(session)
            session.profiles = await self._call(self.client.profiles, session.credentials)
            if not session.profiles:
                raise AccountError("请先在 LittleSkin 创建游戏角色", "LITTLESKIN_NO_PROFILES")
            self.logger.info("LittleSkin 授权完成，正在选择游戏角色")
            session.status = "selecting"
            if len(session.profiles) == 1:
                await self._exchange(session, session.profiles[0].id)
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.credentials = None
            session.minecraft_token = None
            self.logger.info("LittleSkin 登录已取消")
            raise
        except Exception as exc:
            self._fail(session, exc)
        finally:
            if session.callback:
                await session.callback.close()
            self._notify(session)

    async def _authorize_device(self, session: _LoginSession) -> None:
        """
        按服务端间隔和截止时间轮询设备授权，降速不突破会话有效期。
        """
        device = await self._call(self.client.request_device_code)
        session.user_code = device.user_code
        session.verification_uri = device.verification_uri_complete or device.verification_uri
        session.expires_at = time() + device.expires_in
        session.status = "pending"
        self._notify(session)
        interval = device.interval
        deadline = monotonic() + device.expires_in
        while True:
            remaining = deadline - monotonic()
            if remaining <= interval:
                await asyncio.sleep(max(0, remaining))
                raise AccountError("LittleSkin 授权已超时", "LITTLESKIN_EXPIRED_TOKEN")
            await asyncio.sleep(interval)
            try:
                session.credentials = await self._call(
                    self.client.token,
                    session.method,
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device.device_code,
                    },
                )
                break
            except _OAuthError as exc:
                if exc.oauth_error == "slow_down":
                    interval += 5
                elif exc.oauth_error != "authorization_pending":
                    raise

    async def _authorize_browser(self, session: _LoginSession) -> None:
        """
        等待一次性回环授权并关闭监听，再向官方端点兑换令牌。
        """
        session.callback = LittleSkinCallback(self.redirect_uri)
        await session.callback.start()
        session.verification_uri = "https://littleskin.cn/oauth/authorize?" + urlencode(
            {
                "client_id": self.client.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": self.client.authorization_scopes,
                "state": session.callback.state,
            }
        )
        session.expires_at = time() + 300
        session.status = "pending"
        self._notify(session)
        try:
            code = await asyncio.wait_for(asyncio.shield(session.callback.code), 300)
        except TimeoutError as exc:
            raise AccountError("LittleSkin 授权已超时", "LITTLESKIN_EXPIRED_TOKEN") from exc
        await session.callback.close()
        session.credentials = await self._call(
            self.client.token,
            session.method,
            {
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
                "code": code,
            },
        )

    def _fail(self, session: _LoginSession, exc: Exception) -> None:
        session.status = "error"
        session.error_code = exc.error_code if isinstance(exc, AccountError) else "LITTLESKIN_LOGIN_FAILED"
        session.credentials = None
        session.minecraft_token = None
        if isinstance(exc, AccountError):
            method_name = "授权码登录" if session.method == "device_code" else "浏览器登录"
            self.logger.warning("LittleSkin %s失败：%s；错误码：%s", method_name, str(exc), session.error_code)
        else:
            self.logger.exception("LittleSkin 登录发生异常")

    async def _exchange(self, session: _LoginSession, profile_id: str) -> None:
        session.status = "starting"
        self._notify(session)
        session.minecraft_token = await self._call(self.client.minecraft_token, session.credentials, profile_id)
        session.status = "ready"

    async def select(self, session_id: str, profile_id: str) -> LittleSkinLoginSnapshot:
        """
        仅为当前授权中存在的角色兑换令牌，重复选择不会并行兑换。

        :param session_id: 当前登录会话
        :param profile_id: 用户选择的授权角色 UUID
        :return: 更新后的无凭据快照
        """
        session = self._require(session_id)
        if (
            session.status != "selecting"
            or session.cancellation is not None
            or profile_id not in {profile.id for profile in session.profiles}
        ):
            raise AccountError("LittleSkin 角色选择无效", "LITTLESKIN_INVALID_PROFILE")
        session.status = "starting"
        session.task = asyncio.create_task(self._select(session, profile_id))
        return self.snapshot(session_id)

    async def _select(self, session: _LoginSession, profile_id: str) -> None:
        """
        让角色令牌兑换与授权轮询使用相同的实际取消和错误清理语义。
        """
        try:
            await self._exchange(session, profile_id)
        except asyncio.CancelledError:
            session.status = "cancelled"
            session.credentials = None
            session.minecraft_token = None
            raise
        except Exception as exc:
            self._fail(session, exc)
        finally:
            self._notify(session)

    async def complete(self, session_id: str) -> dict[str, object]:
        """
        只提交已准备好的角色账户，文件保存期间拒绝取消或重复提交。

        :param session_id: 当前登录会话
        :return: 已持久化账户的公开信息
        """
        session = self._require(session_id)
        if (
            session.status != "ready"
            or session.cancellation is not None
            or session.credentials is None
            or session.minecraft_token is None
        ):
            raise AccountError("LittleSkin 登录尚未准备完成", "LITTLESKIN_NOT_READY")
        session.status = "saving"
        self._notify(session)
        self._completion = asyncio.create_task(self._complete(session))
        return await asyncio.shield(self._completion)

    async def _complete(self, session: _LoginSession) -> dict[str, object]:
        """
        让落盘任务独立于 IPC 等待方取消而完成，保持最终状态与实际保存一致。
        """
        try:
            account = await self._call(self._commit, session.credentials, session.minecraft_token)
        except Exception as exc:
            session.status = "ready"  # 保留认证结果，磁盘恢复后可重新提交。
            self.logger.warning("LittleSkin 账户保存失败，可重试")
            raise AccountError("LittleSkin 账户保存失败，请重试", "LITTLESKIN_SAVE_FAILED") from exc
        session.status = "completed"
        session.credentials = None
        session.minecraft_token = None
        self.logger.info("LittleSkin 登录完成，账户已保存")
        self._notify(session)
        return account

    async def cancel(self, session_id: str) -> LittleSkinLoginSnapshot:
        """
        取消当前任务并等待网络请求与回调监听实际结束，随后清理凭据。

        :param session_id: 需要取消的会话
        :return: 实际停止后的无凭据快照
        """
        session = self._require(session_id)
        if session.status == "saving":
            raise AccountError("LittleSkin 账户正在保存，请稍后再试", "LITTLESKIN_LOGIN_BUSY")
        if session.status == "completed":
            return self.snapshot(session_id)
        if session.cancellation is None:
            session.cancellation = asyncio.create_task(self._cancel(session))
        await asyncio.shield(session.cancellation)
        return self.snapshot(session_id)

    async def _cancel(self, session: _LoginSession) -> None:
        """
        合并并发取消，只向实际执行任务发送一次取消信号并等待其释放资源。
        """
        was_running = session.task is not None and not session.task.done()
        if session.task and not session.task.done():
            session.task.cancel()
            with suppress(asyncio.CancelledError):
                await session.task
        session.status = "cancelled"
        session.credentials = None
        session.minecraft_token = None
        self._notify(session)
        if not was_running:
            self.logger.info("LittleSkin 登录已取消")

    async def close(self) -> None:
        """
        在账户服务退出时结束当前会话并关闭所属 HTTP 客户端。
        """
        if self._completion and not self._completion.done():
            with suppress(AccountError):
                await asyncio.shield(self._completion)
        if self._session and self._session.status != "completed":
            await self.cancel(self._session.session_id)
        await asyncio.to_thread(self.client.close)
