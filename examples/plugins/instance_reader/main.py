# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在 Worker 中通过授权资源引用查询实例，不接收宿主路径或服务对象。
#
# 公开接口：
#   - class Plugin — 登记返回获准实例数量和首个名称的示例命令。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    仅通过当前运行的 HostClient 查询用户明确选择的实例。
    """

    def on_load(self) -> None:
        """
        登记摘要命令，查询在实际调用时执行，避免缓存过期资源引用。
        """
        self.define_command(
            "summary",
            self.summary,
            output_fields=({"name": "count", "type": "integer"}, {"name": "first", "type": "string"}),
        )

    def summary(self) -> dict[str, int | str]:
        """
        遍历本运行获准资源及其分页，返回不含路径的简短结果。

        :return: 实例数量与第一个实例展示名称
        """
        names: dict[str, str] = {}
        cursor = None
        while True:
            page = self.host.call("resources.list", {"capability": "instances.read", "cursor": cursor})
            for resource in page["items"]:
                if resource["kind"] == "instance":
                    value = self.host.call("instances.get", {"instance_ref": resource["instance_ref"]})
                    names[value["instance_ref"]] = value["alias"]
                    continue
                scan_cursor = None
                while True:
                    scanned = self.host.call(
                        "instances.scan", {"root_ref": resource["root_ref"], "cursor": scan_cursor}
                    )
                    for value in scanned["items"]:
                        names[value["instance_ref"]] = value["alias"]
                    scan_cursor = scanned["next_cursor"]
                    if scan_cursor is None:
                        break
            cursor = page["next_cursor"]
            if cursor is None:
                break
        return {"count": len(names), "first": next(iter(names.values()), "")}
