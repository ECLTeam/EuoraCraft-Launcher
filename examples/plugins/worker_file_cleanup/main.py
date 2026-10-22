# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：显式提交自身私有文件按版本删除，展示实际效果与延迟清理。
#
# 公开接口：
#   - class Plugin — 提供删除提交、状态查询与取消命令，不在生命周期钩子清理文件。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import HostApiError
from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    只处理调用者明确选择的文件和原版本，不扫描或自动删除。
    """

    def on_load(self) -> None:
        """
        登记命令；删除许可在每次实际提交时检查。
        """
        self.define_command(
            "delete",
            self.delete,
            input_fields=tuple(
                {"name": name, "type": "string"} for name in ("root_ref", "name", "expected_etag", "mutation_key")
            ),
            output_fields=({"name": "operation_ref", "type": "string"}, {"name": "state", "type": "string"}),
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=tuple({"name": name, "type": "string"} for name in ("state", "effect", "error_code"))
            + tuple({"name": name, "type": "bool"} for name in ("cleanup_pending", "deleted")),
        )
        self.define_command(
            "cancel",
            self.cancel,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=({"name": "requested", "type": "bool"},),
        )

    def delete(self, root_ref: str, name: str, expected_etag: str, mutation_key: str) -> dict[str, JsonValue]:
        """
        提交一个明确目标和原版本；调用者须保留去重键直到收到回执。

        等待失败不代表删除未接纳，使用原键查询 mutations.get，不换键重删。

        :param root_ref: 当前运行发现的自身根引用
        :param name: 明确选择的平铺文件名
        :param expected_etag: 先前取得的完整原版本
        :param mutation_key: 调用方保留的本次删除去重键
        :return: 任务接纳回执，尚不代表删除完成
        """
        return self.host.call(
            "files.delete",
            {
                "target": {"root_ref": root_ref, "relative_path": name},
                "expected_etag": expected_etag,
            },
            mutation_key=mutation_key,
        )

    def status(self, operation_ref: str) -> dict[str, JsonValue]:
        """
        查询真实效果与待清理标志；unknown 即使后来清理完成也不伪造成功。

        :param operation_ref: 本次运行的任务回执
        :return: 最小安全任务确认；失权时 deleted 为 false
        """
        view = self.host.call("operations.get", {"operation_ref": operation_ref})
        if type(view.get("cleanup_pending")) is not bool:
            raise HostApiError("任务清理状态无效", "PLUGIN_SCHEMA_INVALID")
        return {name: view[name] for name in ("state", "effect", "cleanup_pending")} | {
            "error_code": view.get("error_code") or "",
            "deleted": view.get("result") == {"deleted": True},
        }

    def cancel(self, operation_ref: str) -> dict[str, JsonValue]:
        """
        请求取消提交前的任务；停止必须通过后续状态确认。

        :param operation_ref: 本次运行的任务回执
        :return: 取消请求是否已接纳
        """
        view = self.host.call("operations.cancel", {"operation_ref": operation_ref})
        return {"requested": view["requested"]}
