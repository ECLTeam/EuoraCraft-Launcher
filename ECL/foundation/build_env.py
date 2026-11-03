# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：构建期注入的 Microsoft、LittleSkin 认证配置与 CurseForge API Key。
#
# 公开接口：
#   - class BuildEnvironment — 构建期注入的认证配置。
# ============================================================

from __future__ import annotations


class BuildEnvironment:
    """
    构建期注入的第三方认证配置。

    发布工作流仅替换类属性赋值，不在源码或运行日志中输出密钥内容。
    """

    microsoft_client_id = ""  # Microsoft OAuth 客户端 ID，构建时注入
    curseforge_api_key = ""  # CurseForge API Key，构建时注入
    littleskin_client_id = ""  # LittleSkin OAuth 应用 ID，构建时注入
    littleskin_client_secret = ""  # LittleSkin 授权代码流密钥，构建时注入
    littleskin_redirect_uri = ""  # 可选覆盖本机回调默认地址
