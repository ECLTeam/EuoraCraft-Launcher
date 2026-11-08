# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件权限声明与校验模块。
#
# 公开接口：
#   - class PermissionScope — 权限作用域。
#   - class PermissionAction — 权限操作类型。
#   - class Permission — 单个权限声明。
#       - from_dict(data) -> Permission | None — 从字典解析权限声明，格式错误时返回 None。
#       - to_dict() -> dict[str, Any] — 转换为字典表示。
#       - matches(other) -> bool — 检查当前权限是否满足另一个权限请求。
#   - class PermissionManager — 权限管理器，负责收集与校验插件声明的权限。
#       - register_plugin_permissions(plugin_name, permissions) -> list[Permission] — 注册插件声明的权限，返回成功解析的权限列表。
#       - get_plugin_permissions(plugin_name) -> list[Permission] — 获取指定插件声明的权限列表。
#       - unregister_plugin(plugin_name) -> None — 移除指定插件已注册的权限声明。
#       - has_permission(plugin_name, permission) -> bool — 检查插件是否拥有指定权限。
#       - check_permission(plugin_name, permission) -> None — 校验插件是否拥有指定权限，缺失时抛出 PermissionError。
#       - clear() -> None — 清空所有已注册的权限声明。
# ============================================================

"""插件权限声明与校验模块。"""

from __future__ import annotations

from enum import Enum
from typing import Any


class PermissionScope(Enum):
    """
    权限作用域。
    """

    SETTINGS = "settings"
    EVENTS = "events"
    COMMANDS = "commands"
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    UI = "ui"
    INSTANCES = "instances"
    LAUNCH = "launch"
    ACCOUNTS = "accounts"
    CRASH = "crash"


class PermissionAction(Enum):
    """
    权限操作类型。
    """

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    SUBSCRIBE = "subscribe"
    EMIT = "emit"


class Permission:
    """
    单个权限声明。
    """

    def __init__(
        self,
        scope: PermissionScope,
        action: PermissionAction,
        resource: str = "*",
    ) -> None:
        self.scope = scope
        self.action = action
        self.resource = resource

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Permission | None:
        """
        从字典解析权限声明，格式错误时返回 None。

        :param data: 插件声明的权限字典
        :return: 解析后的权限对象，格式错误时返回 None
        """
        try:
            scope = PermissionScope(data.get("scope"))
            action = PermissionAction(data.get("action"))
        except (ValueError, KeyError):
            return None
        resource = data.get("resource", "*")
        if not isinstance(resource, str) or not resource:
            resource = "*"
        return cls(scope, action, resource)

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典表示。
        """
        return {
            "scope": self.scope.value,
            "action": self.action.value,
            "resource": self.resource,
        }

    def matches(self, other: Permission) -> bool:
        """检查当前权限是否满足另一个权限请求。

        支持两种通配：
        - ``*`` 匹配任意资源；
        - ``前缀:*`` 匹配以该前缀开头的任意资源，且前缀后必须是 ``:`` 分隔的下一级。
        """
        if self.scope != other.scope or self.action != other.action:
            return False
        if self.resource == "*":
            return True
        if self.resource.endswith(":*"):
            prefix = self.resource[:-2]
            return other.resource == prefix or other.resource.startswith(f"{prefix}:")
        return self.resource == other.resource

    def __hash__(self) -> int:
        return hash((self.scope, self.action, self.resource))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Permission):
            return NotImplemented
        return self.scope == other.scope and self.action == other.action and self.resource == other.resource

    def __repr__(self) -> str:
        return f"<Permission {self.scope.value}:{self.action.value}:{self.resource}>"


class PermissionManager:
    """
    权限管理器，负责收集与校验插件声明的权限。
    """

    def __init__(self) -> None:
        self._plugin_permissions: dict[str, set[Permission]] = {}  # 插件名 → 已解析的权限集合

    def register_plugin_permissions(self, plugin_name: str, permissions: list[dict[str, Any]]) -> list[Permission]:
        """
        注册插件声明的权限，返回成功解析的权限列表。

        :param plugin_name: 插件名称
        :param permissions: 插件声明的权限集合
        """
        parsed: set[Permission] = set()
        for item in permissions:
            permission = Permission.from_dict(item)
            if permission is not None:
                parsed.add(permission)
        self._plugin_permissions[plugin_name] = parsed
        return list(parsed)

    def get_plugin_permissions(self, plugin_name: str) -> list[Permission]:
        """
        获取指定插件声明的权限列表。

        :param plugin_name: 插件名称
        """
        return list(self._plugin_permissions.get(plugin_name, set()))

    def unregister_plugin(self, plugin_name: str) -> None:
        """
        移除指定插件已注册的权限声明。

        :param plugin_name: 插件名称
        """
        self._plugin_permissions.pop(plugin_name, None)

    def has_permission(self, plugin_name: str, permission: Permission) -> bool:
        """
        检查插件是否拥有指定权限。

        :param plugin_name: 插件名称
        :param permission: 需要检查的插件权限
        """
        plugin_permissions = self._plugin_permissions.get(plugin_name, set())
        return any(p.matches(permission) for p in plugin_permissions)

    def check_permission(self, plugin_name: str, permission: Permission) -> None:
        """
        校验插件是否拥有指定权限，缺失时抛出 PermissionError。

        :param plugin_name: 插件名称
        :param permission: 需要检查的插件权限
        :raises PermissionError: 插件缺少对应权限时抛出
        """
        if not self.has_permission(plugin_name, permission):
            raise PermissionError(
                f"插件 {plugin_name} 缺少权限 {permission.scope.value}:{permission.action.value}:{permission.resource}"
            )

    def clear(self) -> None:
        """
        清空所有已注册的权限声明。
        """
        self._plugin_permissions.clear()
