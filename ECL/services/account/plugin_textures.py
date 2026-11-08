# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为已授权账户读取有界皮肤 PNG，不向插件交付纹理地址或凭据。
#
# 公开接口：
#   - read_account_skin() — 返回指定账户的已验证 PNG 字节。
# ============================================================

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING

import httpx
from PIL import Image

from ECL.utils.errors import AccountError

if TYPE_CHECKING:
    from ECL.services.account.accounts import AccountManager


def read_account_skin(accounts: AccountManager, client: httpx.Client, account_id: str) -> bytes | None:
    """
    按账户服务现有纹理来源读取 PNG，下载和解码均限制大小。

    调用方必须先验证精确账户授权；此函数不接受插件提供的 URL 或本地路径。

    :param accounts: 应用账户服务
    :param client: 应用共享 HTTP 客户端
    :param account_id: 宿主从已授权目录解析的账户身份
    :return: 至多 512 KiB 的 PNG；账户无皮肤时为空
    :raises AccountError: 纹理读取或图片校验失败
    """
    source = accounts.texture_urls(account_id).get("skinUrl")
    if not source:
        return None
    try:
        if source.startswith("data:image/png;base64,"):
            if len(source) > 700000:
                raise ValueError("skin limit")
            content = base64.b64decode(source.split(",", 1)[1], validate=True)
        else:
            chunks = []
            size = 0
            with client.stream("GET", source, timeout=10) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(8192):
                    size += len(chunk)
                    if size > 512 * 1024:
                        raise ValueError("skin limit")
                    chunks.append(chunk)
            content = b"".join(chunks)
        if len(content) > 512 * 1024:
            raise ValueError("skin limit")
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG" or max(image.size) > 2048:
                raise ValueError("skin format")
            image.verify()
        return content
    except (httpx.HTTPError, ValueError, OSError, Image.DecompressionBombError) as exc:
        raise AccountError("皮肤图片无法读取", "PLUGIN_ACCOUNT_SKIN_FAILED") from exc
