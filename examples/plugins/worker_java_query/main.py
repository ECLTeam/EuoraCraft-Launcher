# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：显式演示已授权 Java 查询、单目标校验和官方安装任务，启用时不执行探测。
#
# 公开接口：
#   - class Plugin — 注册明确调用的 Java 查询与安装示例命令。
# ============================================================
from __future__ import annotations

import json

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    只在用户调用命令时执行查询，读取和校验范围分别由宿主授权。
    """

    def on_load(self) -> None:
        """
        注册命令，不扫描、执行 Java 或访问网络。
        """
        output = ({"name": "json", "type": "string"},)
        self.define_command("inventory", self.inventory, output_fields=output)
        self.define_command(
            "validate",
            self.validate,
            input_fields=({"name": "runtime_ref", "type": "string", "required": True},),
            output_fields=output,
        )
        self.define_command("catalog", self.catalog, output_fields=output)
        self.define_command(
            "prepare_install",
            self.prepare_install,
            input_fields=(
                {"name": "package_ref", "type": "string", "required": True},
                {"name": "mutation_key", "type": "string", "required": True},
            ),
            output_fields=output,
        )
        self.define_command(
            "install",
            self.install,
            input_fields=(
                {"name": "plan_ref", "type": "string", "required": True},
                {"name": "mutation_key", "type": "string", "required": True},
            ),
            output_fields=output,
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string", "required": True},),
            output_fields=output,
        )

    def prepare_install(self, package_ref: str, mutation_key: str) -> dict[str, str]:
        """
        为明确选择的官方包生成短期计划，不下载或安装。

        :param package_ref: 当前 catalog 返回的包引用
        :param mutation_key: 本次计划请求的稳定去重键
        :return: 可供审阅的计划摘要及 plan_ref
        """
        return {
            "json": json.dumps(
                self.host.call("java.install_plan", {"package_ref": package_ref}, mutation_key=mutation_key)
            )
        }

    def install(self, plan_ref: str, mutation_key: str) -> dict[str, str]:
        """
        提交明确计划的自身安装任务，安装并登记成功后才返回可用运行引用。

        :param plan_ref: 明确选定的未过期计划引用
        :param mutation_key: 同一次安装重试保留的去重键
        :return: 所属任务回执，不表示安装已经完成
        """
        return {"json": json.dumps(self.host.call("java.install", {"plan_ref": plan_ref}, mutation_key=mutation_key))}

    def status(self, operation_ref: str) -> dict[str, str]:
        """
        查询本运行任务的实际结果和清理状态。

        :param operation_ref: 安装返回的所属任务引用
        :return: 完成、失败、取消或进行中状态
        """
        return {"json": json.dumps(self.host.call("operations.get", {"operation_ref": operation_ref}))}

    def inventory(self) -> dict[str, str]:
        """
        查询当前获准 Java 的最后登记摘要，不触发扫描。

        :return: 首个分页的 JSON；更多项目使用结果的 next_cursor 继续查询
        """
        return {"json": json.dumps(self.host.call("java.inventory"), ensure_ascii=False)}

    def validate(self, runtime_ref: str) -> dict[str, str]:
        """
        对明确选择的当前运行引用执行一次有界版本校验。

        :param runtime_ref: inventory 返回的运行引用，不能填入路径
        :return: 本次探测摘要的 JSON，不更改任何启动配置
        """
        return {
            "json": json.dumps(
                self.host.call("java.validate", {"runtime_ref": runtime_ref}, timeout=30), ensure_ascii=False
            )
        }

    def catalog(self) -> dict[str, str]:
        """
        查询当前主机推荐主版本的官方 JRE 包，不执行下载或安装。

        :return: 官方包摘要及下一页游标的 JSON
        """
        return {"json": json.dumps(self.host.call("java.catalog", timeout=30), ensure_ascii=False)}
