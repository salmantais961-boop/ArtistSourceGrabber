# -*- coding: utf-8 -*-
"""Danbooru(danbooru.donmai.us) 源。也作为 ATFbooru 之类 Danbooru 风格站点的基类。"""
import re
import urllib.parse
from difflib import SequenceMatcher

from .base import Source, Post
from http_util import http_request, describe_error


PAGE_LIMIT = 200


class DanbooruLikeSource(Source):
    """通用 Danbooru 风格:posts.json / artists.json / counts/posts.json / profile.json。"""

    api_base = "https://danbooru.donmai.us"
    supports_artist_search = True
    can_count = True

    # 子类可覆盖:rating 集合、是否支持匿名
    RATING_VALUES = ("", "g", "s", "q", "e")
    RATING_API = {"g": "general", "s": "sensitive", "q": "questionable", "e": "explicit"}

    # ---- 配置校验 ----
    def normalize_cfg(self, body):
        artist = str(body.get("artist") or "").strip()
        if not artist:
            return "请填写画师 tag 或 ID"
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            return "下载数量必须是整数"
        if count < 0:
            return "下载数量不能为负数"
        rating = str(body.get("rating") or "").strip()
        if rating not in self.RATING_VALUES:
            return "分级参数不合法"
        tag_format = body.get("tag_format")
        if tag_format not in ("comma", "space"):
            tag_format = "comma"
        proxy = str(body.get("proxy") or "").strip()
        return {
            "login": str(body.get("login") or "").strip(),
            "api_key": str(body.get("api_key") or "").strip(),
            "artist": artist,
            "count": count,
            "rating": rating,
            "tag_format": tag_format,
            "include_artist": bool(body.get("include_artist")),
            "include_meta": bool(body.get("include_meta")),
            "skip_video": bool(body.get("skip_video", True)),
            "proxy": proxy,
        }

    # ---- HTTP ----
    def _api(self, path, params, cfg):
        q = dict(params or {})
        if cfg.get("login") and cfg.get("api_key"):
            q["login"] = cfg["login"]
            q["api_key"] = cfg["api_key"]
        url = self.api_base + path
        if q:
            url += "?" + urllib.parse.urlencode(q)
        return http_request(url, cfg.get("proxy"))

    # ---- test ----
    def test(self, cfg):
        try:
            if cfg.get("login") and cfg.get("api_key"):
                prof = self._api("/profile.json", {}, cfg)
                return True, "认证成功:%s(等级:%s)" % (
                    prof.get("name"), prof.get("level_string", "?"))
            self._api("/posts.json", {"limit": 1}, cfg)
            return True, "连接正常(匿名模式)"
        except Exception as exc:
            return False, describe_error(exc)

    # ---- 画师解析 ----
    def resolve_artist(self, cfg, logger):
        raw = cfg["artist"].strip()
        if re.fullmatch(r"\d+", raw):
            try:
                info = self._api("/artists/%s.json" % raw, {}, cfg)
            except Exception as exc:
                raise RuntimeError("查询画师 ID 失败:%s" % describe_error(exc))
            name = info.get("name")
            if not name:
                raise RuntimeError("画师 ID %s 不存在" % raw)
            logger("画师 ID %s 对应 tag:%s" % (raw, name))
            return name
        return raw.replace(" ", "_").lower()

    # ---- 模糊搜索画师 ----
    def search_artists(self, query, cfg, limit=10):
        if not query:
            return []
        if cfg.get("query_type") != "artist":
            return self._search_tags(query, cfg, limit)
        return self._search_artist_entries(query, cfg, limit)

    def _search_artist_entries(self, query, cfg, limit):
        normalized_query = self._normalize_artist_name(query)
        variants = []
        for value in (query, query.replace(" ", "_"), query.replace("-", "_"),
                      re.sub(r"\s*\([^)]*\)\s*", "", query)):
            value = value.strip().lower()
            if value and value not in variants:
                variants.append(value)
        # 取较宽候选集，再在本地综合 name / other_names 排序，避免字段格式差异。
        results = []
        seen = set()
        patterns = []
        for value in variants:
            patterns.extend((value, "*%s*" % value))
        if re.match(r"https?://", query, re.I):
            patterns = [query]
        for pattern in patterns:
            try:
                search_key = "search[url_matches]" if re.match(r"https?://", pattern, re.I) \
                    else "search[any_name_matches]"
                data = self._api("/artists.json",
                                 {search_key: pattern,
                                  "only": "id,name,other_names,group_name,is_banned,is_deleted,urls",
                                  "limit": max(limit * 3, 20)}, cfg)
            except Exception:
                data = None
            if isinstance(data, list):
                for a in data:
                    aid = a.get("id")
                    if aid in seen:
                        continue
                    seen.add(aid)
                    name = a.get("name", "")
                    other_names = a.get("other_names", "")
                    aliases = other_names if isinstance(other_names, list) else str(other_names or "").split()
                    urls = a.get("urls") or []
                    url_values = [str(u.get("url") or "") if isinstance(u, dict) else str(u)
                                  for u in urls]
                    url_exact = re.match(r"https?://", query, re.I) and any(
                        u.rstrip("/").casefold() == query.rstrip("/").casefold()
                        for u in url_values)
                    name_score = self._artist_similarity(normalized_query, name)
                    alias_scores = [self._artist_similarity(normalized_query, alias) for alias in aliases]
                    alias_score = max(alias_scores or [0.0])
                    canonical_exact = normalized_query == self._normalize_artist_name(name)
                    alias_exact = any(normalized_query == self._normalize_artist_name(alias)
                                      for alias in aliases)
                    if url_exact:
                        match_priority, match_reason, score = 3, "url_exact", 1.0
                    elif canonical_exact:
                        match_priority, match_reason, score = 2, "name_exact", 1.0
                    elif alias_exact:
                        match_priority, match_reason, score = 1, "alias_exact", 0.95
                    else:
                        match_priority, match_reason = 0, "fuzzy"
                        score = max(name_score, alias_score * 0.9)
                    results.append({
                        "id": str(aid),
                        "name": name,
                        "site": self.id,
                        "profile_url": "%s/artists/%s" % (self.api_base, name),
                        "post_count": a.get("post_count"),
                        "other_names": other_names,
                        "is_banned": bool(a.get("is_banned")),
                        "is_deleted": bool(a.get("is_deleted")),
                        "urls": urls,
                        "score": round(score, 4),
                        "match_priority": match_priority,
                        "match_reason": match_reason,
                    })
            if len(results) >= max(limit * 3, 20):
                break
        results.sort(key=lambda item: (
            item.get("is_banned", False),
            -int(item.get("match_priority") or 0),
            -float(item.get("score") or 0),
            -int(item.get("post_count") or 0),
            item.get("name", ""),
        ))
        return results[:limit]

    @staticmethod
    def _normalize_artist_name(value):
        value = str(value or "").casefold().lstrip("@")
        value = re.sub(r"\([^)]*\)", "", value)
        return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", "", value)

    @classmethod
    def _artist_similarity(cls, query, candidate):
        candidate = cls._normalize_artist_name(candidate)
        if not query or not candidate:
            return 0.0
        if query == candidate:
            return 1.0
        containment = min(len(query), len(candidate)) / max(len(query), len(candidate)) \
            if query in candidate or candidate in query else 0.0
        return max(containment, SequenceMatcher(None, query, candidate).ratio())

    def _search_tags(self, query, cfg, limit):
        query = query.strip().replace(" ", "_")
        CATEGORY_LABELS = {"-1": "全部", "0": "通用", "1": "画师", "3": "版权", "4": "角色"}
        try:
            params = "search[name_matches]=*%s*&search[order]=count&limit=%d" % (
                urllib.parse.quote(query, safe="*"), max(limit * 3, 30))
            url = self.api_base + "/tags.json?" + params
            if cfg.get("login") and cfg.get("api_key"):
                url += "&login=%s&api_key=%s" % (
                    urllib.parse.quote(cfg["login"]), urllib.parse.quote(cfg["api_key"]))
            data = http_request(url, cfg.get("proxy"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        results = []
        for t in data:
            name = t.get("name", "")
            category = str(t.get("category") or "")
            results.append({
                "id": name,
                "name": name,
                "site": self.id,
                "profile_url": "%s/posts?tags=%s" % (self.api_base, urllib.parse.quote(name)),
                "post_count": t.get("post_count"),
                "other_names": "类型：%s" % CATEGORY_LABELS.get(category, category),
                "is_banned": bool(t.get("is_deprecated")),
            })
        results.sort(key=lambda x: (-int(x.get("post_count") or 0), x.get("name", "")))
        return results[:limit]

    # ---- 计数 ----
    def count_posts(self, artist_key, cfg):
        # 优先走 counts 接口
        search = artist_key
        if cfg.get("rating"):
            search += " rating:%s" % self.RATING_API.get(cfg["rating"], cfg["rating"])
        try:
            data = self._api("/counts/posts.json", {"tags": search}, cfg)
        except Exception:
            return -1
        return int(((data or {}).get("counts") or {}).get("posts") or 0)

    # ---- 列出作品 ----
    def _build_search(self, artist_key, cfg):
        search = artist_key
        if cfg.get("rating"):
            search += " rating:%s" % self.RATING_API.get(cfg["rating"], cfg["rating"])
        return search

    def list_posts(self, artist_key, page, cfg):
        search = self._build_search(artist_key, cfg)
        data = self._api("/posts.json",
                         {"tags": search, "limit": PAGE_LIMIT, "page": page}, cfg)
        posts = []
        if isinstance(data, list):
            for p in data:
                posts.append(self._normalize_post(p))
        return posts

    def _normalize_post(self, p):
        ext = (p.get("file_ext") or "").lower()
        is_video = ext in ("mp4", "webm", "zip")
        return Post(
            id=str(p.get("id")),
            ext=ext,
            file_url=p.get("file_url") or p.get("large_file_url") or p.get("preview_file_url"),
            large_url=p.get("large_file_url"),
            is_video=is_video,
            artist=p.get("tag_string_artist", ""),
            raw=p,
        )

    # ---- 用站点 tag 构造标注 ----
    def build_caption(self, post, cfg):
        p = post.get("raw", {})
        groups = []
        if cfg.get("include_artist"):
            groups.append(p.get("tag_string_artist", ""))
        groups.append(p.get("tag_string_character", ""))
        groups.append(p.get("tag_string_copyright", ""))
        groups.append(p.get("tag_string_general", ""))
        if cfg.get("include_meta"):
            groups.append(p.get("tag_string_meta", ""))
        tags = " ".join(g for g in groups if g).split()
        if cfg["tag_format"] == "comma":
            pretty = [t.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
                      for t in tags]
            return ", ".join(pretty)
        return " ".join(tags)


class DanbooruSource(DanbooruLikeSource):
    id = "danbooru"
    label = "Danbooru"
    needs_auth = False


class ATFBooruSource(DanbooruLikeSource):
    """注:此源默认不启用——其内容在很多司法辖区受法律限制。"""
    id = "atfbooru"
    label = "ATFbooru(默认禁用)"
    api_base = "https://booru.allthefallen.moe"
    needs_auth = False


# ATFbooru 默认不在 registry 中暴露,见 sources/__init__.py
