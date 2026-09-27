# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：主页信息卡服务：公告/小贴士内容与远程公告拉取。
#
# 公开接口：
#   - class InfoCardManager — 组装首页信息卡数据，并管理远程公告的拉取、校验与本地缓存。
#       - get_info_card() -> dict[str, Any] — 返回首页轮播模式、提示、公告和欢迎卡片数据。
# ============================================================

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any

import httpx

from ECL.utils import atomic_write_text, get_logger, get_with_retries

NoticeLoader = Callable[[str], Any]
Clock = Callable[[], datetime]
LocalizedAnnouncement = dict[str, str]
Announcement = dict[str, str | dict[str, LocalizedAnnouncement]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _get_without_ssl_verify(url: str, **kwargs: Any) -> httpx.Response:
    # 在未注入共享客户端时保留历史的宽松证书校验策略。
    return httpx.get(url, verify=False, **kwargs)


class InfoCardManager:
    """
    组装首页信息卡数据，并管理远程公告的拉取、校验与本地缓存。

    公告在配置的刷新间隔内复用内存结果；远程拉取失败时回退到磁盘缓存，本地
    缓存同样损坏时才返回空公告列表，始终不阻断首页渲染。

    :param data_path: 启动器数据目录，用于持久化公告缓存
    """

    notice_url = "https://api.eclteam.top/raw/ECLTeam/EuoraCraft-Launcher.API/main/notice.json"
    notice_schema_version = 1
    notice_refresh_seconds = 300.0
    notice_timeout_seconds = 5.0
    default_info_card = {
        "mode": "rotate",
        "tip_title": "你知道吗",
        "announcement_title": "公告",
        "tips": [
            "可以在设置中调整游戏内存、窗口大小和 Java 路径。",
            "可以在版本管理中为不同游戏版本保存独立设置。",
            "账户管理支持离线账户与 Microsoft 正版账户。",
            "插件、日志和账户等可变数据统一保存在 ECL_data 目录。",
        ],
        "welcome": {
            "title": "欢迎使用 EuoraCraft Launcher",
            "content": "选择账户和游戏版本后即可开始游戏。",
        },
        "interval": 8000,
    }

    def __init__(
        self,
        data_path: Path | str,
        *,
        notice_loader: NoticeLoader | None = None,
        http_client: httpx.Client | None = None,
        clock: Clock = _utc_now,
        refresh_seconds: float | None = None,
        request_timeout: float | None = None,
        request_retries: int = 2,
    ):
        self.logger = get_logger("InfoCardManager")
        self.data_path = Path(data_path)
        self.notice_cache_path = self.data_path / "notice.json"
        self.http = http_client
        self._notice_loader = notice_loader or self._download_notice
        self._clock = clock
        self._refresh_seconds = max(0.0, self.notice_refresh_seconds if refresh_seconds is None else refresh_seconds)
        self._request_timeout = max(
            1.0, float(self.notice_timeout_seconds if request_timeout is None else request_timeout)
        )
        self._request_retries = max(0, int(request_retries))
        self._lock = RLock()
        self._last_refresh_at: float | None = None
        self._announcements: list[Announcement] | None = None
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _download_notice(self, url: str) -> Any:
        # 公告请求使用启动器的网络设置，并限制连接阶段不超过总超时。
        timeout = httpx.Timeout(self._request_timeout, connect=min(3.0, self._request_timeout))
        headers = {
            "Accept": "application/json",
            "User-Agent": "EuoraCraft-Launcher",
        }
        # 注入客户端沿用其 SSL 策略；否则降级为历史的宽松证书校验请求。
        request = self.http.get if self.http is not None else _get_without_ssl_verify
        response = get_with_retries(
            request,
            url,
            retries=self._request_retries,
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("公告时间必须是 ISO 8601 字符串")
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _normalize_locales(value: Any) -> dict[str, LocalizedAnnouncement]:
        """
        校验远程公告的多语言字段并剔除不完整翻译。

        远程数据不能直接透传到前端；每个语言项必须同时包含非空标题与正文，
        以便前端能够在当前语言、中文和根级默认文案之间稳定回退。

        :param value: 远程公告的 locales 字段
        :return: 按语言代码索引的有效标题和正文
        """
        if not isinstance(value, dict):
            return {}

        locales: dict[str, LocalizedAnnouncement] = {}
        for locale, translation in value.items():
            if not isinstance(locale, str) or not locale.strip() or not isinstance(translation, dict):
                continue
            title = translation.get("title")
            content = translation.get("content")
            if not all(isinstance(item, str) and item.strip() for item in (title, content)):
                continue
            locales[locale.strip()] = {"title": title.strip(), "content": content.strip()}
        return locales

    @classmethod
    def _create_announcement(cls, item: dict[str, Any], notice_id: str, title: str, content: str) -> Announcement:
        """
        构建面向前端的公告数据，并保留经过校验的可选翻译表。

        :param item: 已通过公告基础字段校验的远程条目
        :param notice_id: 规范化后的公告标识
        :param title: 根级回退标题
        :param content: 根级回退正文
        :return: 可安全返回给前端的公告数据
        """
        date = item.get("date")
        announcement: Announcement = {
            "id": notice_id,
            "title": title.strip(),
            "date": date.strip() if isinstance(date, str) else "",
            "content": content.strip(),
        }
        locales = cls._normalize_locales(item.get("locales"))
        if locales:
            announcement["locales"] = locales
        return announcement

    @classmethod
    def _normalize_announcements(cls, data: Any, now: datetime) -> list[Announcement]:
        if not isinstance(data, dict):
            raise ValueError("远程公告根节点必须是对象")
        if data.get("schema_version") != cls.notice_schema_version:
            raise ValueError(f"不支持的公告数据版本: {data.get('schema_version')!r}")

        raw_announcements = data.get("announcements")
        if not isinstance(raw_announcements, list):
            raise ValueError("远程公告 announcements 必须是数组")

        current_time = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        current_time = current_time.astimezone(UTC)
        normalized: list[tuple[int, int, Announcement]] = []
        seen_ids: set[str] = set()

        for index, item in enumerate(raw_announcements):
            if not isinstance(item, dict) or item.get("enabled", True) is not True:
                continue

            notice_id = item.get("id")
            title = item.get("title")
            content = item.get("content")
            if not all(isinstance(value, str) and value.strip() for value in (notice_id, title, content)):
                continue

            normalized_id = notice_id.strip()
            if normalized_id in seen_ids:
                continue

            try:
                start_at = cls._parse_timestamp(item.get("start_at"))
                end_at = cls._parse_timestamp(item.get("end_at"))
            except ValueError:
                continue

            if start_at is not None and current_time < start_at:
                continue
            if end_at is not None and current_time >= end_at:
                continue

            priority = item.get("priority", 0)
            if not isinstance(priority, int) or isinstance(priority, bool):
                priority = 0

            seen_ids.add(normalized_id)
            announcement = cls._create_announcement(item, normalized_id, title, content)
            normalized.append((priority, index, announcement))

        normalized.sort(key=lambda entry: (-entry[0], entry[1]))
        return [announcement for _, _, announcement in normalized]

    def _write_notice_cache(self, data: Any) -> None:
        atomic_write_text(self.notice_cache_path, json.dumps(data, ensure_ascii=False, indent=2))

    def _read_cached_announcements(self, now: datetime) -> list[Announcement]:
        data = json.loads(self.notice_cache_path.read_text(encoding="utf-8"))
        return self._normalize_announcements(data, now)

    def _load_announcements(self) -> list[Announcement]:
        refresh_started_at = monotonic()
        if (
            self._announcements is not None
            and self._last_refresh_at is not None
            and refresh_started_at - self._last_refresh_at < self._refresh_seconds
        ):
            return deepcopy(self._announcements)

        now = self._clock()
        try:
            remote_data = self._notice_loader(self.notice_url)
            announcements = self._normalize_announcements(remote_data, now)
            self._write_notice_cache(remote_data)
        except (httpx.HTTPError, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.logger.warning("获取远程公告失败，将尝试使用本地缓存: %s", exc)
            try:
                announcements = self._read_cached_announcements(now)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as cache_exc:
                self.logger.warning("读取公告缓存失败，将返回空公告列表: %s", cache_exc)
                announcements = []

        self._announcements = announcements
        self._last_refresh_at = refresh_started_at
        return deepcopy(announcements)

    def get_info_card(self) -> dict[str, Any]:
        """
        返回首页轮播模式、提示、公告和欢迎卡片数据。
        """
        with self._lock:
            data = deepcopy(self.default_info_card)
            data["announcements"] = self._load_announcements()
            return data
