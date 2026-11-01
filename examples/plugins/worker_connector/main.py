# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示通过自身命令和面板创建、加入、查询与退出所属联机会话。
#
# 公开接口：
#   - class Plugin — 使用当前运行会话引用的联机示例。
# ============================================================

from __future__ import annotations

import json

from ECL.plugin_api import JsonValue
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    房间执行归宿主任务管理，插件只保留自己的会话引用。
    """

    def on_load(self) -> None:
        """
        登记所属操作；面板的房间码是本插件设置，不读取其他用户房间。
        """
        self._session_ref: str | None = None
        self._panel_registered = False
        self.host.call("settings.define", {"definitions": [{"key": "room_code", "type": "string", "default": ""}]})
        for name, callback in (
            ("host", self.host_room),
            ("join", self.join_room),
            ("refresh", self.refresh),
            ("leave", self.leave_room),
            ("echo", self.request_echo),
        ):
            self.define_command(name, callback, output_fields=({"name": "result", "type": "string"},))
        self._panel("建房端口为 25565，请先启动对应 Minecraft 服务。")

    def on_enable(self) -> None:
        """
        登记同名插件之间的最小 JSON 回声协议，不交付房客机器信息。
        """
        result = self.host.call(
            "connector.protocols.register", {"name": "echo", "callback_ref": self.register_callback(self.echo)}
        )
        self._protocol_ref = result["protocol_ref"]

    def echo(self, payload: JsonValue) -> JsonValue:
        """
        返回收到的有界业务数据。

        :param payload: 宿主已校验的 JSON 载荷
        :return: 原业务载荷
        """
        return payload

    def request_echo(self) -> dict[str, JsonValue]:
        """
        房客通过所属会话请求房主的同名插件。

        :return: 回声结果
        :raises ValueError: 尚无自身会话时抛出
        """
        if self._session_ref is None:
            raise ValueError("尚无自己的会话")
        result = self.host.call(
            "connector.request",
            {"session_ref": self._session_ref, "protocol_ref": self._protocol_ref, "payload": {"message": "hello"}},
        )
        self._panel("房主已返回回声数据。")
        return {"result": json.dumps(result, ensure_ascii=False)}

    def _panel(self, message: str) -> None:
        self._message = message
        if self._panel_registered:
            return
        self.host.call(
            "panels.register",
            {
                "panels": [
                    {
                        "key": "main",
                        "title": "插件自己的联机房间",
                        "data_callback_ref": self.register_callback(self.panel_data),
                        "nodes": [
                            {"type": "text", "text": "房间操作由宿主任务管理，取消后请等待实际清理。"},
                            {"type": "list", "source": "status", "columns": [{"key": "message", "label": "当前状态"}]},
                            {"type": "setting", "key": "room_code"},
                            *[
                                {"type": "button", "label": label, "command": command}
                                for label, command in (
                                    ("创建自己的房间", "host"),
                                    ("加入填写的房间", "join"),
                                    ("查询自己的房间", "refresh"),
                                    ("请求退出自己的房间", "leave"),
                                    ("向房主发送回声请求", "echo"),
                                )
                            ],
                        ],
                    }
                ]
            },
        )
        self._panel_registered = True

    def panel_data(self) -> dict[str, JsonValue]:
        """
        返回当前操作提示，不重复登记面板。

        :return: 宿主面板的数据快照
        """
        return {"lists": {"status": [{"message": self._message}]}, "tasks": {}}

    def _open(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        room = self.host.call("connector.open", params)
        reference = room.get("session_ref")
        if not isinstance(reference, str):
            raise ValueError("宿主未交付会话引用")
        self._session_ref = reference
        self._panel("已提交联机任务，请查询状态；取消请求不等于已经退出。")
        return {"result": json.dumps(room, ensure_ascii=False)}

    def host_room(self) -> dict[str, JsonValue]:
        """
        为固定 Minecraft 服务端口提交所属建房任务。

        :return: 所属会话与任务引用；实际状态见任务与查询
        """
        return self._open({"port": 25565})

    def join_room(self) -> dict[str, JsonValue]:
        """
        使用本插件设置中的房间码提交加入任务。

        :return: 所属会话与任务引用；实际状态见任务与查询
        """
        values = self.host.call("settings.get", {"keys": ["room_code"]}).get("values")
        if not isinstance(values, dict) or not isinstance(values.get("room_code"), str):
            raise ValueError("请填写房间码")
        return self._open({"code": values["room_code"]})

    def refresh(self) -> dict[str, JsonValue]:
        """
        查询自己的会话状态，不取得其他房间信息。

        :return: 所属会话当前状态
        """
        if self._session_ref is None:
            raise ValueError("尚无自己的会话")
        result = self.host.call("connector.get", {"session_ref": self._session_ref})
        self._panel(
            f"会话状态：{result['mode']}；房间码：{result['room_code'] or '尚未生成'}；等待清理：{result['cleanup_pending']}"
        )
        return {"result": json.dumps(result, ensure_ascii=False)}

    def leave_room(self) -> dict[str, JsonValue]:
        """
        请求取消所属房间任务，等待宿主确认实际资源停止。

        :return: 取消接纳结果；任务中心显示清理结果
        """
        if self._session_ref is None:
            raise ValueError("尚无自己的会话")
        result = self.host.call("connector.leave", {"session_ref": self._session_ref})
        self._panel("已请求退出，请等待任务结束及实际资源清理。")
        return {"result": json.dumps(result, ensure_ascii=False)}
