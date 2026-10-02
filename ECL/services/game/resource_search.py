# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：维护双平台搜索会话，负责保守身份合并、排名融合及稳定分页，不执行下载。
#
# 公开接口：
#   - SearchCriteria — 不可变的会话筛选条件。
#   - SearchItem、SearchBatch、SearchResult — 校验后的项目、平台批次和聚合响应。
#   - ResourceSearchService — 拥有有界请求执行器及搜索会话的聚合服务。
# ============================================================

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import BoundedSemaphore, Lock, RLock
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ECL.utils import GameServiceError

SearchSource = Literal["modrinth", "curseforge"]


class _SearchModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, coerce_numbers_to_str=True)


class SearchReference(_SearchModel):
    """
    保存实际平台的项目标识，供详情和下载选择来源。
    """

    source: SearchSource
    project_id: str
    slug: str = ""
    project_url: str = ""


class SearchWiki(_SearchModel):
    """
    保存已经由本地百科映射确认的项目身份和译名。
    """

    id: str
    title: str = ""
    english_name: str = ""
    summary: str = ""
    url: str = ""


class SearchItem(_SearchModel):
    """
    将平台边界结果转换为字段明确的搜索卡片。
    """

    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    source: SearchSource
    slug: str = ""
    title: str = ""
    display_title: str = ""
    description: str = ""
    author: str = ""
    icon_url: str | None = None
    downloads: int = 0
    follows: int = 0
    date_modified: str | None = None
    date_created: str | None = None
    project_url: str = ""
    categories: list[str] = Field(default_factory=list)
    loaders: list[str] = Field(default_factory=list)
    game_versions: list[str] = Field(default_factory=list)
    resource_type: str
    wiki: SearchWiki | None = None
    alternatives: list[SearchReference] = Field(default_factory=list)
    group_id: str = ""


class SearchBatch(BaseModel):
    """
    保存一个平台批次的原始数量与已校验候选，偏移量不受去重影响。
    """

    items: list[SearchItem]
    raw_count: int = Field(ge=0, le=20)
    total: int | None = Field(default=None, ge=0)


class SearchSourceStatus(_SearchModel):
    """
    区分来源配置、失败和耗尽状态，不携带平台密钥。
    """

    available: bool = True
    error: str = ""
    error_code: str = ""
    total: int = 0
    exhausted: bool = False


class SearchResult(_SearchModel):
    """
    返回稳定页面及渐进数量，只有完整耗尽时 total 才是最终总量。
    """

    items: list[SearchItem]
    sources: dict[SearchSource, SearchSourceStatus]
    total: int
    query: str
    session_id: str
    page: int
    page_count: int
    has_more: bool
    total_exact: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """
    确定搜索会话的资源范围、版本、加载器和排序。
    """

    query: str = ""
    game_version: str = ""
    loader: str = ""
    resource_type: str = "mod"
    sort: str = ""


@dataclass(slots=True)
class _SourceState:
    offset: int = 0
    status: SearchSourceStatus = field(default_factory=SearchSourceStatus)


@dataclass(slots=True)
class _Group:
    item: SearchItem
    ranks: dict[SearchSource, int]
    members: dict[SearchSource, SearchItem]


@dataclass(slots=True)
class _Session:
    criteria: SearchCriteria
    id: str = field(default_factory=lambda: uuid4().hex)
    used_at: float = field(default_factory=monotonic)
    users: int = 0
    lock: Lock = field(default_factory=Lock)
    sources: dict[SearchSource, _SourceState] = field(
        default_factory=lambda: {"modrinth": _SourceState(), "curseforge": _SourceState()}
    )
    groups: dict[str, _Group] = field(default_factory=dict)
    source_ids: dict[tuple[SearchSource, str], str] = field(default_factory=dict)
    wiki_ids: dict[str, set[str]] = field(default_factory=dict)
    signatures: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    pages: list[list[str]] = field(default_factory=list)
    last_source: SearchSource = "curseforge"
    raw_count: int = 0
    truncated: bool = False


def _normalized(value: str, *, slug: bool = False) -> str:
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return re.sub(r"[ _-]+", "-", value) if slug else value


def _signature(item: SearchItem) -> tuple[str, str, str] | None:
    values = (_normalized(item.slug, slug=True), _normalized(item.title), _normalized(item.author))
    return values if all(values) else None


def _timestamp(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    except (ValueError, OverflowError, OSError):
        return float("-inf")


class ResourceSearchService:
    """
    拥有双平台搜索会话和有界执行器，串行扩充同一会话。

    网络回调运行在工作线程；调用方必须提供有限超时的回调，并在应用退出时关闭服务。
    """

    page_size: int = 20
    max_rounds: int = 3
    max_sessions: int = 16
    max_groups: int = 2000
    max_raw_items: int = 8000
    idle_ttl: float = 600
    sources: tuple[SearchSource, ...] = ("modrinth", "curseforge")

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        """
        创建独立会话缓存及最多四个在途平台请求的执行器。

        :param clock: 用于空闲过期判断的单调时钟
        """
        self._clock = clock
        self._lock = RLock()
        self._sessions: dict[str, _Session] = {}
        self._session_by_criteria: dict[SearchCriteria, str] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ECL-ResourceSearch")
        self._slots = BoundedSemaphore(4)
        self._is_closed = False
        self._logger = logging.getLogger(__name__)

    def close(self) -> None:
        """
        禁止新请求并清空会话，等待已有有限超时请求释放线程。
        """
        with self._lock:
            self._is_closed = True
            self._sessions.clear()
            self._session_by_criteria.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def search(
        self,
        criteria: SearchCriteria,
        fetch: Callable[[SearchSource, SearchCriteria, int], SearchBatch],
        *,
        curseforge_available: bool,
        session_id: str = "",
        page: int = 1,
        refresh: bool = False,
    ) -> SearchResult:
        """
        获取既有页面或按独立平台偏移量扩充下一页。

        已展示顺序固定，后续命中只更新来源入口；不允许跳过尚未建立的页面。

        :param criteria: 会话筛选条件
        :param fetch: 返回校验后平台批次的有限超时网络回调
        :param curseforge_available: CurseForge 密钥是否已配置
        :param session_id: 已建立的会话标识，空值复用同条件第一页
        :param page: 从一开始的目标页码
        :param refresh: 为第一页创建新会话，旧会话仍可供其他消费者读取
        :return: 当前稳定页面及来源状态
        :raises GameServiceError: 会话失效、条件不匹配、跳页、忙碌或全部平台失败
        """
        if criteria.resource_type not in {"mod", "resourcepack", "shaderpack", "datapack", "modpack"}:
            raise GameServiceError("此资源类型不支持双平台搜索", "INVALID_RESOURCE_TYPE")
        if page < 1 or (refresh and page != 1):
            raise GameServiceError("搜索页码无效", "INVALID_SEARCH_PAGE")
        session = self._acquire_session(criteria, session_id, refresh, curseforge_available)
        try:
            if not session.lock.acquire(timeout=35):
                raise GameServiceError("搜索繁忙，请稍后重试", "SEARCH_BUSY")
            try:
                if page > len(session.pages) + 1:
                    raise GameServiceError("请按顺序加载搜索页面", "INVALID_SEARCH_PAGE")
                if page > len(session.pages):
                    self._extend(session, fetch)
                    chosen = self._select(session)
                    if chosen:
                        session.pages.append(chosen)
                    # 补齐预算内没有新项目时停在原页，允许继续读取。
                    page = min(page, max(1, len(session.pages)))
                return self._result(session, page)
            finally:
                session.lock.release()
        finally:
            with self._lock:
                session.users -= 1
                session.used_at = self._clock()

    def _acquire_session(
        self, criteria: SearchCriteria, session_id: str, refresh: bool, curseforge_available: bool
    ) -> _Session:
        """
        在全局短锁内淘汰空闲会话并固定本次使用引用，网络期间不持有该锁。
        """
        with self._lock:
            if self._is_closed:
                raise GameServiceError("搜索服务已关闭", "SEARCH_CLOSED")
            now = self._clock()
            for stale in list(self._sessions.values()):
                if not stale.users and now - stale.used_at >= self.idle_ttl:
                    self._remove(stale)
            selected_id = session_id if session_id and not refresh else self._session_by_criteria.get(criteria, "")
            session = self._sessions.get(selected_id) if not refresh else None
            if session_id and not refresh and session is None:
                raise GameServiceError("搜索会话已过期，请重新搜索", "SEARCH_SESSION_EXPIRED")
            if session is not None and session.criteria != criteria:
                raise GameServiceError("筛选条件与搜索会话不匹配", "SEARCH_SESSION_MISMATCH")
            if session is None:
                if len(self._sessions) >= self.max_sessions:
                    candidates = [value for value in self._sessions.values() if not value.users]
                    if not candidates:
                        raise GameServiceError("搜索繁忙，请稍后重试", "SEARCH_BUSY")
                    self._remove(min(candidates, key=lambda value: value.used_at))
                session = _Session(criteria, used_at=now)
                if not curseforge_available:
                    session.sources["curseforge"].status = SearchSourceStatus(
                        available=False, error="CurseForge 尚未配置", error_code="CURSEFORGE_KEY_REQUIRED"
                    )
                self._sessions[session.id] = session
                self._session_by_criteria[criteria] = session.id
            session.users += 1
            return session

    def _remove(self, session: _Session) -> None:
        self._sessions.pop(session.id, None)
        if self._session_by_criteria.get(session.criteria) == session.id:
            self._session_by_criteria.pop(session.criteria, None)

    def _submit(
        self, fetch: Callable[[SearchSource, SearchCriteria, int], SearchBatch], session: _Session, source: SearchSource
    ) -> Future[SearchBatch]:
        """
        先取得有限请求名额再提交，避免执行器队列无限积累。
        """
        if not self._slots.acquire(timeout=5):
            raise GameServiceError("平台搜索繁忙，请重试", "SEARCH_BUSY")
        try:
            with self._lock:
                if self._is_closed:
                    raise GameServiceError("搜索服务已关闭", "SEARCH_CLOSED")
                future = self._executor.submit(fetch, source, session.criteria, session.sources[source].offset)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return future

    def _extend(self, session: _Session, fetch: Callable[[SearchSource, SearchCriteria, int], SearchBatch]) -> None:
        """
        有界补齐候选，失败不推进偏移量，固定消费顺序消除响应先后差异。
        """
        failed: set[SearchSource] = set()
        for _ in range(self.max_rounds):
            if len(session.pending) >= self.page_size or session.truncated:
                break
            futures: dict[SearchSource, Future[SearchBatch]] = {}
            for source, state in session.sources.items():
                if state.status.available and not state.status.exhausted and source not in failed:
                    try:
                        futures[source] = self._submit(fetch, session, source)
                    except GameServiceError as exc:
                        self._failure(state, exc)
                        failed.add(source)
            if not futures:
                if failed and not session.groups:
                    raise GameServiceError("在线资源搜索失败，请重试", "MOD_SEARCH_FAILED")
                break
            succeeded = False
            for source, future in futures.items():
                state = session.sources[source]
                try:
                    batch = future.result()
                except Exception as exc:
                    self._failure(state, exc)
                    failed.add(source)
                    continue
                succeeded = True
                self._consume_batch(session, source, batch)
            if not succeeded:
                if not session.groups:
                    raise GameServiceError("在线资源搜索失败，请重试", "MOD_SEARCH_FAILED")
                break

    def _failure(self, state: _SourceState, error: Exception) -> None:
        code = error.error_code if isinstance(error, GameServiceError) else "RESOURCE_SOURCE_UNAVAILABLE"
        state.status.error_code = code
        state.status.error = "平台搜索失败，请重试"
        self._logger.warning("在线资源来源请求失败: %s", code)

    def _consume_batch(self, session: _Session, source: SearchSource, batch: SearchBatch) -> None:
        state = session.sources[source]
        state.status.error = ""
        state.status.error_code = ""
        start = state.offset
        state.offset += batch.raw_count
        session.raw_count += batch.raw_count
        if batch.total is not None:
            state.status.total = max(0, batch.total)
        state.status.exhausted = batch.raw_count == 0 or (
            batch.total is not None and batch.total > 0 and state.offset >= batch.total
        )
        for index, item in enumerate(batch.items):
            self._merge(session, item, start + index + 1)
        if len(session.groups) >= self.max_groups or session.raw_count >= self.max_raw_items:
            session.truncated = True

    def _match_group(self, session: _Session, item: SearchItem) -> str | None:
        """
        查找无歧义的跨平台项目，百科冲突和同平台不同 ID 均拒绝合并。
        """
        wiki_id = item.wiki.id if item.wiki else ""
        signature = _signature(item)
        candidates = session.wiki_ids.get(wiki_id, set()) if wiki_id else set()
        if not candidates and signature:
            candidates = session.signatures.get(signature, set())
        compatible: list[str] = []
        for candidate in candidates:
            group = session.groups[candidate]
            if item.source in group.members or group.item.resource_type != item.resource_type:
                continue
            existing_wiki = group.item.wiki.id if group.item.wiki else ""
            if wiki_id and existing_wiki and wiki_id != existing_wiki:
                continue
            compatible.append(candidate)
        return compatible[0] if len(compatible) == 1 else None

    def _merge(self, session: _Session, item: SearchItem, rank: int) -> None:
        """
        逐项更新索引，只合并无歧义的跨平台身份，拒绝同平台项目和百科冲突。
        """
        source_key = (item.source, item.project_id)
        group_id = session.source_ids.get(source_key) or self._match_group(session, item)
        wiki_id = item.wiki.id if item.wiki and item.wiki.id else ""
        signature = _signature(item)
        if group_id is None:
            if len(session.groups) >= self.max_groups:
                return
            group_id = f"{item.resource_type}:{item.source}:{item.project_id}"
            primary = item.model_copy(deep=True, update={"group_id": group_id})
            session.groups[group_id] = _Group(primary, {}, {})
            session.pending.add(group_id)
        group = session.groups[group_id]
        if item.source not in group.members:
            group.members[item.source] = item.model_copy(deep=True)
            group.ranks[item.source] = rank
            group.item.alternatives = [
                SearchReference(
                    source=member.source, project_id=member.project_id, slug=member.slug, project_url=member.project_url
                )
                for member in group.members.values()
            ]
            for name in ("icon_url", "description", "author", "wiki", "date_created", "date_modified"):
                if not getattr(group.item, name) and getattr(item, name):
                    setattr(group.item, name, getattr(item, name))
        session.source_ids[source_key] = group_id
        if wiki_id:
            session.wiki_ids.setdefault(wiki_id, set()).add(group_id)
        if signature:
            session.signatures.setdefault(signature, set()).add(group_id)

    @staticmethod
    def _score(group: _Group, criteria: SearchCriteria) -> float:
        """
        融合平台内排名与名称命中，不使用跨平台下载量倍率。
        """
        if criteria.sort in {"newest", "updated"}:
            name = "date_created" if criteria.sort == "newest" else "date_modified"
            return max(_timestamp(getattr(member, name)) for member in group.members.values())
        ranks = sorted((60 / (60 + rank) for rank in group.ranks.values()), reverse=True)
        score = ranks[0] + (0.15 * ranks[1] if len(ranks) > 1 else 0)
        query = _normalized(criteria.query)
        if not query or criteria.sort not in {"", "relevance"}:
            return score
        match = 0.0
        for member in group.members.values():
            names = (member.title, member.slug, member.wiki.title if member.wiki else "")
            for name in names:
                normalized = _normalized(name)
                if normalized == query:
                    match = max(match, 1.0)
                elif normalized.startswith(query):
                    match = max(match, 0.7)
                elif all(token in normalized for token in query.split()):
                    match = max(match, 0.4)
        return score + 0.6 * match

    def _select(self, session: _Session) -> list[str]:
        """
        对未展示候选排序，同分时轮换来源，已展示页面不参与重新排序。
        """
        scored: dict[float, list[str]] = {}
        for group_id in session.pending:
            scored.setdefault(self._score(session.groups[group_id], session.criteria), []).append(group_id)
        chosen: list[str] = []
        for score in sorted(scored, reverse=True):
            candidates = sorted(scored[score], key=lambda key: session.groups[key].item.project_id)
            while candidates and len(chosen) < self.page_size:
                preferred = "modrinth" if session.last_source == "curseforge" else "curseforge"
                key = next((key for key in candidates if session.groups[key].item.source == preferred), candidates[0])
                candidates.remove(key)
                chosen.append(key)
                session.last_source = session.groups[key].item.source
            if len(chosen) == self.page_size:
                break
        session.pending.difference_update(chosen)
        return chosen

    def _result(self, session: _Session, page: int) -> SearchResult:
        keys = session.pages[page - 1] if session.pages else []
        remaining = any(state.status.available and not state.status.exhausted for state in session.sources.values())
        return SearchResult(
            items=[session.groups[key].item.model_copy(deep=True) for key in keys],
            sources={source: state.status.model_copy(deep=True) for source, state in session.sources.items()},
            total=len(session.groups),
            query=session.criteria.query,
            session_id=session.id,
            page=page,
            page_count=len(session.pages) + (len(session.pending) + self.page_size - 1) // self.page_size,
            has_more=page < len(session.pages) or bool(session.pending) or (remaining and not session.truncated),
            total_exact=not remaining and not session.truncated,
            truncated=session.truncated,
        )
