# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：按显式原版本上传替换文本，仅查询本运行任务，不在生命周期自动改写文件。
#
# 公开接口：
#   - class Plugin — 提供替换提交、状态和取消命令。
# ============================================================

from __future__ import annotations

import base64
import hashlib

from ECL.plugin_api import HostApiError
from ECL.plugin_api import Plugin as BasePlugin
from ECL.plugin_api.contracts import JsonValue


class Plugin(BasePlugin):
    """
    只处理调用者明确选择的原文件与版本，恢复入口由可信主窗口提供。
    """

    def on_load(self) -> None:
        """
        登记命令，不自动初始化或修改文件。
        """
        self.define_command(
            "replace",
            self.replace,
            input_fields=tuple(
                {"name": name, "type": "string"}
                for name in ("root_ref", "name", "expected_etag", "text", "mutation_key")
            ),
            output_fields=({"name": "operation_ref", "type": "string"}, {"name": "state", "type": "string"}),
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=(
                *({"name": name, "type": "string"} for name in ("state", "effect", "error_code")),
                {"name": "cleanup_pending", "type": "bool"},
            ),
        )
        self.define_command(
            "cancel",
            self.cancel,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=({"name": "requested", "type": "bool"},),
        )

    def replace(
        self, root_ref: str, name: str, expected_etag: str, text: str, mutation_key: str
    ) -> dict[str, JsonValue]:
        """
        接纳一次原版本替换；调用者保留原键，提交超时不能换键重做。

        :param root_ref: 当前运行的私有根引用
        :param name: 已有文件的平铺名称
        :param expected_etag: 已合法取得的原版本
        :param text: 用于示例的有限新文本
        :param mutation_key: 调用者保留的本次动作键，至多80字符
        :return: queued任务回执，不表示已发布
        """
        if not mutation_key or len(mutation_key) > 80:
            raise HostApiError("请提供有限的替换动作键", "PLUGIN_SCHEMA_INVALID")
        payload = text.encode("utf-8")
        upload = self.host.call(
            "files.replace_begin",
            {
                "target": {"root_ref": root_ref, "relative_path": name},
                "expected_etag": expected_etag,
                "total_bytes": len(payload),
            },
            mutation_key=mutation_key + "-begin",
        )
        write_ref = upload.get("write_ref")
        if not isinstance(write_ref, str):
            raise HostApiError("替换上传引用无效", "PLUGIN_SCHEMA_INVALID")
        try:
            for offset in range(0, len(payload), 32768):
                self.host.call(
                    "files.write_chunk",
                    {
                        "write_ref": write_ref,
                        "offset": offset,
                        "data_b64": base64.b64encode(payload[offset : offset + 32768]).decode("ascii"),
                    },
                    mutation_key=f"{mutation_key}-chunk-{offset}",
                )
            return self.host.call(
                "files.write_commit",
                {
                    "write_ref": write_ref,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
                mutation_key=mutation_key + "-commit",
            )
        finally:
            self.host.call("files.write_abort", {"write_ref": write_ref})

    def status(self, operation_ref: str) -> dict[str, JsonValue]:
        """
        查询真实副作用及清理标志，unknown不随恢复结束伪造为成功。

        :param operation_ref: 本次运行返回的任务引用
        :return: 最小任务状态
        """
        view = self.host.call("operations.get", {"operation_ref": operation_ref})
        return {name: view[name] for name in ("state", "effect", "cleanup_pending")} | {
            "error_code": view.get("error_code") or "",
        }

    def cancel(self, operation_ref: str) -> dict[str, JsonValue]:
        """
        请求取消尚未接受提交的替换，实际停止需继续查询。

        :param operation_ref: 本次运行返回的任务引用
        :return: 取消请求是否已接纳
        """
        return {"requested": self.host.call("operations.cancel", {"operation_ref": operation_ref})["requested"]}
