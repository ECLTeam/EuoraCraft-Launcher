# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过公开 Host API 导出唯一命名的 JSON 快照，上传完成后由宿主任务发布。
#
# 公开接口：
#   - function export_snapshot — 创建一次快照上传并返回任务接纳信息。
#   - class Plugin — 提供导出和查询导出状态的示例命令。
# ============================================================

from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

from ECL.plugin_api import HostApiError, HostClient
from ECL.plugin_api import Plugin as BasePlugin


def export_snapshot(host: HostClient) -> dict[str, str]:
    """
    通过有限分块写入运行信息快照，不访问宿主路径、不覆盖已有导出。

    提交等待失败不代表任务没有接受，因此只清理仍归上传会话所有的暂存。

    :param host: Worker 已绑定的公开 Host API
    :return: 任务引用与文件的逻辑名称、根引用
    :raises HostApiError: 权限、空间或上传失败时抛出
    """
    roots = host.call("resources.list", {"capability": "filesystem.write"})["items"]
    if not isinstance(roots, list) or not roots or not isinstance(roots[0], dict):
        raise HostApiError("私有文件根不可用", "PLUGIN_SCHEMA_INVALID")
    reference = roots[0].get("root_ref")
    if not isinstance(reference, str):
        raise HostApiError("私有文件根结构无效", "PLUGIN_SCHEMA_INVALID")
    name = f"snapshot-{uuid4().hex}.json"
    payload = json.dumps(host.call("runtime.info"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    upload = host.call(
        "files.write_begin",
        {
            "target": {"root_ref": reference, "relative_path": name},
            "total_bytes": len(payload),
            "overwrite": False,
        },
    )
    write_ref = upload.get("write_ref")
    if not isinstance(write_ref, str):
        raise HostApiError("上传引用无效", "PLUGIN_SCHEMA_INVALID")
    try:
        for offset in range(0, len(payload), 32768):
            host.call(
                "files.write_chunk",
                {
                    "write_ref": write_ref,
                    "offset": offset,
                    "data_b64": base64.b64encode(payload[offset : offset + 32768]).decode("ascii"),
                },
            )
        receipt = host.call(
            "files.write_commit", {"write_ref": write_ref, "sha256": hashlib.sha256(payload).hexdigest()}
        )
        operation_ref = receipt.get("operation_ref")
        if not isinstance(operation_ref, str):
            raise HostApiError("任务回执无效", "PLUGIN_SCHEMA_INVALID")
        return {"operation_ref": operation_ref, "name": name, "root_ref": reference}
    finally:
        # 已交给任务时 abort 返回 false；它不会取消已经接纳的发布。
        host.call("files.write_abort", {"write_ref": write_ref})


class Plugin(BasePlugin):
    """
    独立运行的快照导出示例，上传成功和发布完成通过不同命令确认。
    """

    def on_load(self) -> None:
        """
        登记公开命令；可选文件能力由每次实际调用检查。
        """
        self.define_command(
            "export",
            self.export,
            output_fields=(
                {"name": "operation_ref", "type": "string"},
                {"name": "name", "type": "string"},
                {"name": "root_ref", "type": "string"},
            ),
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=({"name": "state", "type": "string"}, {"name": "effect", "type": "string"}),
        )

    def export(self) -> dict[str, str]:
        """
        接纳一次导出任务，返回时尚不声称文件已经发布。

        :return: 后续查询任务及文件所需的逻辑引用
        """
        return export_snapshot(self.host)

    def status(self, operation_ref: str) -> dict[str, str]:
        """
        查询本次运行自己的任务状态，不读取其他来源的任务。

        :param operation_ref: export 返回的任务引用
        :return: 当前执行状态和实际效果
        """
        view = self.host.call("operations.get", {"operation_ref": operation_ref})
        if not isinstance(view.get("state"), str) or not isinstance(view.get("effect"), str):
            raise HostApiError("任务视图无效", "PLUGIN_SCHEMA_INVALID")
        return {"state": view["state"], "effect": view["effect"]}
