# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：处理窗口列表、创建、聚焦和尺寸位置请求。
#
# 公开接口：
#   - class WindowHandlers — Host-owned, local-only WebView window lifecycle boundary.
#       - window_list(body) -> dict[str, Any]
#       - window_open(body, app_handle) -> dict[str, Any]
#       - window_focus(body) -> dict[str, Any]
#       - window_close(body) -> dict[str, Any]
#       - window_update_bounds(body) -> dict[str, Any]
#       - close_plugin_windows(plugin_name) -> None
# ============================================================

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any
from uuid import uuid4

try:
    from pytauri import AppHandle, WebviewUrl
    from pytauri.ffi.image import Image as TauriImage
    from pytauri.webview import WebviewWindowBuilder
except ImportError:  # pragma: no cover - lightweight unit-test stubs omit window bindings
    AppHandle = Any  # type: ignore[misc,assignment]
    TauriImage = None  # type: ignore[misc,assignment]

    class WebviewUrl:  # type: ignore[no-redef]
        @staticmethod
        def App(value: str) -> str:  # noqa: N802 - mirrors PyTauri's public constructor
            return value

    class WebviewWindowBuilder:  # type: ignore[no-redef]
        @staticmethod
        def build(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("PyTauri 窗口 API 不可用")


from ECL.api.bridge import _FrontendState, _validate_body
from ECL.api.contracts import failure, success
from ECL.api.models import WindowBoundsRequest, WindowLabelRequest, WindowOpenRequest
from ECL.plugins.permissions import Permission, PermissionAction, PermissionScope


class WindowDescriptorRegistry:
    """
    保存宿主内置窗口的描述符。
    """

    descriptors: dict[str, dict[str, Any]] = {}


def _create_child_window_icon() -> Any | None:
    # Create a known-valid RGBA icon instead of inheriting a malformed ICO.
    if TauriImage is None:
        return None
    size = 32
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            offset = (y * size + x) * 4
            # Rounded blue tile.
            corner_x = min(x, size - 1 - x)
            corner_y = min(y, size - 1 - y)
            visible = corner_x >= 4 or corner_y >= 4 or (corner_x - 4) ** 2 + (corner_y - 4) ** 2 <= 16
            if visible:
                pixels[offset : offset + 4] = bytes((91, 111, 245, 255))
            # Small white "E" mark.
            if (9 <= x <= 12 and 8 <= y <= 23) or (12 <= x <= 22 and (8 <= y <= 11 or 14 <= y <= 17 or 20 <= y <= 23)):
                pixels[offset : offset + 4] = bytes((255, 255, 255, 255))
    return TauriImage(bytes(pixels), size, size)


class WindowHandlers(_FrontendState):
    """
    Host-owned, local-only WebView window lifecycle boundary.
    """

    def _plugin_window_descriptor(self, descriptor_id: str) -> dict[str, Any] | None:
        if not descriptor_id.startswith("plugin."):
            return None
        _, plugin_name, window_id = [*descriptor_id.split(".", 2), "", ""][:3]
        plugin = self.plugins.get_plugin(plugin_name)
        if plugin is None:
            return None
        status = next(
            (item.get("status") for item in self.plugins.list_plugins() if item.get("name") == plugin_name), None
        )
        if status != "enabled":
            return None
        permission = Permission(PermissionScope.UI, PermissionAction.WRITE, f"window:{window_id}")
        if not self.plugins._permission_manager.has_permission(plugin_name, permission):
            return None
        contributes = plugin.metadata.get("contributes") or {}
        windows = contributes.get("windows") if isinstance(contributes, dict) else None
        if not isinstance(windows, list):
            return None
        for raw in windows:
            if not isinstance(raw, dict) or raw.get("id") != window_id:
                continue
            route = raw.get("route")
            if not isinstance(route, str) or not route.startswith(f"/plugin/{plugin_name}/"):
                return None
            return {
                "id": descriptor_id,
                "type": "plugin",
                "plugin": plugin_name,
                "route": route,
                "title": str(raw.get("title") or plugin.title),
                "width": min(max(int(raw.get("width", 720)), 320), 1920),
                "height": min(max(int(raw.get("height", 560)), 240), 1080),
                "minWidth": min(max(int(raw.get("minWidth", 360)), 320), 1920),
                "minHeight": min(max(int(raw.get("minHeight", 300)), 240), 1080),
                "singleton": raw.get("singleton", True) is not False,
                "followMain": True,
                "dataSchema": raw.get("dataSchema") if isinstance(raw.get("dataSchema"), dict) else {},
                "allowedCommands": raw.get("commands") if isinstance(raw.get("commands"), list) else [],
                "allowedEvents": raw.get("events") if isinstance(raw.get("events"), list) else [],
                "allowedSettings": raw.get("settings") if isinstance(raw.get("settings"), list) else [],
            }
        return None

    def _resolve_window_descriptor(self, descriptor_id: str) -> dict[str, Any] | None:
        descriptor = WindowDescriptorRegistry.descriptors.get(descriptor_id)
        return dict(descriptor) if descriptor else self._plugin_window_descriptor(descriptor_id)

    async def window_list(self, body: dict[str, Any]) -> dict[str, Any]:
        return success(list(self._window_metadata.values()))

    async def window_open(  # noqa: C901
        self, body: dict[str, Any], app_handle: AppHandle
    ) -> dict[str, Any]:
        request, invalid = _validate_body(WindowOpenRequest, body)
        if invalid is not None:
            return invalid
        descriptor = self._resolve_window_descriptor(request.descriptor_id)
        if descriptor is None:
            return failure("窗口类型不存在或未获授权", "WINDOW_DESCRIPTOR_DENIED")

        if descriptor["singleton"]:
            existing = next(
                (meta for meta in self._window_metadata.values() if meta.get("descriptorId") == request.descriptor_id),
                None,
            )
            if existing and self._focus_registered_window(existing["label"]):
                return success(existing)

        suffix = request.instance_key or uuid4().hex[:10]
        label = request.descriptor_id if descriptor["singleton"] else f"{request.descriptor_id}:{suffix}"
        query = {
            "window": descriptor["type"],
            "label": label,
            "title": descriptor["title"],
        }
        if request.session_id:
            query["session"] = request.session_id
        if descriptor.get("route"):
            query["route"] = descriptor["route"]
        url = "index.html"
        ui_config = self.config.get_config("ui") or {}
        saved_bounds = (ui_config.get("windows") or {}).get(request.descriptor_id, {})
        width = min(max(int(saved_bounds.get("width", descriptor["width"])), descriptor["minWidth"]), 7680)
        height = min(max(int(saved_bounds.get("height", descriptor["height"])), descriptor["minHeight"]), 4320)
        builder_options: dict[str, Any] = {
            "title": descriptor["title"],
            "inner_size": (width, height),
            "min_inner_size": (descriptor["minWidth"], descriptor["minHeight"]),
            "decorations": False,
            "transparent": bool(descriptor.get("transparent", False)),
            "visible": False,
            "resizable": True,
            "initialization_script": f"window.__ECL_WINDOW_CONTEXT__={json.dumps(query, ensure_ascii=True)};",
            "background_color": (244, 246, 250, 255),
        }
        window_icon = _create_child_window_icon()
        if window_icon is not None:
            builder_options["icon"] = window_icon
        if isinstance(saved_bounds.get("x"), int) and isinstance(saved_bounds.get("y"), int):
            builder_options["position"] = (saved_bounds["x"], saved_bounds["y"])
        try:
            webview = WebviewWindowBuilder.build(
                app_handle,
                label,
                WebviewUrl.App(url),
                **builder_options,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.logger.exception("创建窗口失败: %s", request.descriptor_id)
            return failure(f"创建窗口失败: {exc}", "WINDOW_CREATE_FAILED")
        self._webviews[label] = webview
        metadata = {
            "label": label,
            "descriptorId": request.descriptor_id,
            "windowType": descriptor["type"],
            "plugin": descriptor.get("plugin"),
            "sessionId": request.session_id,
            "singleton": descriptor["singleton"],
            "followMain": descriptor["followMain"],
            "dataSchema": descriptor["dataSchema"],
            "allowedCommands": descriptor.get("allowedCommands", []),
            "allowedEvents": descriptor.get("allowedEvents", []),
            "allowedSettings": descriptor.get("allowedSettings", []),
            "ready": False,
            "bounds": {
                "width": width,
                "height": height,
                **({"x": saved_bounds["x"], "y": saved_bounds["y"]} if "position" in builder_options else {}),
            },
        }
        self._window_metadata[label] = metadata
        self.logger.info("已创建受控窗口：窗口标识：%s；类型：%s", label, descriptor["type"])
        on_window_event = getattr(webview, "on_window_event", None)
        if callable(on_window_event):

            def handle_native_window_event(*args: Any) -> None:
                event = args[-1] if args else None
                event_name = type(event).__name__.lower()
                if "destroy" not in event_name and "close" not in event_name:
                    return
                removed = self._window_metadata.pop(label, None)
                self._webviews.pop(label, None)
                if removed is not None:
                    self.emit_to_frontend("window:closed", removed)

            on_window_event(handle_native_window_event)

        def reveal_window() -> None:
            webview.unminimize()
            webview.show()
            webview.set_focus()

        with suppress(OSError, RuntimeError):
            webview.run_on_main_thread(reveal_window)
        self.emit_to_frontend("window:opened", metadata)
        return success(metadata)

    def _focus_registered_window(self, label: str) -> bool:
        webview = self._webviews.get(label)
        if webview is None:
            return False

        def focus() -> None:
            webview.unminimize()
            webview.show()
            webview.set_focus()

        try:
            webview.run_on_main_thread(focus)
            return True
        except (OSError, RuntimeError):
            self._webviews.pop(label, None)
            self._window_metadata.pop(label, None)
            return False

    async def window_focus(self, body: dict[str, Any]) -> dict[str, Any]:
        request, invalid = _validate_body(WindowLabelRequest, body)
        if invalid is not None:
            return invalid
        if not self._focus_registered_window(request.label):
            return failure("窗口不存在", "WINDOW_NOT_FOUND")
        return success()

    async def window_close(self, body: dict[str, Any]) -> dict[str, Any]:
        request, invalid = _validate_body(WindowLabelRequest, body)
        if invalid is not None:
            return invalid
        if request.label == "main":
            return failure("主窗口必须通过启动器退出流程关闭", "WINDOW_MAIN_CLOSE_DENIED")
        webview = self._webviews.pop(request.label, None)
        metadata = self._window_metadata.pop(request.label, None)
        if webview is None:
            return failure("窗口不存在", "WINDOW_NOT_FOUND")
        with suppress(OSError, RuntimeError):
            webview.run_on_main_thread(webview.close)
        self.emit_to_frontend("window:closed", metadata or {"label": request.label})
        return success()

    async def window_update_bounds(self, body: dict[str, Any]) -> dict[str, Any]:
        request, invalid = _validate_body(WindowBoundsRequest, body)
        if invalid is not None:
            return invalid
        metadata = self._window_metadata.get(request.label)
        if metadata is None:
            return failure("窗口不存在", "WINDOW_NOT_FOUND")
        bounds = request.model_dump(mode="json", exclude={"label"}, exclude_none=True)
        metadata["bounds"] = bounds
        ui = self.config.get_config("ui") or {}
        windows = dict(ui.get("windows") or {})
        windows[metadata["descriptorId"]] = bounds
        ui["windows"] = windows
        self.config.save_config("ui", ui)
        return success(metadata)

    def close_plugin_windows(self, plugin_name: str) -> None:
        labels = [label for label, metadata in self._window_metadata.items() if metadata.get("plugin") == plugin_name]
        for label in labels:
            webview = self._webviews.pop(label, None)
            metadata = self._window_metadata.pop(label, None)
            if webview is not None:
                with suppress(OSError, RuntimeError):
                    webview.run_on_main_thread(webview.close)
            self.emit_to_frontend("window:closed", metadata or {"label": label, "plugin": plugin_name})


__all__ = ["WindowDescriptorRegistry", "WindowHandlers"]
