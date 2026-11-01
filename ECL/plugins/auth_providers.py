# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：自定义账户/登录扩展点：插件注册第三方认证提供方并参与凭据解析。
#
# 公开接口：
#   - class AuthField — 动态登录表单的单个输入项。
#   - class AuthProvider — 插件注册的认证提供方。
#       - to_dict() -> dict[str, Any] — 返回前端可安全消费的提供方定义。
#   - class AuthProviderRegistry — 维护插件认证提供方，供账户服务聚合展示与凭据解析。
#   - sanitize_private_auth_data — 校验仅原提供方可读取的不透明数据。
#       - register(owner, provider_id, title, fields, authenticate, resolve_credentials, description=…) -> None — 注册或原位更新一个认证提供方。
#       - unregister_owner(owner) -> None — 撤销指定插件注册的全部提供方。
#       - get(provider_id) -> AuthProvider | None — 按稳定标识获取认证提供方。
#       - list_providers() -> list[AuthProvider] — 按注册顺序返回全部提供方。
# ============================================================

"""
提供自定义账户表单、认证返回值校验与所属提供方登记。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ECL.plugin_api.contracts import JsonValue
from ECL.utils import get_logger

# 第三方认证返回值上限：账户标识与不透明负载都要有界。
limit_account_text = 256
limit_account_data_fields = 16
limit_private_text = 16_384
user_types = frozenset({"msa", "yggdrasil", "legacy"})


def sanitize_account_payload(payload: object) -> dict[str, JsonValue]:
    """
    校验第三方认证回调返回的账户信息，越界或缺失时整体拒绝。

    :param payload: 插件回调返回值
    :return: 可直接交给账户服务的账户信息
    :raises ValueError: 必需字段缺失、类型不符或超长时抛出
    """
    if not isinstance(payload, dict):
        raise ValueError("认证返回值必须是对象")
    if not payload.keys() <= {"id", "alias", "uuid", "data", "skinUrl"}:
        raise ValueError("认证返回值包含未声明字段")
    result: dict[str, JsonValue] = {}
    for key in ("id", "alias", "uuid"):
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > limit_account_text:
            raise ValueError(f"认证返回值的 {key} 无效")
        result[key] = value
    skin_url = payload.get("skinUrl")
    if skin_url is not None:
        if not isinstance(skin_url, str) or not skin_url or len(skin_url) > limit_account_text:
            raise ValueError("认证返回的皮肤地址无效")
        result["skinUrl"] = skin_url
    data = payload.get("data")
    if data is not None:
        result["data"] = sanitize_private_auth_data(data)
    return result


def sanitize_private_auth_data(payload: object) -> dict[str, str]:
    """
    校验仅原认证提供方可读取的不透明持久数据，保留凭据的完整内容。

    不截断令牌或转换字段类型；超过约定预算时整体失败，避免保存损坏凭据。

    :param payload: 提供方返回或持久化读取的私有数据
    :return: 独立的字符串字段快照
    :raises ValueError: 数据形状或预算不符合约定时抛出
    """
    if not isinstance(payload, dict) or len(payload) > limit_account_data_fields:
        raise ValueError("认证提供方的持久数据无效")
    result: dict[str, str] = {}
    for key, value in payload.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or not isinstance(value, str)
            or len(value) > limit_private_text
        ):
            raise ValueError("认证提供方的持久数据无效")
        result[key] = value
    return result


def sanitize_credentials_payload(payload: object) -> dict[str, str]:
    """
    校验启动时的凭据解析结果，凭据只交给账户服务，不写日志。

    :param payload: 插件回调返回值
    :return: 账户服务可消费的凭据字典
    :raises ValueError: 必需字段缺失或 user_type 不在允许范围时抛出
    """
    if not isinstance(payload, dict):
        raise ValueError("凭据解析返回值必须是对象")
    if not payload.keys() <= {"player_name", "uuid", "user_type", "access_token", "auth_server"}:
        raise ValueError("凭据解析返回值包含未声明字段")
    result: dict[str, str] = {}
    for key in ("player_name", "uuid", "user_type", "access_token"):
        value = payload.get(key)
        limit = limit_private_text if key == "access_token" else limit_account_text
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError(f"凭据解析返回值的 {key} 无效")
        result[key] = value
    if result["user_type"] not in user_types:
        raise ValueError("凭据解析返回的 user_type 不受支持")
    auth_server = payload.get("auth_server")
    if auth_server is not None:
        if not isinstance(auth_server, str) or not auth_server or len(auth_server) > limit_account_text:
            raise ValueError("凭据解析返回的认证地址无效")
        result["auth_server"] = auth_server
    return result


@dataclass(frozen=True)
class AuthField:
    """
    动态登录表单的单个输入项。
    """

    key: str  # 提交给认证回调的字段键。
    label: str  # 面向用户的字段标签。
    type: str = "text"  # 前端输入控件类型。
    required: bool = True  # 是否必须填写。
    placeholder: str = ""  # 输入提示文本。


@dataclass
class AuthProvider:
    """
    插件注册的认证提供方。

    ``authenticate`` 接收前端提交的字段值字典，返回账户原始信息：
    ``{"id", "alias", "uuid"}`` 必填，``data`` 可保存令牌等不透明负载，
    ``skinUrl`` 可选。``resolve_credentials`` 在启动时接收自身账户摘要及私有 ``data`` 并返回
    ``{"player_name", "uuid", "user_type", "access_token", "auth_server"?}``，
    其中 ``user_type`` 必须为 ``msa`` / ``yggdrasil`` / ``legacy`` 之一。
    """

    id: str  # 提供方稳定标识。
    title: str  # 前端显示名称。
    fields: list[AuthField]  # 动态登录表单字段。
    authenticate: Callable[[Mapping[str, Any]], dict[str, Any]]  # 认证回调。
    resolve_credentials: Callable[[dict[str, Any]], dict[str, str]]  # 启动凭据解析回调。
    owner: str = ""  # 注册此提供方的插件名。
    description: str = ""  # 可选的用户说明。

    def to_dict(self) -> dict[str, Any]:
        """
        返回前端可安全消费的提供方定义。

        :return: 不包含认证回调的表单元数据
        """
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "fields": [
                {
                    "key": item.key,
                    "label": item.label,
                    "type": item.type,
                    "required": item.required,
                    "placeholder": item.placeholder,
                }
                for item in self.fields
            ],
        }


class AuthProviderRegistry:
    """
    维护插件认证提供方，供账户服务聚合展示与凭据解析。
    """

    def __init__(self) -> None:
        self._providers: dict[str, AuthProvider] = {}  # 按标识索引的提供方。
        self._logger = get_logger("AuthProviderRegistry")  # 扩展点日志器。

    def register(
        self,
        owner: str,
        provider_id: str,
        title: str,
        fields: list[AuthField],
        authenticate: Callable[[Mapping[str, Any]], dict[str, Any]],
        resolve_credentials: Callable[[dict[str, Any]], dict[str, str]],
        description: str = "",
    ) -> None:
        """
        注册或原位更新一个认证提供方。

        :param owner: 插件名
        :param provider_id: 稳定标识，最终账户 ID 为 ``plugin:<provider_id>:<account_id>``
        :param title: 面向用户的显示名称
        :param fields: 动态登录表单字段
        :param authenticate: 认证回调
        :param resolve_credentials: 启动凭据解析回调
        :param description: 可选描述
        """
        self._providers[provider_id] = AuthProvider(
            id=provider_id,
            title=title,
            fields=list(fields),
            authenticate=authenticate,
            resolve_credentials=resolve_credentials,
            owner=owner,
            description=description,
        )

    def unregister_owner(self, owner: str) -> None:
        """
        撤销指定插件注册的全部提供方。
        """
        for provider_id in [pid for pid, provider in self._providers.items() if provider.owner == owner]:
            self._providers.pop(provider_id, None)

    def get(self, provider_id: str) -> AuthProvider | None:
        """
        按稳定标识获取认证提供方。

        :param provider_id: 提供方标识
        :return: 已注册的提供方；不存在时为 None
        """
        return self._providers.get(provider_id)

    def list_providers(self) -> list[AuthProvider]:
        """
        按注册顺序返回全部提供方。
        """
        return list(self._providers.values())
