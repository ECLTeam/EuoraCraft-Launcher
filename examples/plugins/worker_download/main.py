# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：通过公开 Host API 下载到插件私有空间，加载时只注册命令，不主动联网。
#
# 公开接口：
#   - submit_download — 接纳一次有限的新文件下载。
#   - class Plugin — 提供下载、状态和取消示例命令。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import HostApiError, HostClient
from ECL.plugin_api import Plugin as BasePlugin


def submit_download(host: HostClient, url: str, name: str, mutation_key: str, sha256: str = "") -> dict[str, str]:
    """
    将调用方明确给出的公网站点资源下载到新命名私有文件，不自动重试。

    :param host: 当前 Worker 的公开宿主接口
    :param url: 必须在清单声明和用户批准范围中的 URL
    :param name: 非空平铺文件名，目标必须不存在
    :param mutation_key: 调用方保留的提交去重键，等待超时后可用它查询回执
    :param sha256: 可选的期望 SHA-256；空字符串表示不提供
    :return: 自身任务引用与后续读取所需的逻辑文件引用
    :raises HostApiError: 未授权、路径或提交失败时抛出
    """
    if not mutation_key:
        raise HostApiError("下载必须提供提交去重键", "PLUGIN_SCHEMA_INVALID")
    roots = host.call("resources.list", {"capability": "plugin.base"}).get("items")
    if not isinstance(roots, list) or not roots or not isinstance(roots[0], dict):
        raise HostApiError("私有文件根不可用", "PLUGIN_SCHEMA_INVALID")
    root_ref = roots[0].get("root_ref")
    if not isinstance(root_ref, str):
        raise HostApiError("私有文件根结构无效", "PLUGIN_SCHEMA_INVALID")
    params = {"url": url, "target": {"root_ref": root_ref, "relative_path": name}, "overwrite": False}
    if sha256:
        params["expected_sha256"] = sha256
    receipt = host.call("downloads.submit", params, mutation_key=mutation_key)
    reference = receipt.get("operation_ref")
    if not isinstance(reference, str):
        raise HostApiError("下载任务回执无效", "PLUGIN_SCHEMA_INVALID")
    return {"operation_ref": reference, "root_ref": root_ref, "name": name}


class Plugin(BasePlugin):
    """
    只在明确执行 submit 命令后开始下载，状态与取消沿用当前运行的自身任务。
    """

    def on_load(self) -> None:
        """
        注册命令，不发送网络请求或初始化私有文件空间。
        """
        self.define_command(
            "submit",
            self.submit,
            input_fields=(
                {"name": "url", "type": "string"},
                {"name": "mutation_key", "type": "string", "max_length": 64},
                {"name": "name", "type": "string", "max_length": 120},
                {"name": "sha256", "type": "string", "required": False, "max_length": 64},
            ),
            output_fields=tuple({"name": name, "type": "string"} for name in ("operation_ref", "root_ref", "name")),
        )
        self.define_command(
            "status",
            self.status,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=(
                {"name": "state", "type": "string"},
                {"name": "effect", "type": "string"},
                {"name": "error_code", "type": "string"},
                {"name": "percent", "type": "number"},
                {"name": "size", "type": "integer"},
            ),
        )
        self.define_command(
            "cancel",
            self.cancel,
            input_fields=({"name": "operation_ref", "type": "string"},),
            output_fields=({"name": "requested", "type": "bool"},),
        )

    def submit(self, url: str, name: str, mutation_key: str, sha256: str = "") -> dict[str, str]:
        """
        提交一个只新建文件的下载，返回不表示文件已经发布。

        :param url: 已批准公网站点的资源 URL
        :param name: 新文件名
        :param mutation_key: 调用方保留的原始去重键
        :param sha256: 可选期望摘要
        :return: 自身任务和文件逻辑引用
        """
        return submit_download(self.host, url, name, mutation_key, sha256)

    def status(self, operation_ref: str) -> dict[str, str | float | int]:
        """
        只查询当前运行的任务，缺少独立读权限时不交付文件大小。

        :param operation_ref: submit 返回的任务引用
        :return: 执行状态、实际效果、进度和可读取的大小，隐藏时大小为 -1
        """
        view = self.host.call("operations.get", {"operation_ref": operation_ref})
        state, effect, percent = view.get("state"), view.get("effect"), view.get("percent")
        if not isinstance(state, str) or not isinstance(effect, str) or type(percent) not in (int, float):
            raise HostApiError("下载任务视图无效", "PLUGIN_SCHEMA_INVALID")
        result = view.get("result")
        size = result.get("size") if isinstance(result, dict) else -1
        if type(size) is not int:
            raise HostApiError("下载结果大小无效", "PLUGIN_SCHEMA_INVALID")
        code = view.get("error_code")
        if code is not None and not isinstance(code, str):
            raise HostApiError("下载错误结构无效", "PLUGIN_SCHEMA_INVALID")
        return {"state": state, "effect": effect, "percent": percent, "error_code": code or "", "size": size}

    def cancel(self, operation_ref: str) -> dict[str, bool]:
        """
        请求取消自身下载，实际停止须继续通过 status 确认。

        :param operation_ref: submit 返回的任务引用
        :return: 是否接纳取消请求
        """
        result = self.host.call("operations.cancel", {"operation_ref": operation_ref})
        requested = result.get("requested")
        if type(requested) is not bool:
            raise HostApiError("下载取消结构无效", "PLUGIN_SCHEMA_INVALID")
        return {"requested": requested}
