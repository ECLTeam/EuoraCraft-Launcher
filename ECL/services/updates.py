# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：启动器版本更新检测与公告读取：发布信息查询、公告校验和离线缓存。
#
# 公开接口：
#   - parse_version(version) -> tuple[tuple[int, ...], tuple[int, int] | None] | None — 解析 SemVer 版本为可比较结构，忽略构建元数据。
#   - compare_versions(left, right) -> int — 按 SemVer 规则比较两个版本号：left 小于 right 返回负数、相等返回 0、大于返回正数。
#   - class UpdateCheckResult — 一次版本检测的结果。
#   - class UpdateChecker — 按当前版本通道检查 GitHub Releases 是否有新版本。
#       - check() -> UpdateCheckResult
#       - latest_release() -> dict[str, Any] | None — 返回当前通道匹配到的最高版本 Release 对象，供自动更新提取安装包。
#   - class LocalizedAnnouncement — 一种语言的公告标题与正文。
#   - class Announcement — 已校验的公告数据。
#   - class AnnouncementService — 获取公告并管理内存刷新与离线缓存。
#       - get_announcements() -> list[Announcement] — 返回有效公告的独立副本。
#   - class StartupUpdateService — 后端启动后在后台检查一次更新，并保存检查结果。
# ============================================================

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import Any, NotRequired, TypedDict

import httpx

from ECL.foundation import EventBus
from ECL.utils import atomic_write_text, get_logger, get_with_retries


def parse_version(version: str) -> tuple[tuple[int, ...], tuple[int, int] | None] | None:
    """
    解析 SemVer 版本为可比较结构，忽略构建元数据。

    :param version: 版本号，如 ``v1.4.2-alpha.3+20260906`` 或 ``1.4.2``
    :return: ``(release 段, 预发布标识)``；预发布标识为 ``(通道优先级, 序号)``，无法解析时返回 None
    """
    text = version.strip().lstrip("vV")
    text = text.split("+", 1)[0]
    prerelease = None
    if "-" in text:
        text, _, marker = text.partition("-")
        marker = marker.strip()
        if marker:
            parts = marker.split(".")
            order = UpdateChecker.prerelease_order.get(parts[0], 99)
            try:
                number = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                return None
            prerelease = (order, number)
    try:
        release = tuple(int(part) for part in text.split("."))
    except ValueError:
        return None
    if not release:
        return None
    return release, prerelease


def compare_versions(left: str, right: str) -> int:
    """
    按 SemVer 规则比较两个版本号：left 小于 right 返回负数、相等返回 0、大于返回正数。

    无法解析的版本视为相等，避免误报更新。

    :param left: 左侧版本号
    :param right: 右侧版本号
    """
    left_parsed = parse_version(left)
    right_parsed = parse_version(right)
    if left_parsed is None or right_parsed is None:
        return 0
    left_release, left_pre = left_parsed
    right_release, right_pre = right_parsed
    for index in range(max(len(left_release), len(right_release))):
        left_part = left_release[index] if index < len(left_release) else 0
        right_part = right_release[index] if index < len(right_release) else 0
        if left_part != right_part:
            return left_part - right_part
    if left_pre is None and right_pre is None:
        return 0
    if left_pre is None:
        return 1
    if right_pre is None:
        return -1
    return (left_pre[0] - right_pre[0]) or (left_pre[1] - right_pre[1])


@dataclass
class UpdateCheckResult:
    """
    一次版本检测的结果。

    :param status: disabled=通道不检测 / up_to_date=已是最新 / update_available=有新版本 / error=检测失败
    :param current_version: 当前启动器版本
    :param channel: 检测通道，alpha=预发布禁用 / beta=测试版 / release=正式版
    :param latest_version: 通道内最新版本号，无可用版本或失败时为 None
    :param latest_url: 最新版本的 GitHub Release 页面地址
    :param latest_notes: 最新版本的更新说明（Release 正文）
    :param message: 失败原因等补充信息
    """

    status: str
    current_version: str
    channel: str
    latest_version: str | None = None
    latest_url: str | None = None
    latest_notes: str | None = None
    message: str | None = None


class UpdateChecker:
    """
    按当前版本通道检查 GitHub Releases 是否有新版本。

    版本类型为 alpha 时直接禁用检测；beta/rc 查询预发布版本，正式版只查询
    正式 release，避免把测试版本误报给正式版用户。

    :param http_client: 共享的启动器通道 HTTP 客户端
    :param current_version: 当前启动器版本号
    :param version_type: 当前版本类型（alpha / beta / rc / release）
    """

    releases_api = "https://api.github.com/repos/ECLTeam/EuoraCraft-Launcher/releases"
    releases_per_page = 50
    request_timeout_seconds = 10.0
    prerelease_order = {"alpha": 0, "beta": 1, "rc": 2}

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        current_version: str,
        version_type: str,
        request_timeout: float | None = None,
        request_retries: int = 2,
    ):
        self.logger = get_logger("UpdateChecker")
        self.http = http_client
        self.current_version = current_version
        self.version_type = version_type or "release"
        self._request_timeout = max(
            1.0, float(self.request_timeout_seconds if request_timeout is None else request_timeout)
        )
        self._request_retries = max(0, int(request_retries))

    def check(self) -> UpdateCheckResult:
        channel = self._channel()
        self.logger.info("开始检查更新：当前版本 %s，通道 %s", self.current_version, channel)
        base = UpdateCheckResult(
            status="error",
            current_version=self.current_version,
            channel=channel,
        )
        releases = self._fetch_releases(base)
        if releases is None:
            self.logger.warning("检查更新失败：%s", base.message)
            return base
        # alpha 查看全部版本类型的最新版本；beta/rc 只看预发布；正式版只看正式 release
        want_prerelease = None if channel == "alpha" else (channel == "beta")
        best_tag, best_release = self._select_best_release(releases, want_prerelease=want_prerelease)
        if best_release is None:
            base.status = "up_to_date"
            self.logger.info("未找到可更新的版本，当前已是最新")
            return base
        if compare_versions(best_tag, self.current_version) > 0:
            base.status = "update_available"
            base.latest_version = best_tag.lstrip("vV")
            base.latest_url = str(best_release.get("html_url") or "") or None
            base.latest_notes = str(best_release.get("body") or "").strip() or None
            self.logger.info("发现新版本：%s", base.latest_version)
            return base
        base.status = "up_to_date"
        self.logger.info("当前已是最新版本")
        return base

    def latest_release(self) -> dict[str, Any] | None:
        """
        返回当前通道匹配到的最高版本 Release 对象，供自动更新提取安装包。

        :return: 最高版本的 Release 对象；检测失败或无可用版本时返回 None
        """
        channel = self._channel()
        releases = self._fetch_releases(
            UpdateCheckResult(status="error", current_version=self.current_version, channel=channel)
        )
        if releases is None:
            return None
        want_prerelease = None if channel == "alpha" else (channel == "beta")
        best_tag, best_release = self._select_best_release(releases, want_prerelease=want_prerelease)
        if best_release is None or compare_versions(best_tag, self.current_version) <= 0:
            return None
        self.logger.info("自动更新可用版本：%s", best_tag)
        return best_release

    def _channel(self) -> str:
        # 按版本类型映射检测通道，alpha 归入预发布通道。
        if self.version_type == "alpha":
            return "alpha"
        return "beta" if self.version_type in ("beta", "rc") else "release"

    def _fetch_releases(self, base: UpdateCheckResult) -> list[dict[str, Any]] | None:
        """
        请求 GitHub Releases 并返回解析结果，失败时写入提示信息并返回 None。

        :param base: 用于记录失败提示的检测结果载体
        :return: Release 列表；请求或解析失败时返回 None
        """
        try:
            response = get_with_retries(
                self.http.get,
                self.releases_api,
                retries=self._request_retries,
                timeout=self._request_timeout,
                params={"per_page": self.releases_per_page},
                headers={"Accept": "application/vnd.github+json"},
            )
        except httpx.RequestError as exc:
            self.logger.warning("版本检测请求失败: %s", exc)
            base.message = "无法连接更新服务器，请检查网络后重试"
            return None
        if response.status_code != 200:
            self.logger.warning("版本检测接口返回异常状态: %s", response.status_code)
            base.message = "更新服务器响应异常，请稍后重试"
            return None
        try:
            releases = response.json()
        except ValueError:
            base.message = "更新服务器数据解析失败，请稍后重试"
            return None
        if not isinstance(releases, list):
            base.message = "更新服务器数据格式异常，请稍后重试"
            return None
        return releases

    @staticmethod
    def _select_best_release(
        releases: list[dict[str, Any]], *, want_prerelease: bool | None
    ) -> tuple[str, dict[str, Any] | None]:
        """
        按通道筛选 Release 列表并返回其中版本号最高的一项。

        :param releases: GitHub Releases 返回的 Release 列表
        :param want_prerelease: 是否只看预发布版本；None 表示不过滤全部版本
        :return: ``(最高版本号, 对应 Release 对象)``；无可匹配项时返回空串与 None
        """
        best_tag = ""
        best_release: dict[str, Any] | None = None
        for release in releases:
            if not isinstance(release, dict):
                continue
            if want_prerelease is not None and bool(release.get("prerelease")) != want_prerelease:
                continue
            tag = str(release.get("tag_name") or "").strip()
            if parse_version(tag) is None:
                continue
            if best_tag and compare_versions(tag, best_tag) <= 0:
                continue
            best_tag = tag
            best_release = release
        return best_tag, best_release


class StartupUpdateService:
    """
    后端启动后在后台检查一次更新，并保存检查结果。

    后端启动时开始检查更新，WebView 刷新不会重新触发检查。检测完成后通过事件总线发送
    ``update:check_completed``；前端较晚就绪时可从 ``result`` 读取同一份缓存结果。

    :param http_client: 共享的启动器通道 HTTP 客户端
    :param event_bus: 应用事件总线，用于通知检测完成
    :param current_version: 当前启动器版本号
    :param version_type: 当前版本类型
    :param checker_factory: 可选的更新检测器工厂，可在测试中替换网络请求
    """

    def __init__(
        self,
        http_client: httpx.Client,
        event_bus: EventBus,
        *,
        current_version: str,
        version_type: str,
        checker_factory: Callable[[], UpdateChecker] | None = None,
    ) -> None:
        self.logger = get_logger("StartupUpdateService")
        self.events = event_bus
        self._current_version = current_version
        self._version_type = version_type
        self._checker_factory = checker_factory or (
            lambda: UpdateChecker(
                http_client,
                current_version=current_version,
                version_type=version_type,
            )
        )
        self._lock = RLock()
        self._stopped = Event()
        self._started = False
        self._result: UpdateCheckResult | None = None
        self._thread: Thread | None = None

    def start(self) -> bool:
        """
        启动一次后台检测。

        重复调用不会创建新的网络请求，保证前端重载或重复就绪事件不重复消耗
        GitHub API 配额。

        :return: 首次成功启动时为 True；已启动或已关闭时为 False
        """

        with self._lock:
            if self._started or self._stopped.is_set():
                return False
            self._started = True
            self._thread = Thread(target=self._run, name="ECL-UpdateCheck", daemon=True)
            self._thread.start()
            return True

    def result(self) -> dict[str, Any] | None:
        """
        返回最近一次启动检测的可序列化结果。

        :return: 检测尚未完成时返回 None；完成后返回独立字典，调用方可安全修改
        """

        with self._lock:
            return asdict(self._result) if self._result is not None else None

    def close(self) -> None:
        """
        标记服务已关闭，阻止尚未完成的任务继续向已销毁的前端发送事件。

        线程使用守护模式，并只进行极短的收尾等待，避免网络请求阻塞启动器关闭；共享
        应用关闭时会关闭共享的 HTTP 客户端。
        """

        self._stopped.set()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=0.1)

    def _run(self) -> None:
        """
        执行检测并在进程仍存活时发布结果。

        检测器通常会把网络失败转换为 ``error`` 结果；此处再次捕获异常，避免替换检测器时
        的意外异常让前端永远等不到完成状态。
        """

        try:
            result = self._checker_factory().check()
        except Exception as exc:
            self.logger.exception("启动更新检测发生未处理异常")
            result = UpdateCheckResult(
                status="error",
                current_version=self._current_version,
                channel="beta" if self._version_type in {"beta", "rc"} else self._version_type or "release",
                message=str(exc) or "更新检测失败",
            )
        with self._lock:
            self._result = result
        if not self._stopped.is_set():
            self.events.emit("update:check_completed", asdict(result))


class LocalizedAnnouncement(TypedDict):
    """
    保存一种语言中完整的公告标题与正文。
    """

    title: str
    content: str


class Announcement(TypedDict):
    """
    保存已校验且可供前端展示的公告及可选翻译。
    """

    id: str
    title: str
    date: str
    content: str
    locales: NotRequired[dict[str, LocalizedAnnouncement]]


class AnnouncementService:
    """
    读取远程公告并管理校验、刷新和离线缓存。

    公告在配置的刷新间隔内复用内存结果；远程拉取失败时回退到磁盘缓存，本地
    缓存同样损坏时才返回空公告列表，始终不阻断首页渲染。共享客户端由应用负责关闭。

    :param data_path: 启动器数据目录，用于持久化公告缓存
    :param http_client: 应用管理的共享 HTTP 客户端
    :param notice_loader: 可注入的公告读取边界，默认使用共享客户端
    :param clock: 可注入的当前时间来源，默认使用 UTC
    :param refresh_seconds: 内存缓存刷新间隔
    :param request_timeout: 公告请求总超时
    :param request_retries: 网络请求重试上限
    """

    notice_url: str = "https://api.euora.cn/raw/ECLTeam/EuoraCraft-Launcher.API/main/notice.json"
    notice_schema_version: int = 1
    notice_refresh_seconds: float = 300.0
    notice_timeout_seconds: float = 5.0

    def __init__(
        self,
        data_path: Path | str,
        *,
        http_client: httpx.Client,
        notice_loader: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] | None = None,
        refresh_seconds: float | None = None,
        request_timeout: float | None = None,
        request_retries: int = 2,
    ) -> None:
        self.logger = get_logger("AnnouncementService")
        self.data_path = Path(data_path)
        self.notice_cache_path = self.data_path / "notice.json"
        self.http = http_client
        self._notice_loader = notice_loader or self._download_notice
        self._clock = clock or (lambda: datetime.now(UTC))
        self._refresh_seconds = max(0.0, self.notice_refresh_seconds if refresh_seconds is None else refresh_seconds)
        self._request_timeout = max(
            1.0, float(self.notice_timeout_seconds if request_timeout is None else request_timeout)
        )
        self._request_retries = max(0, int(request_retries))
        self._lock = RLock()
        self._last_refresh_at: float | None = None
        self._announcements: list[Announcement] | None = None
        self.data_path.mkdir(parents=True, exist_ok=True)

    def _download_notice(self, url: str) -> object:
        """
        使用应用的共享客户端读取不可信公告 JSON。

        超时、重试和证书策略由请求参数及共享客户端管理；HTTP 或 JSON 错误由
        公共读取入口捕获并触发离线缓存回退。
        """
        # 公告请求使用启动器的网络设置，并限制连接阶段不超过总超时。
        timeout = httpx.Timeout(self._request_timeout, connect=min(3.0, self._request_timeout))
        headers = {
            "Accept": "application/json",
            "User-Agent": "EuoraCraft-Launcher",
        }
        response = get_with_retries(
            self.http.get,
            url,
            retries=self._request_retries,
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        """
        将远程公告时间转换为 UTC，无时区时间按 UTC 解释。

        空值表示不限制该侧时间边界；错误类型和非法 ISO 时间向上抛出，
        由条目解析跳过对应公告，避免错误数据改变展示时间范围。
        """
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("公告时间必须是 ISO 8601 字符串")
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _normalize_locales(value: object) -> dict[str, LocalizedAnnouncement]:
        """
        校验远程公告的多语言字段并剔除不完整翻译。

        返回给前端之前必须检查远程数据；每种语言都必须有非空标题和正文，
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
            if not isinstance(title, str) or not title.strip() or not isinstance(content, str) or not content.strip():
                continue
            locales[locale.strip()] = {"title": title.strip(), "content": content.strip()}
        return locales

    @classmethod
    def _create_announcement(cls, item: dict[str, object], notice_id: str, title: str, content: str) -> Announcement:
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
    def _normalize_announcements(cls, data: object, now: datetime) -> list[Announcement]:
        """
        校验不可信公告根结构并筛选当前有效的独立公告数据。

        根结构或版本错误触发缓存回退；单条公告错误仅剔除该条目，保留其他
        有效条目。输出保留翻译并按优先级稳定排序。
        """
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
            if (
                not isinstance(notice_id, str)
                or not notice_id.strip()
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(content, str)
                or not content.strip()
            ):
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

    def _write_notice_cache(self, data: object) -> None:
        atomic_write_text(self.notice_cache_path, json.dumps(data, ensure_ascii=False, indent=2))

    def _read_cached_announcements(self, now: datetime) -> list[Announcement]:
        data = json.loads(self.notice_cache_path.read_text(encoding="utf-8"))
        return self._normalize_announcements(data, now)

    def get_announcements(self) -> list[Announcement]:
        """
        返回经过有效期过滤的公告列表，并按刷新间隔复用独立缓存。

        读取与刷新在同一锁内串行执行；网络失败时读取磁盘缓存，缓存也不可用时
        返回空列表。返回值为深拷贝，调用方修改不会污染服务缓存。

        :return: 已校验的公告列表，不包含首页文案或展示配置
        """
        with self._lock:
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


__all__ = [
    "Announcement",
    "AnnouncementService",
    "LocalizedAnnouncement",
    "StartupUpdateService",
    "UpdateCheckResult",
    "UpdateChecker",
    "compare_versions",
    "parse_version",
]
