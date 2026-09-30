# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件注册表：路由/槽位/事件订阅/前端组件的集中查询。
#
# 公开接口：
#   - class PluginRegistry — 负责插件注册与扩展点收集，涵盖路由、设置、命令与 Vue 注入等条目。
#       - get_plugin(name) -> Plugin | None — 获取插件。
#       - get_plugin_metadata(name) -> dict | None — 获取目录或归档插件的清单元数据。
#       - subscribe_event(plugin, event, handler) -> None — 统一注册插件的事件订阅，自动以插件名作为所有者标识。
#       - list_plugins() -> list[dict[str, Any]] — 获取插件列表。
#       - get_routes() -> list[dict[str, Any]] — 获取插件路由列表。
#       - get_slots() -> dict[str, list[dict[str, str]]] — 返回按插槽分组的插件 HTML 注入内容。
#       - get_vue_slots() -> dict[str, list[dict[str, Any]]] — 返回按插槽分组的插件 Vue 组件定义。
#       - get_vue_components() -> dict[str, dict[str, Any]] — 返回所有已注册 Vue 组件定义的浅拷贝。
#       - get_vue_routes() -> list[dict[str, Any]] — 获取已注册的 Vue 路由列表。
#       - get_settings(name) -> dict[str, Any] — 返回插件设置结构及当前持久化值。
#       - update_setting(name, key, value) -> PluginActionResult — 更新一个已声明的插件设置，并返回保存结果。
#       - call_command(command, params=…, timeout=…) -> Any — 在线程池中调用 ``插件名:命令名``，失败或超时时抛出 ``PluginCommandError``。
# ============================================================

from __future__ import annotations

import math
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from ECL.plugins.plugin import Plugin

from .base import _PluginState
from .contracts import PluginAction, PluginActionResult, PluginCommandError


def _plugin_entry(
    name: str,
    metadata: Any,
    status: str,
    error: Any = None,
    *,
    permissions: list[dict[str, Any]] | None = None,
    services: list[str] | None = None,
    settings: list[str] | None = None,
    is_system: bool = False,
) -> dict[str, Any]:
    # 构建前端插件列表条目，统一已加载/依赖跳过/禁用/实例化失败四类来源的字典结构。
    if isinstance(metadata, Plugin):
        title = metadata.title
        version = metadata.version
        description = metadata.description
        author = metadata.author
        dependencies = metadata.metadata.get("dependencies", {})
        services = list(metadata._commands.keys())
        settings = list(metadata._settings.keys())
        is_system = getattr(metadata, "is_system", False)
        contributes = metadata.metadata.get("contributes", {})
    elif isinstance(metadata, dict):
        title = metadata.get("title", name)
        version = metadata.get("version", "")
        description = metadata.get("description", "")
        author = metadata.get("author", "")
        dependencies = metadata.get("dependencies", {})
        contributes = metadata.get("contributes", {})
    else:
        title = name
        version = ""
        description = ""
        author = ""
        dependencies = {}
        contributes = {}
    return {
        "name": name,
        "title": title,
        "version": version,
        "description": description,
        "author": author,
        "icon": "",
        "status": status,
        "error": error,
        "dependencies": dependencies,
        "contributes": contributes if isinstance(contributes, dict) else {},
        "permissions": permissions or [],
        "services": services or [],
        "settings": settings or [],
        "is_system": is_system,
    }


class PluginRegistry(_PluginState):
    """
    负责插件注册与扩展点收集，涵盖路由、设置、命令与 Vue 注入等条目。
    """

    def _register_route(self, plugin: Plugin, path: str, title: str, icon: str) -> None:
        # 注册前端路由，同一插件重复注册相同路径时先移除旧条目。
        # 同一插件重复注册相同路径时先移除旧条目，避免路由列表出现重复
        self._routes = [r for r in self._routes if not (r["plugin"] == plugin.name and r["path"] == path)]
        self._routes.append({"plugin": plugin.name, "path": path, "title": title, "icon": icon})
        self.events.emit("plugin:route_registered", plugin.name, path, title, icon)

    def get_plugin(self, name: str) -> Plugin | None:
        """
        获取插件。

        :param name: 插件名称
        """
        return self._plugins.get(name)

    def get_plugin_metadata(self, name: str) -> dict[str, Any] | None:
        """
        读取已加载目录插件或已恢复归档插件的元数据，不导入归档代码。

        :param name: 插件名称
        :return: 清单副本；插件不存在时返回 None
        """
        plugin = self._plugins.get(name)
        if plugin is not None:
            return dict(plugin.metadata)
        metadata = self._package_metadata(name)
        return metadata

    def subscribe_event(self, plugin: Plugin, event: str, handler: Any) -> None:
        """
        统一注册插件的事件订阅，自动以插件名作为所有者标识。
        插件自身无需也不能指定所有者，由插件管理器统一注入。
        :param plugin: 插件实例
        :param event: 事件名称
        :param handler: 回调函数
        """
        self.events.subscribe(event, handler, owner=plugin.name)

    def _append_unloaded_candidate_entries(self, result: list[dict[str, Any]], loaded_names: set[str]) -> None:
        # 安全模式下补充「已发现但未加载」的用户插件条目，元数据从 plugin.json 读取。
        error_names = set(self._plugin_errors.keys()) | set(self._dependency_resolution.errors.keys())
        pending_names = (
            self._candidate_map.keys()
            - loaded_names
            - self._disabled_plugins
            - set(self._dependency_resolution.skipped)
            - error_names
        )
        for name in sorted(pending_names):
            candidate = self._candidate_map[name]
            if candidate.get("is_system", False):
                continue
            result.append(
                _plugin_entry(
                    name,
                    candidate.get("metadata", {}),
                    "unloaded",
                    None,
                )
            )

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        获取插件列表。

        """
        result = []
        for name, plugin in self._plugins.items():
            if getattr(plugin, "is_system", False) is True:
                continue
            result.append(
                _plugin_entry(
                    name,
                    plugin,
                    self._status.get(name, "unloaded"),
                    self._plugin_errors.get(name) or self._dependency_resolution.errors.get(name),
                    permissions=[p.to_dict() for p in self._permission_manager.get_plugin_permissions(name)],
                )
            )
        # 补充因依赖错误未被加载的插件条目，便于前端展示
        loaded_names = set(self._plugins.keys()) | set(self._package_entries)
        for name in sorted(self._dependency_resolution.skipped - loaded_names - self._disabled_plugins):
            if self._candidate_map.get(name, {}).get("is_system", False):
                continue
            result.append(
                _plugin_entry(
                    name,
                    None,
                    "unloaded",
                    self._dependency_resolution.errors.get(name),
                )
            )
        # 补充安全模式（--disable-plugins）下未加载的候选插件条目，使其可见并可按需启用
        if not self._startup_auto_enable:
            self._append_unloaded_candidate_entries(result, loaded_names)
        # 补充被禁用且未实例化的插件条目，仍从 plugin.json 读取元数据供前端展示
        for name in sorted((self._disabled_plugins & self._candidate_map.keys()) - loaded_names):
            candidate = self._candidate_map[name]
            if candidate.get("is_system", False):
                continue
            result.append(
                _plugin_entry(
                    name,
                    candidate.get("metadata", {}),
                    "disabled",
                    None,
                    permissions=[p.to_dict() for p in self._permission_manager.get_plugin_permissions(name)],
                    is_system=candidate.get("is_system", False),
                )
            )
        # 补充实例化失败的插件条目
        for name in sorted(set(self._plugin_errors.keys()) - loaded_names):
            candidate = self._candidate_map.get(name, {})
            if candidate.get("is_system", False):
                continue
            result.append(
                _plugin_entry(
                    name,
                    candidate.get("metadata", {}),
                    self._status.get(name, "error"),
                    self._plugin_errors.get(name),
                    permissions=[p.to_dict() for p in self._permission_manager.get_plugin_permissions(name)],
                    is_system=candidate.get("is_system", False),
                )
            )
        self._append_package_entries(result)
        return result

    def _append_package_entries(self, result: list[dict[str, Any]]) -> None:
        with self._package_lock:
            package_entries = tuple((name, dict(entry)) for name, entry in sorted(self._package_entries.items()))
        for name, package_entry in package_entries:
            if name in self._plugins:
                continue
            result.append(
                _plugin_entry(
                    name,
                    package_entry["metadata"],
                    package_entry["status"],
                    package_entry["error"]
                    or self._plugin_errors.get(name)
                    or self._dependency_resolution.errors.get(name),
                    services=package_entry.get("services", []),
                )
            )
        for entry in result:
            package_entry = self._package_entries.get(entry["name"])
            if package_entry is not None:
                entry["pending_restart"] = bool(package_entry.get("pending_restart"))
                entry["installed_version"] = package_entry["metadata"].get("version", "")
                if package_entry.get("error") and not entry.get("error"):
                    entry["error"] = package_entry["error"]

    def get_routes(self) -> list[dict[str, Any]]:
        """
        获取插件路由列表。
        """
        return list(self._routes)

    def get_slots(self) -> dict[str, list[dict[str, str]]]:
        """
        返回按插槽分组的插件 HTML 注入内容。
        """
        return {slot_id: [dict(entry) for entry in entries] for slot_id, entries in self._slots.items()}

    def get_vue_slots(self) -> dict[str, list[dict[str, Any]]]:
        """
        返回按插槽分组的插件 Vue 组件定义。
        """
        return {slot_id: [dict(entry) for entry in entries] for slot_id, entries in self._vue_slots.items()}

    def get_vue_components(self) -> dict[str, dict[str, Any]]:
        """
        返回所有已注册 Vue 组件定义的浅拷贝。
        """
        return dict(self._vue_components)

    def get_vue_routes(self) -> list[dict[str, Any]]:
        """
        获取已注册的 Vue 路由列表。
        """
        return list(self._vue_routes)

    def _on_html_injected(
        self, plugin_name: str, slot_id: str, html: str, key: str | None, context_key: str | None = None
    ) -> None:
        # 收集 HTML 注入事件到对应插槽，按插件名与 key 原位更新或追加。
        entries = self._slots.setdefault(slot_id, [])
        entry = {"plugin": plugin_name, "html": html}
        if context_key is not None:
            entry["contextKey"] = context_key
        if key is None:
            entries.append(entry)
            return
        entry["key"] = key
        for index, current in enumerate(entries):
            if current["plugin"] == plugin_name and current.get("key") == key:
                entries[index] = entry
                return
        entries.append(entry)

    def _on_vue_slot_registered(
        self,
        plugin_name: str,
        slot_id: str,
        component_name: str,
        template: str,
        script: str,
        style: str,
        context_key: str | None = None,
    ) -> None:
        # 收集 Vue 组件注册事件到插槽，并在全局组件表中登记以供路由引用。
        entries = self._vue_slots.setdefault(slot_id, [])
        entry = {
            "plugin": plugin_name,
            "component_name": component_name,
            "template": template,
            "script": script,
            "style": style,
        }
        if context_key is not None:
            entry["contextKey"] = context_key
        for index, current in enumerate(entries):
            if current["plugin"] == plugin_name and current["component_name"] == component_name:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        # 全局组件表用于路由引用 / 去重
        self._vue_components[component_name] = {
            "plugin": plugin_name,
            "template": template,
            "script": script,
            "style": style,
        }

    def _register_vue_route(
        self,
        plugin: Plugin,
        path: str,
        title: str,
        icon: str,
        component_name: str,
        template: str,
        script: str,
        style: str,
    ) -> None:
        # 注册 Vue 组件路由，同时记录到 _routes 与 _vue_routes；相同路径的先移除旧条目。
        # 同一插件重复注册相同路径时先移除旧条目，避免路由列表出现重复
        self._routes = [r for r in self._routes if not (r["plugin"] == plugin.name and r["path"] == path)]
        self._vue_routes = [r for r in self._vue_routes if not (r["plugin"] == plugin.name and r["path"] == path)]
        self._routes.append(
            {
                "plugin": plugin.name,
                "path": path,
                "title": title,
                "icon": icon,
                "component": component_name,
                "type": "vue",
            }
        )
        self._vue_routes.append(
            {
                "plugin": plugin.name,
                "path": path,
                "title": title,
                "icon": icon,
                "component_name": component_name,
                "template": template,
                "script": script,
                "style": style,
            }
        )
        self._vue_components[component_name] = {
            "plugin": plugin.name,
            "template": template,
            "script": script,
            "style": style,
        }
        self.events.emit("plugin:route_registered", plugin.name, path, title, icon)
        self.events.emit(
            "plugin:vue_route_registered", plugin.name, path, title, component_name, template, script, style, icon
        )

    def get_settings(self, name: str) -> dict[str, Any]:
        """
        返回插件设置结构及当前持久化值。

        :param name: 插件名称
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            return {"schema": None, "values": {}}
        schema = list(plugin._settings.values())
        values = {}
        for key in plugin._settings:
            full_key = f"{name}.{key}"
            values[key] = self._config_values.get(full_key, plugin._settings[key]["default"])
        return {"schema": schema, "values": values}

    def update_setting(self, name: str, key: str, value: Any) -> PluginActionResult:
        """
        更新一个已声明的插件设置，并返回保存结果。

        :param name: 插件名称
        :param key: 设置项键名
        :param value: 需要保存的配置值
        :return: 保存结果
        """
        plugin = self._plugins.get(name)
        if plugin is None or key not in plugin._settings:
            return PluginActionResult(name, PluginAction.UPDATE_SETTING, "not_found", "插件或设置项不存在")
        full_key = f"{name}.{key}"
        old_value = self._config_values.get(full_key)
        self._config_values[full_key] = value
        self._save_plugin_config(name)
        self.events.emit("plugin:settings_changed", name, key, old_value, value)
        return PluginActionResult(name, PluginAction.UPDATE_SETTING, "updated")

    def call_command(self, command: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        """
        在线程池中调用 ``插件名:命令名``，失败或超时时抛出 ``PluginCommandError``。

        :param command: 插件命令，格式为 "插件名:命令名"
        :param params: 传递给命令的参数
        :param timeout: 命令最长执行时间，单位为秒
        :return: 命令处理结果
        :raises PluginCommandError: 命令不存在、未启用、执行失败或超时时抛出
        """
        if not isinstance(command, str) or ":" not in command:
            raise PluginCommandError(f"命令格式错误: {command}")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
            or not isinstance(params, (dict, type(None)))
            or (isinstance(params, dict) and any(not isinstance(key, str) for key in params))
        ):
            raise PluginCommandError("插件命令参数或超时无效")
        plugin_name, cmd_name = command.split(":", 1)
        plugin = self._plugins.get(plugin_name)
        if plugin is None or self._status.get(plugin_name) != "enabled":
            raise PluginCommandError(f"插件 {plugin_name} 未启用或不存在")
        handler = plugin._commands.get(cmd_name)
        if handler is None:
            raise PluginCommandError(f"插件 {plugin_name} 不存在命令 {cmd_name}")
        try:
            future = self._command_executor.submit(handler, **(params or {}))
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            self.logger.error("插件 %s 命令 %s 执行超时", plugin_name, cmd_name)
            raise PluginCommandError(f"插件 {plugin_name} 命令 {cmd_name} 执行超时") from None
        except PluginCommandError:
            raise
        except Exception as exc:
            self.logger.exception("插件 %s 命令 %s 执行失败", plugin_name, cmd_name)
            raise PluginCommandError(f"插件 {plugin_name} 命令 {cmd_name} 执行失败: {exc}") from exc
