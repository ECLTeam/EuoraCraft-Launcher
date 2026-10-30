# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示明确命令驱动的游戏Host API，加载时不下载、不启动游戏。
#
# 公开接口：
#   - class Plugin — 注册资源、游戏和自身任务示例命令。
# ============================================================

from __future__ import annotations

import json

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    所有资源引用和提交去重键均由调用方明确提供，不自动选择目录或实例。
    """

    def on_load(self) -> None:
        """
        仅注册命令，所有实际副作用都由之后的明确命令触发。
        """
        self.define_command(
            "discover",
            self.discover,
            input_fields=({"name": "capability", "type": "string"},),
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "cancel",
            self.cancel,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "catalog",
            self.catalog,
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "loaders",
            self.loaders,
            input_fields=({"name": "game_version", "type": "string"}, {"name": "loader", "type": "string"}),
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "install",
            self.install,
            input_fields=tuple(
                {"name": name, "type": "string"}
                for name in ("root_ref", "version_id", "version_name", "loader", "mutation_key")
            ),
            output_fields=({"name": "json", "type": "string"},),
        )

    def discover(self, capability: str) -> dict[str, str]:
        """
        列出当前运行获准的资源，不把宿主持久化标识作为API引用。

        :param capability: 本示例声明的具体实例能力
        :return: 当前运行随机引用的JSON分页
        """
        return {"json": json.dumps(self.host.call("resources.list", {"capability": capability}))}

    def status(self, operation_ref: str) -> dict[str, str]:
        """
        查询自身任务，完成结果可能因缺少独立读取权限而为空。

        :param operation_ref: 本运行提交返回的任务引用
        :return: 状态、实际效果、清理状态和允许读取的结果
        """
        return {"json": json.dumps(self.host.call("operations.get", {"operation_ref": operation_ref}))}

    def cancel(self, operation_ref: str) -> dict[str, str]:
        """
        请求取消自身任务，实际停止仍须继续查询状态。

        :param operation_ref: 本运行提交返回的任务引用
        :return: 取消接纳结果
        """
        return {"json": json.dumps(self.host.call("operations.cancel", {"operation_ref": operation_ref}))}

    def catalog(self) -> dict[str, str]:
        """
        查询宿主有效来源中的正式版首页，不返回下载地址。

        :return: 公开版本目录分页
        """
        return {"json": json.dumps(self.host.call("game.catalog", {"type": "release", "limit": 10}, timeout=30))}

    def loaders(self, game_version: str, loader: str) -> dict[str, str]:
        """
        查询指定游戏版本的受支持加载器版本。

        :param game_version: 目录中的真实游戏版本
        :param loader: fabric、forge、neoforge或quilt
        :return: 公开加载器版本分页
        """
        return {
            "json": json.dumps(
                self.host.call(
                    "game.loaders", {"game_version": game_version, "loader": loader, "limit": 10}, timeout=30
                )
            )
        }

    def install(
        self, root_ref: str, version_id: str, version_name: str, loader: str, mutation_key: str
    ) -> dict[str, str]:
        """
        在明确选择的获准根目录中新建实例，加载器缺省版本由宿主决定。

        :param root_ref: discover返回且由用户选定的安装根引用
        :param version_id: 目录中的游戏版本
        :param version_name: 不覆盖现有实例的新名称
        :param loader: vanilla、fabric、forge、neoforge或quilt
        :param mutation_key: 同一提交保存并重用的去重键
        :return: 接纳回执，不表示安装已经完成
        """
        return {
            "json": json.dumps(
                self.host.call(
                    "game.install",
                    {"root_ref": root_ref, "version_id": version_id, "version_name": version_name, "loader": loader},
                    mutation_key=mutation_key,
                )
            )
        }
