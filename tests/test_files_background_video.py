# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证背景视频 IPC 从独立视频配置分支读取路径。
# ============================================================

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from ECL.api.files import FileHandlers


@pytest.mark.anyio
async def test_background_video_open_prefers_nested_video_path() -> None:
    """
    使用新配置结构签发背景视频地址。

    ``ui.background.video.path`` 是新版唯一的持久化视频路径；旧版扁平 ``path``
    即使仍存在，也不能覆盖它。
    """

    opened_paths: list[str] = []
    handler = object.__new__(FileHandlers)
    handler.config = SimpleNamespace(
        get_config=lambda section: {
            "background": {
                "media_type": "video",
                "path": "C:/legacy-background.mp4",
                "video": {"path": "C:/saved-background.mp4"},
            }
        }
    )
    handler.media_protocol = SimpleNamespace(
        register_local=lambda path: opened_paths.append(path) or "http://ecl-media.localhost/token/background.mp4"
    )

    result = await handler.background_video_open({})

    assert result == {"success": True, "data": {"url": "http://ecl-media.localhost/token/background.mp4"}}
    assert opened_paths == ["C:/saved-background.mp4"]


@pytest.mark.anyio
async def test_image_read_success_is_debug_logged(tmp_path, caplog) -> None:
    """
    成功读取图片只记录调试日志，避免列表重渲染污染常规运行日志。

    文件不存在等异常路径仍由处理器保留警告级别日志，不能因降低成功日志而隐藏故障。
    """
    image_path = tmp_path / "logo.png"
    image_path.write_bytes(b"png")
    handler = object.__new__(FileHandlers)
    handler.logger = logging.getLogger("test.image_read")

    with caplog.at_level(logging.DEBUG, logger="test.image_read"):
        result = await handler.image_read_file({"path": str(image_path)})

    assert result["success"] is True
    records = [record for record in caplog.records if "图片读取成功" in record.message]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
