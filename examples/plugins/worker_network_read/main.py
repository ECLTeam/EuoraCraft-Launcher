# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：使用正式只读网络 API 读取已批准站点的小型资源，不取得宿主网络客户端。
#
# 公开接口：
#   - function read_metadata — 读取一次 GET 或 HEAD 并返回有限摘要。
#   - class Plugin — 注册公开测试资源读取示例。
# ============================================================

from __future__ import annotations

import base64

from ECL.plugin_api import HostApiError, HostClient
from ECL.plugin_api import Plugin as BasePlugin


def read_metadata(host: HostClient, url: str, method: str = "GET") -> dict[str, int]:
    """
    通过当前 run 的精确站点许可读取资源，只返回状态和完整实体长度。

    :param host: Worker 已绑定的公开 Host API
    :param url: 必须匹配已审阅站点的 URL
    :param method: GET 或 HEAD，不自动重试
    :return: HTTP 状态与实际响应体字节数
    :raises HostApiError: 未授权、超限或网络读取失败时抛出
    """
    result = host.call("network.request", {"method": method, "url": url}, timeout=30)
    status, encoded = result.get("status_code"), result.get("body_b64")
    if type(status) is not int or not isinstance(encoded, str):
        raise HostApiError("网络结果结构无效", "PLUGIN_SCHEMA_INVALID")
    return {"status_code": status, "body_bytes": len(base64.b64decode(encoded, validate=True))}


class Plugin(BasePlugin):
    """
    注册小型公开连接测试资源的读取命令，网络许可可由用户单独拒绝。
    """

    def on_load(self) -> None:
        """
        只登记命令，加载插件本身不发起网络请求。
        """
        self.define_command(
            "read",
            self.read,
            output_fields=({"name": "status_code", "type": "integer"}, {"name": "body_bytes", "type": "integer"}),
        )

    def read(self) -> dict[str, int]:
        """
        读取 Google 的公开连通性测试状态，不传送身份头或本机数据。

        :return: HTTP 状态与实际响应体长度
        :raises HostApiError: 权限或网络读取失败时抛出
        """
        return read_metadata(self.host, "https://connectivitycheck.gstatic.com/generate_204")
