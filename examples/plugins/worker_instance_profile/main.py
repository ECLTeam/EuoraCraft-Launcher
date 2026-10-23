# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：显式修改用户选定实例资料的Worker示例，加载及卸载不执行写入。
#
# 公开接口：
#   - class Plugin — 发现/读取已授权实例并提交四字段资料任务。
# ============================================================

from __future__ import annotations

import json

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    所有目标引用、期望版本及变更键由命令调用方明确提供。
    """

    def on_load(self) -> None:
        """
        只登记命令，实例发现及资料写入均等待用户明确调用。
        """
        text = {"name": "json", "type": "string", "max_length": 16384}
        self.define_command("discover", self.discover, output_fields=(text,))
        self.define_command(
            "scan",
            self.scan,
            input_fields=({"name": "root_ref", "type": "string", "max_length": 32},),
            output_fields=(text,),
        )
        self.define_command(
            "inspect",
            self.inspect,
            input_fields=({"name": "instance_ref", "type": "string", "max_length": 32},),
            output_fields=(text,),
        )
        self.define_command(
            "update",
            self.update_profile,
            input_fields=(
                {"name": "instance_ref", "type": "string", "max_length": 32},
                {"name": "expected_profile_etag", "type": "string", "max_length": 64},
                {"name": "mutation_key", "type": "string", "max_length": 80},
                {"name": "alias", "type": "string", "required": False, "max_length": 120},
                {"name": "description", "type": "string", "required": False, "max_length": 1000},
                {"name": "favorite", "type": "bool", "required": False},
                {"name": "tags_json", "type": "string", "required": False, "max_length": 4096},
            ),
            output_fields=({"name": "operation_ref", "type": "string"}, {"name": "state", "type": "string"}),
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string", "max_length": 32},),
            output_fields=(text,),
        )
        self.define_command(
            "cancel",
            self.cancel,
            input_fields=({"name": "operation_ref", "type": "string", "max_length": 32},),
            output_fields=({"name": "requested", "type": "bool"},),
        )

    def discover(self) -> dict[str, str]:
        """
        返回当前运行的获准读资源，写范围仍须由宿主独立检查。
        """
        return {"json": json.dumps(self.host.call("resources.list", {"capability": "instances.read", "limit": 10}))}

    def scan(self, root_ref: str) -> dict[str, str]:
        """
        每次只读取至多五项实例摘要，示例不隐式写入或选择实例。

        :param root_ref: 当前运行获准目录引用
        :return: 第一页的显式JSON摘要
        """
        return {"json": json.dumps(self.host.call("instances.scan", {"root_ref": root_ref, "limit": 5}))}

    def inspect(self, instance_ref: str) -> dict[str, str]:
        """
        读取当前完整资料版本，修改前应保存此时的profile_etag。

        :param instance_ref: 当前运行的具体实例引用
        :return: 无路径的实例摘要
        """
        return {"json": json.dumps(self.host.call("instances.get", {"instance_ref": instance_ref}))}

    def update_profile(
        self,
        instance_ref: str,
        expected_profile_etag: str,
        mutation_key: str,
        alias: str | None = None,
        description: str | None = None,
        favorite: bool | None = None,
        tags_json: str | None = None,
    ) -> dict[str, str]:
        """
        只提交显式给出的四字段增量，不自动重读版本后覆盖用户新修改。

        :param instance_ref: 当前运行的目标实例
        :param expected_profile_etag: 调用方确认的旧完整资料版本
        :param mutation_key: 本次提交保留的稳定变更键
        :param alias: 可选的新别名
        :param description: 可选的新描述，空文本为显式空描述
        :param favorite: 可选收藏值，false必须真实保存
        :param tags_json: 可选的标签JSON数组，空数组为显式清空
        :return: 接纳回执，不表示文件已经保存
        """
        patch = {
            name: value
            for name, value in (("alias", alias), ("description", description), ("favorite", favorite))
            if value is not None
        }
        if tags_json is not None:
            patch["tags"] = json.loads(tags_json)
        return self.host.call(
            "instances.profile_patch",
            {"instance_ref": instance_ref, "expected_profile_etag": expected_profile_etag, "patch": patch},
            mutation_key=mutation_key,
        )

    def status(self, operation_ref: str) -> dict[str, str]:
        """
        查询自身任务实际状态及清理，不把提交或等待超时当作完成。

        :param operation_ref: 本运行自己的任务引用
        :return: 任务实际白名单视图
        """
        return {"json": json.dumps(self.host.call("operations.get", {"operation_ref": operation_ref}))}

    def cancel(self, operation_ref: str) -> dict[str, bool]:
        """
        请求取消未接受提交的自身任务，实际停止仍需查询。

        :param operation_ref: 本运行自己的任务引用
        :return: 是否接纳取消请求
        """
        result = self.host.call("operations.cancel", {"operation_ref": operation_ref})
        return {"requested": result["requested"]}
