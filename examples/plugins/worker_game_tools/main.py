# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过明确命令演示资源、整合包、公共查询和私有文件任务，不在启用时操作游戏。
#
# 公开接口：
#   - class Plugin — 提供保留调用方去重键的第一批业务示例命令。
# ============================================================

from __future__ import annotations

import json
from typing import ClassVar

from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import ProtocolError


class Plugin(BasePlugin):
    """
    由调用方选择具体方法、资源和去重键，所有授权与副作用归属由宿主执行。
    """

    allowed_methods: ClassVar[frozenset[str]] = frozenset(
        {
            "resources.list",
            "instances.scan",
            "instances.get",
            "config.public",
            "accounts.list",
            "accounts.get",
            "operations.get",
            "operations.list",
            "operations.cancel",
            "downloads.submit",
            "files.write_begin",
            "files.write_chunk",
            "files.write_commit",
            "files.list",
            "files.stat",
            "files.read",
            "game.launch",
            "game.running",
            "game.stop",
            "mods.search",
            "mods.versions",
            "mods.download_info",
            "mods.list",
            "mods.install",
            "mods.delete",
            "mods.set_enabled",
            "resourcepacks.search",
            "resourcepacks.versions",
            "resourcepacks.download_info",
            "resourcepacks.list",
            "resourcepacks.install",
            "resourcepacks.delete",
            "shaders.search",
            "shaders.versions",
            "shaders.download_info",
            "shaders.list",
            "shaders.install",
            "shaders.delete",
            "modpacks.prepare",
            "modpacks.get",
            "modpacks.install",
            "modpacks.export_preview",
            "modpacks.export_files",
            "modpacks.export",
        }
    )

    def on_load(self) -> None:
        """
        只注册命令，不扫描、联网、安装、导出或启动游戏。
        """
        self.define_command(
            "request",
            self.request,
            input_fields=tuple(
                {"name": name, "type": "string", "required": True} for name in ("method", "params_json", "mutation_key")
            ),
            output_fields=({"name": "json", "type": "string"},),
        )

    def request(self, method: str, params_json: str, mutation_key: str) -> dict[str, str]:
        """
        执行调用方明确指定的演示方法；长操作只提交任务，不阻塞等待实际完成。

        :param method: 本示例明确列出的 Host API
        :param params_json: 对象形式的 JSON 参数，不接受宿主裸路径
        :param mutation_key: 同一操作重试保持相同值，不同操作使用不同值
        :return: 有限 DTO 或所属任务回执的 JSON
        :raises ProtocolError: 方法不在示例范围或参数不是对象时抛出
        """
        if method not in self.allowed_methods:
            raise ProtocolError("示例未提供此方法", "PLUGIN_API_METHOD_NOT_FOUND")
        params = json.loads(params_json)
        if not isinstance(params, dict):
            raise ProtocolError("示例参数必须为对象", "PLUGIN_SCHEMA_INVALID")
        value = self.host.call(method, params, timeout=30, mutation_key=mutation_key)
        return {"json": json.dumps(value, ensure_ascii=False)}
