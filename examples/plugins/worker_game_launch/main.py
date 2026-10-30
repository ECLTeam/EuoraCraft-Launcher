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
            "launch",
            self.launch,
            input_fields=({"name": "instance_ref", "type": "string"}, {"name": "mutation_key", "type": "string"}),
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "running",
            self.running,
            input_fields=({"name": "instance_ref", "type": "string"},),
            output_fields=({"name": "json", "type": "string"},),
        )
        self.define_command(
            "stop",
            self.stop,
            input_fields=({"name": "game_run_ref", "type": "string"}, {"name": "mutation_key", "type": "string"}),
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

    def launch(self, instance_ref: str, mutation_key: str) -> dict[str, str]:
        """
        提交所选实例的启动，账户与Java由宿主提供。

        :param instance_ref: 已获独立读取及启动授权的具体实例
        :param mutation_key: 保存并重用的提交去重键
        :return: 启动任务回执
        """
        return {
            "json": json.dumps(
                self.host.call(
                    "game.launch",
                    {"instance_ref": instance_ref, "options": {"memory_mb": 2048}},
                    mutation_key=mutation_key,
                )
            )
        }

    def running(self, instance_ref: str) -> dict[str, str]:
        """
        查询当前运行自己启动的进程，保留实际退出状态。

        :param instance_ref: 获准读取的具体实例引用
        :return: 只属于当前Worker运行的进程记录
        """
        return {"json": json.dumps(self.host.call("game.running", {"instance_ref": instance_ref}))}

    def stop(self, game_run_ref: str, mutation_key: str) -> dict[str, str]:
        """
        在独立停止授权下停止自己创建的确切进程。

        :param game_run_ref: launch结果或running返回的本运行游戏引用
        :param mutation_key: 保存并重用的提交去重键
        :return: 停止任务接纳结果，实际退出须查询status确认
        """
        return {
            "json": json.dumps(self.host.call("game.stop", {"game_run_ref": game_run_ref}, mutation_key=mutation_key))
        }
