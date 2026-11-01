# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：演示提供方自身表单和持久数据的往返，不连接外部认证服务器。
#
# 公开接口：
#   - class Plugin — 仅供接口验证的本地认证提供方。
# ============================================================

from __future__ import annotations

from ECL.plugin_api import JsonValue
from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    """
    只验证宿主接口，不声称输入的测试令牌能够登录真实 Minecraft 服务。
    """

    def on_enable(self) -> None:
        """
        注册本安装提供方，要求独立登录许可。
        """
        self.register_auth_provider(
            "demo",
            "本地认证接口示例",
            fields=[{"key": "token", "label": "测试令牌", "type": "password", "required": True}],
            authenticate=self.authenticate,
            resolve_credentials=self.resolve,
            description="只验证提供方自己的数据往返，不连接外部认证服务。",
        )

    def authenticate(self, fields: dict[str, JsonValue]) -> JsonValue:
        """
        校验测试表单并返回属于本提供方的不透明数据。

        :param fields: 宿主按表单交付的字段
        :return: 测试账户及其私有数据
        :raises ValueError: 测试令牌为空时抛出
        """
        token = fields.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("请填写测试令牌")
        return {"id": "demo", "alias": "DemoPlayer", "uuid": "0" * 31 + "1", "data": {"token": token}}

    def resolve(self, account: dict[str, JsonValue]) -> JsonValue:
        """
        读取自身账户持久数据，用于验证重启后的凭据解析。

        :param account: 该提供方自己的账户摘要及 data
        :return: 合成凭据，仅用于接口测试
        :raises ValueError: 自身数据缺失时抛出
        """
        private = account.get("data")
        if not isinstance(private, dict) or not isinstance(private.get("token"), str):
            raise ValueError("请重新登录")
        return {
            "player_name": "DemoPlayer",
            "uuid": "0" * 31 + "1",
            "user_type": "msa",
            "access_token": private["token"],
        }
