# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：验证本地背景视频流服务的令牌隔离、Range 响应与文件校验。
# ============================================================

from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ECL.host.background_media import BackgroundMediaService


def test_background_media_serves_only_the_authorized_video_range(tmp_path) -> None:
    media_file = tmp_path / "background.mp4"
    media_file.write_bytes(b"0123456789")
    service = BackgroundMediaService()

    try:
        url = service.open(str(media_file))
        request = Request(url, headers={"Range": "bytes=2-5"})
        with urlopen(request, timeout=2) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert response.headers["Content-Type"] == "video/mp4"
            assert response.read() == b"2345"

        service.revoke()
        with pytest.raises(HTTPError) as exc_info:
            urlopen(url, timeout=2)
        assert exc_info.value.code == 404
    finally:
        service.close()


def test_background_media_rejects_unsupported_or_oversized_files(tmp_path) -> None:
    media_file = tmp_path / "background.avi"
    media_file.write_bytes(b"video")
    service = BackgroundMediaService()

    try:
        with pytest.raises(ValueError, match="MP4"):
            service.open(str(media_file))
    finally:
        service.close()
