# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：mcmod.cn 中文译名查询：本地索引、别名与中文搜索。
#
# 公开接口：
#   - class McmodTranslator — MC百科译名离线查询器。
#       - lookup_by_modrinth_slug(slug) -> dict[str, Any] | None — 按 Modrinth slug 精确查找译名条目。
#       - lookup_by_curseforge_slug(slug) -> dict[str, Any] | None — 按 CurseForge slug 精确查找译名条目。
#       - lookup_by_alias(*aliases) -> dict[str, Any] | None — 按英文名、平台 slug 或模组 ID 查询译名。
#       - search_chinese(query, limit=…) -> list[dict[str, Any]] — 按中文名匹配搜索译名条目，精确/前缀命中优先，其次按名称长度升序。
#       - to_english_query(query) -> str — 将中文关键词转换为英文搜索词，供 Modrinth/CurseForge 搜索使用。
#       - mcmod_url(mcmod_id) -> str — 生成 MC百科模组详情页 URL。
#       - to_wiki_info(mod) -> dict[str, str] — 将译名条目转换为前端 ``McmodInfo`` 结构。
# ============================================================

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


class McmodTranslator:
    """
    MC百科译名离线查询器。

    懒加载 ``resources/mcmod_data.json``（由 ``scripts/build_mcmod_data.py`` 合并
    三源生成），构建 Modrinth/CurseForge slug 索引，提供英文名转中文名、中文关键词
    转英文搜索词与百科页 URL 生成能力。数据文件缺失时所有查询返回空结果。
    """

    mcmod_base_url = "https://www.mcmod.cn/class/{}.html"
    stopwords = {"the", "of", "for", "and", "with", "mod", "mods", "forge", "fabric", "quilt", "neoforge"}

    def __init__(self, data_path: Path | None = None) -> None:
        """
        创建译名查询器，数据文件在首次查询时才加载。

        :param data_path: mcmod_data.json 的路径；为 None 或文件不存在时查询返回空
        """
        self._data_path = data_path
        self._mods: list[dict[str, Any]] = []
        self._by_mr: dict[str, dict[str, Any]] = {}
        self._by_cf: dict[str, dict[str, Any]] = {}
        self._by_alias: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        # 首次调用时加载数据文件并构建索引，加载失败时保持空索引。
        if self._loaded:
            return
        self._loaded = True
        if self._data_path is None or not self._data_path.is_file():
            return
        try:
            data = json.loads(self._data_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for mod in data.get("mods", []):
            if not isinstance(mod, dict):
                continue
            self._mods.append(mod)
            mr = str(mod.get("mr") or "").casefold()
            cf = str(mod.get("cf") or "").casefold()
            if mr:
                self._by_mr.setdefault(mr, mod)
            if cf:
                self._by_cf.setdefault(cf, mod)
            aliases = [mod.get("english"), mod.get("mr"), mod.get("cf"), *(mod.get("modIds") or [])]
            for alias in aliases:
                key = self._normalize_alias(alias)
                if key:
                    self._by_alias.setdefault(key, mod)

    @staticmethod
    def _normalize_alias(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    def lookup_by_modrinth_slug(self, slug: str) -> dict[str, Any] | None:
        """
        按 Modrinth slug 精确查找译名条目。

        :param slug: Modrinth 项目 slug
        :return: 译名条目字典；未命中返回 None
        """
        self._ensure_loaded()
        return self._by_mr.get(str(slug or "").casefold())

    def lookup_by_curseforge_slug(self, slug: str) -> dict[str, Any] | None:
        """
        按 CurseForge slug 精确查找译名条目。

        :param slug: CurseForge 项目 slug
        :return: 译名条目字典；未命中返回 None
        """
        self._ensure_loaded()
        return self._by_cf.get(str(slug or "").casefold())

    def lookup_by_alias(self, *aliases: str) -> dict[str, Any] | None:
        """
        按英文名、平台 slug 或模组 ID 查询译名。
        """
        self._ensure_loaded()
        for alias in aliases:
            key = self._normalize_alias(alias)
            if key and key in self._by_alias:
                return self._by_alias[key]
        return None

    def search_chinese(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """
        按中文名匹配搜索译名条目，精确/前缀命中优先，其次按名称长度升序。

        :param query: 中文搜索关键词
        :param limit: 返回的最大条目数
        :return: 匹配的译名条目列表
        """
        self._ensure_loaded()
        keyword = str(query or "").strip()
        if not keyword:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for mod in self._mods:
            name = str(mod.get("name") or "")
            if not name:
                continue
            if name == keyword:
                score = 3
            elif name.startswith(keyword) or keyword.startswith(name):
                score = 2
            elif keyword in name:
                score = 1
            else:
                continue
            scored.append((score, mod))
        scored.sort(key=lambda item: (-item[0], len(str(item[1].get("name") or ""))))
        return [mod for _, mod in scored[:limit]]

    @staticmethod
    def _entry_english(mod: dict[str, Any]) -> str:
        # 提取单条译名条目的英文搜索词，优先英文名，其次 Modrinth/CurseForge slug。
        for source in (str(mod.get("english") or ""), str(mod.get("mr") or ""), str(mod.get("cf") or "")):
            words = [
                word
                for word in re.split(r"[\s\-_]+", source)
                if word and word.casefold() not in McmodTranslator.stopwords and not word.isdigit()
            ]
            if words:
                return " ".join(words)
        return ""

    def to_english_query(self, query: str) -> str:
        """
        将中文关键词转换为英文搜索词，供 Modrinth/CurseForge 搜索使用。

        首个匹配为精确/前缀命中时直接使用其英文名；否则按词频取前三个关键词。

        :param query: 含中文的搜索关键词
        :return: 英文搜索词；无匹配时返回空字符串
        """
        matches = self.search_chinese(query)
        if not matches:
            return ""
        top_name = str(matches[0].get("name") or "")
        if top_name == query or top_name.startswith(query) or query.startswith(top_name):
            return self._entry_english(matches[0])
        words: list[str] = []
        for mod in matches:
            for source in (str(mod.get("english") or ""), str(mod.get("mr") or ""), str(mod.get("cf") or "")):
                for word in re.split(r"[\s\-_]+", source):
                    word = word.strip().casefold()
                    if word and word not in self.stopwords and not word.isdigit():
                        words.append(word)
        top = [word for word, _ in Counter(words).most_common(3)]
        return " ".join(top)

    @staticmethod
    def mcmod_url(mcmod_id: int) -> str:
        """
        生成 MC百科模组详情页 URL。

        :param mcmod_id: MC百科 class id
        :return: 百科详情页地址
        """
        return McmodTranslator.mcmod_base_url.format(int(mcmod_id))

    def to_wiki_info(self, mod: dict[str, Any]) -> dict[str, str]:
        """
        将译名条目转换为前端 ``McmodInfo`` 结构。

        :param mod: 译名条目字典
        :return: 前端 wiki 字段字典
        """
        return {
            "id": str(mod.get("id") or ""),
            "title": str(mod.get("name") or ""),
            "englishName": str(mod.get("english") or ""),
            "summary": "",
            "url": self.mcmod_url(int(mod.get("id") or 0)),
        }


__all__ = ["McmodTranslator"]
