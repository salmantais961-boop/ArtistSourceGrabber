# -*- coding: utf-8 -*-
"""Moebooru API 源:konachan.com / yande.re。

Moebooru:
  GET /post.json?tags=...&limit=...&page=...
  GET /artist.json?name=...
返回 JSON 数组。post 字段:id, file_url, sample_jpeg_file_url, preview_url,
  tags(空格分隔), rating, author, source。
"""
import urllib.parse

from .base import Source, Post, normalize_search_tags
from http_util import http_request, describe_error


PAGE_LIMIT = 100


class MoebooruSource(Source):
    api_base = "https://konachan.com"
    needs_auth = False
    supports_artist_search = True
    can_count = True
    RATING_VALUES = ("", "s", "q", "e")

    def normalize_cfg(self, body):
        artist = str(body.get("artist") or "").strip()
        if not artist:
            return "请填写画师用户名 / tag"
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
        return {
            "login": str(body.get("login") or "").strip(),
            "password_hash": str(body.get("password_hash") or "").strip(),
            "artist": artist,
            "count": count,
            "rating": rating,
            "tag_format": tag_format,
            "include_artist": bool(body.get("include_artist")),
            "include_meta": bool(body.get("include_meta")),
            "skip_video": bool(body.get("skip_video", True)),
            "proxy": str(body.get("proxy") or "").strip(),
        }

    def _api(self, path, params, cfg):
        url = self.api_base + path + "?" + urllib.parse.urlencode(params or {})
        data = http_request(url, cfg.get("proxy"))
        if isinstance(data, dict):
            success = data.get("success")
            failed = success is False or str(success).lower() == "false"
            error = data.get("error")
            if failed or error:
                detail = data.get("reason") or data.get("message") or error or "unknown error"
                raise RuntimeError("%s API 返回错误：%s" % (self.label, detail))
        return data

    def test(self, cfg):
        try:
            self._api("/post.json", {"limit": 1}, cfg)
            return True, "连接正常"
        except Exception as exc:
            return False, describe_error(exc)

    def resolve_artist(self, cfg, logger):
        raw = cfg["artist"].strip()
        if cfg.get("query_type", "artist") != "artist":
            query = normalize_search_tags(raw)
            if not query:
                raise RuntimeError("请至少填写一个有效标签")
            return query
        return raw.replace(" ", "_")

    def search_artists(self, query, cfg, limit=10):
        if not query:
            return []
        query = query.strip()
        patterns = []
        for pattern in (query, "*" + query + "*", query.replace(" ", "_")):
            if pattern not in patterns:
                patterns.append(pattern)
        last_error = None
        had_valid_response = False
        for name_pattern in patterns:
            try:
                data = self._api("/artist.json", {"name": name_pattern}, cfg)
                if not isinstance(data, list):
                    raise RuntimeError("%s API 返回了无法识别的画师列表" % self.label)
                if any(not isinstance(item, dict) for item in data):
                    raise RuntimeError("%s API 返回了损坏的画师数据" % self.label)
                had_valid_response = True
            except Exception as exc:
                last_error = exc
                continue
            out = []
            for a in data:
                out.append({
                    "id": str(a.get("id")),
                    "name": a.get("name", ""),
                    "site": self.id,
                    "profile_url": "%s/post/show?tags=%s" % (
                        self.api_base, urllib.parse.quote(a.get("name", ""))),
                    "post_count": None,
                    "other_names": a.get("aliases") or "",
                    "is_banned": False,
                })
            if out:
                return out[:limit]
        # 兜底:用 tag 搜索。Moebooru 无 tag search artist 接口,这里返回空
        if not had_valid_response and last_error is not None:
            raise last_error
        return []

    def count_posts(self, artist_key, cfg):
        search = "%s" % artist_key
        if cfg.get("rating"):
            search += " rating:%s" % cfg["rating"]
        try:
            data = self._api("/post.json", {"tags": search, "limit": 1, "page": 1}, cfg)
        except Exception:
            return -1
        # Moebooru 没有显式 count 字段——单 limit 1 仅判断存在性
        return -1

    def _build_search(self, artist_key, cfg):
        s = artist_key
        if cfg.get("rating"):
            s += " rating:%s" % cfg["rating"]
        return s

    def list_posts(self, artist_key, page, cfg):
        s = self._build_search(artist_key, cfg)
        data = self._api("/post.json", {"tags": s, "limit": PAGE_LIMIT, "page": page}, cfg)
        if not isinstance(data, list):
            raise RuntimeError("%s API 返回了无法识别的作品列表" % self.label)
        if any(not isinstance(item, dict) for item in data):
            raise RuntimeError("%s API 返回了损坏的作品数据" % self.label)
        out = []
        for p in data:
            out.append(self._normalize_post(p, artist_key))
        return out

    def _normalize_post(self, p, artist_key=""):
        url = p.get("file_url") or p.get("sample_jpeg_file_url") or p.get("preview_url")
        ext = ""
        if url:
            ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
        is_video = ext in ("mp4", "webm", "zip", "gif")
        return Post(
            id=str(p.get("id")),
            ext=ext,
            file_url=url,
            large_url=p.get("sample_jpeg_file_url") or p.get("sample_url"),
            is_video=is_video,
            # Moebooru's author is the uploader, not an artist tag.
            artist=str(artist_key or ""),
            raw=p,
        )

    def build_caption(self, post, cfg):
        p = post.get("raw", {})
        tags = (p.get("tags") or "").split()
        if cfg.get("include_artist"):
            artist = (post.get("artist") or "").replace(" ", "_")
            if artist and artist not in tags:
                tags = [artist] + tags
        if cfg["tag_format"] == "comma":
            pretty = [t.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
                      for t in tags]
            return ", ".join(pretty)
        return " ".join(tags)


class KonachanSource(MoebooruSource):
    id = "konachan"
    label = "Konachan"
    api_base = "https://konachan.com"
    can_count = False


class YandereSource(MoebooruSource):
    id = "yandere"
    label = "yande.re"
    api_base = "https://yande.re"
    can_count = False
