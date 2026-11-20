# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：插件注册表：路由/槽位/事件订阅/前端组件的集中查询。
#
# 公开接口：
#   - class PluginRegistry — 负责插件注册与扩展点收集，涵盖路由、设置、命令与 Vue 注入等条目。
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
#       - call_command(command, params=…, timeout=…) -> Any — 经唯一运行服务调用 ``插件名:命令名``，失败或超时时抛出 ``PluginCommandError``。
# ============================================================

from __future__ import annotations

import base64
import math
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from ECL.plugins.contracts import JsonValue
from ECL.plugins.run_store import PluginRunError

from .base import _PluginState
from .contracts import PluginAction, PluginActionResult, PluginCommandError


def _plugin_entry(
    name: str,
    metadata: Any,
    status: str,
    error: Any = None,
    *,
    services: list[str] | None = None,
    settings: list[str] | None = None,
    is_system: bool = False,
    icon_data_url: str = "",
) -> dict[str, Any]:
    # 构建前端插件列表条目，统一已登记/依赖跳过/禁用/清单无效四类来源的字典结构。
    if isinstance(metadata, dict):
        title = metadata.get("title", name)
        version = metadata.get("version", "")
        description = metadata.get("description", "")
        author = metadata.get("author", "")
        homepage = metadata.get("homepage", "")
        license = metadata.get("license", "")
        dependencies = metadata.get("dependencies", {})
        contributes = metadata.get("contributes", {})
    else:
        title = name
        version = ""
        description = ""
        author = ""
        homepage = ""
        license = ""
        dependencies = {}
        contributes = {}
    return {
        "name": name,
        "title": title,
        "version": version,
        "description": description,
        "author": author,
        "homepage": homepage,
        "license": license,
        "icon": icon_data_url,
        "status": status,
        "error": error,
        "dependencies": dependencies,
        "contributes": contributes if isinstance(contributes, dict) else {},
        "services": services or [],
        "settings": settings or [],
        "is_system": is_system,
    }


class PluginRegistry(_PluginState):
    """
    负责插件列表、路由插槽收集、设置与命令调用的统一入口。
    """

    icon_mime_by_suffix: ClassVar[Mapping[str, str]] = MappingProxyType(
        {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    )
    max_icon_bytes: ClassVar[int] = 256 * 1024

    def _icon_data_url(self, plugin_dir: Path | None, metadata: Any) -> str:
        """
        读取清单 ``icon`` 声明的图片文件并编码为 data URL。

        图标是纯展示资源：文件缺失、超限、不可读或清单未声明时返回空串，
        界面回退默认拼图图标，不阻断插件列表构建。

        :param plugin_dir: 插件安装目录；归档待恢复等无目录来源传 None
        :param metadata: 清单元数据
        :return: data URL；无法提供图标时为空串
        """
        icon_name = metadata.get("icon") if isinstance(metadata, dict) else ""
        if not icon_name or plugin_dir is None:
            return ""
        icon_path = plugin_dir / icon_name
        mime = self.icon_mime_by_suffix.get(icon_path.suffix.lower())
        if mime is None or not icon_path.is_file() or icon_path.is_symlink():
            return ""
        try:
            if icon_path.stat().st_size > self.max_icon_bytes:
                self.logger.warning("插件图标超过大小上限，已回退默认图标：%s", icon_path.name)
                return ""
            payload = icon_path.read_bytes()
        except OSError as exc:
            self.logger.warning("插件图标读取失败，已回退默认图标：%s；%s", icon_path.name, exc)
            return ""
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"

    def get_plugin_metadata(self, name: str) -> dict[str, Any] | None:
        """
        读取已登记目录插件或已恢复归档插件的元数据，不导入插件代码。

        :param name: 插件名称
        :return: 清单副本；插件不存在时返回 None
        """
        candidate = self._candidate_map.get(name)
        if candidate is not None:
            return dict(candidate["metadata"])
        return self._package_metadata(name)

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
                    icon_data_url=self._icon_data_url(candidate.get("plugin_dir"), candidate.get("metadata", {})),
                )
            )

    def list_plugins(self) -> list[dict[str, Any]]:
        """
        从发现元数据和唯一运行服务构造第三方列表，不实例化插件获取展示字段。

        已启用的运行按实际登记结果回填 has_panels，未启用或运行失效时明确为 False，
        供管理页决定是否提供声明式面板入口。

        :return: 安装、审阅、运行、清理状态及是否声明面板
        """
        result = []
        names = set(self._candidate_map) | set(self._package_entries)
        for name in sorted(names):
            candidate = self._candidate_map.get(name, {})
            is_system = bool(candidate.get("is_system", False))
            package = self._package_entries.get(name, {})
            metadata = candidate.get("metadata") or package.get("metadata", {})
            plugin_dir = candidate.get("plugin_dir")
            if plugin_dir is None:
                active = package.get("active")
                plugin_dir = active.code_path if active is not None else None
            entry = _plugin_entry(
                name,
                metadata,
                self._status.get(name, package.get("status", "unloaded")),
                self._plugin_errors.get(name) or package.get("error") or self._dependency_resolution.errors.get(name),
                is_system=is_system,
                icon_data_url=self._icon_data_url(plugin_dir, metadata),
            )
            entry["pending_restart"] = bool(package.get("pending_restart"))
            entry["installed_version"] = metadata.get("version", "")
            entry["has_panels"] = False
            session_reason = self._session_disable_reason(name)
            if session_reason:
                entry["status"] = "disabled"
                entry["error"] = session_reason
            elif self.worker_service is not None and entry["status"] != "unsupported":
                try:
                    self._apply_worker_details(name, entry)
                except ValueError:
                    entry["status"] = "unsupported"
            result.append(entry)
        return result

    def _apply_worker_details(self, name: str, entry: dict[str, Any]) -> None:
        """
        以运行服务的实际身份和清理状态补充展示条目。

        归档等待重启时保留安装状态；运行故障优先使用监管服务记录的稳定错误码。
        """
        assert self.worker_service is not None
        identity = self.worker_service.run_identity(name)
        runtime = self.worker_service.runtime_info(name)
        entry.update(
            installation_id=identity["installation_id"],
            content_digest=identity["content_digest"],
            cleanup_state=runtime["cleanup_state"],
        )
        source = next((item for item in self.worker_service.directory_sources() if item.name == name), None)
        if entry.get("is_system"):
            entry["source"] = "system"
        else:
            entry["source"] = "directory" if source is not None else "package"
        entry["supports_hot_reload"] = source.manifest.supports_hot_reload if source is not None else False
        entry["development"] = next(
            (item for item in self.worker_service.development.view()["items"] if item["name"] == name), None
        )
        if entry["development"] is not None:
            entry["supports_hot_reload"] = entry["development"]["supports_hot_reload"]
        if not entry["pending_restart"]:
            if runtime["run_state"] in {"enabled", "failed", "stopping", "disabled"}:
                entry["status"] = runtime["run_state"]
            else:
                entry["status"] = "disabled"
        if runtime["last_error"] is not None:
            entry["error"] = runtime["last_error"]["code"]
        if runtime["run_state"] == "enabled":
            broker = self.worker_service.broker(name)
            entry["services"] = broker.command_names()
            try:
                entry["settings"] = broker.settings_view()["schema"]
            except ValueError:
                entry["settings"] = []
            try:
                entry["has_panels"] = bool(broker.panels_view())
            except ValueError:
                entry["has_panels"] = False

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

    def clear_plugin_slots(self, plugin_name: str) -> None:
        """
        移除该插件注册的插槽内容，禁用、撤销或卸载后不再渲染。

        :param plugin_name: 插件名称
        """
        for slot_id, entries in list(self._slots.items()):
            remaining = [entry for entry in entries if entry.get("plugin") != plugin_name]
            if remaining:
                self._slots[slot_id] = remaining
            else:
                del self._slots[slot_id]
        for slot_id, entries in list(self._vue_slots.items()):
            remaining = [entry for entry in entries if entry.get("plugin") != plugin_name]
            if remaining:
                self._vue_slots[slot_id] = remaining
            else:
                del self._vue_slots[slot_id]
        for component_name, entry in list(self._vue_components.items()):
            if entry.get("plugin") == plugin_name:
                del self._vue_components[component_name]
        self._routes = [route for route in self._routes if route["plugin"] != plugin_name]
        self._vue_routes = [route for route in self._vue_routes if route["plugin"] != plugin_name]
        self.events.emit("plugin:slots_cleared", plugin_name)

    def _on_html_injected(
        self, plugin_name: str, slot_id: str, html: str, key: str | None, context_key: str | None = None
    ) -> None:
        # 收集 HTML 注入事件到对应插槽，按插件名与 key 原位更新或追加；只接受已登记安装。
        if plugin_name not in self._candidate_map:
            return
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
        if plugin_name not in self._candidate_map:
            return
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

    def get_settings(self, name: str) -> dict[str, Any]:
        """
        返回插件设置结构及当前持久化值。

        :param name: 插件名称
        """
        if self._candidate_map.get(name) is None:
            return {"schema": None, "values": {}}
        assert self.worker_service is not None
        return self.worker_service.settings_view(name)

    def update_setting(self, name: str, key: str, value: Any) -> PluginActionResult:
        """
        更新一个已声明的插件设置，并返回保存结果。

        :param name: 插件名称
        :param key: 设置项键名
        :param value: 需要保存的配置值
        :return: 保存结果
        """
        candidate = self._candidate_map.get(name)
        if candidate is None:
            return PluginActionResult(name, PluginAction.UPDATE_SETTING, "not_found", "插件或设置项不存在")
        assert self.worker_service is not None
        view = self.worker_service.settings_view(name)
        if view.get("schema") is None or not any(item["key"] == key for item in view["schema"]):
            return PluginActionResult(name, PluginAction.UPDATE_SETTING, "not_found", "插件或设置项不存在")
        try:
            self.worker_service.update_settings(
                name,
                view["content_digest"],
                view["schema_revision"],
                view["revision"],
                {key: value},
            )
        except (PluginRunError, ValueError) as exc:
            return PluginActionResult(name, PluginAction.UPDATE_SETTING, "failed", str(exc))
        return PluginActionResult(name, PluginAction.UPDATE_SETTING, "updated")

    def query_command(self, command: str, params: dict[str, JsonValue] | None = None) -> JsonValue:
        """
        调用普通 Worker 明确登记的查询命令，沿用其权限和结果边界。

        :param command: 插件名与查询名
        :param params: 有界 JSON 输入
        :return: 当前运行的查询结果
        :raises PluginCommandError: 目标、查询声明或参数无效时抛出
        """
        if not isinstance(command, str) or ":" not in command or self.worker_service is None:
            raise PluginCommandError("插件查询格式或目标无效")
        plugin_name, name = command.split(":", 1)
        try:
            return self.worker_service.broker(plugin_name).call_query(name, params or {})
        except ValueError as exc:
            raise PluginCommandError("插件查询未完成") from exc

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
        if self._candidate_map.get(plugin_name) is None:
            raise PluginCommandError(f"插件 {plugin_name} 未启用或不存在")
        try:
            assert self.worker_service is not None
            return self.worker_service.broker(plugin_name).call_command(cmd_name, params or {}, timeout)
        except ValueError as exc:
            raise PluginCommandError("插件命令执行未完成") from exc
