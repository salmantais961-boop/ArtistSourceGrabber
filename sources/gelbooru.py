# -*- coding: utf-8 -*-
"""Gelbooru API 兼容源:Gelbooru / Safebooru。

Gelbooru dapi:
  GET /index.php?page=dapi&s=post&q=index&json=1&tags=...&limit=...&pid=...
返回:{"@attributes":...,"post":[...]} (单条返回为 {"post": <obj>})

Gelbooru artist 接口不公开 JSON 端点传统搜索,改用 dapi 的 artist 节点:
  /index.php?page=dapi&s=artist&q=index&json=1&name=...
返回 {"artist":[...]}. 注意 Safebooru 同源接口。
"""
import urllib.parse
import xml.etree.ElementTree as ET

from .base import Source, Post
from http_util import http_request, describe_error


PAGE_LIMIT = 100


class GelbooruLikeSource(Source):
    api_base = "https://gelbooru.com"
    needs_auth = False
    supports_artist_search = True
    can_count = True
    # Gelbooru 有 #ext 也支持 #rating 等
    RATING_VALUES = ("", "general", "sensitive", "questionable", "explicit")

    def normalize_cfg(self, body):
        artist = str(body.get("artist") or "").strip()
        if not artist:
            return "请填写画师 tag / 用户名 / ID"
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
            "user_id": str(body.get("user_id") or "").strip(),
            "api_key": str(body.get("api_key") or "").strip(),
            "artist": artist,
            "count": count,
            "rating": rating,
            "tag_format": tag_format,
            "include_artist": bool(body.get("include_artist")),
            "include_meta": bool(body.get("include_meta")),
            "skip_video": bool(body.get("skip_video", True)),
            "proxy": str(body.get("proxy") or "").strip(),
        }

    def _api(self, s, q, pid, tags, cfg, limit=None, extra_params=None):
        params = {
            "page": "dapi", "s": s, "q": "index", "json": 1,
        }
        if q is not None:
            params["q"] = q
        if tags:
            params["tags"] = tags
        if pid is not None:
            params["pid"] = pid
        if limit is not None:
            params["limit"] = int(limit)
        if extra_params:
            params.update(extra_params)
        if cfg.get("user_id") and cfg.get("api_key"):
            params["user_id"] = cfg["user_id"]
            params["api_key"] = cfg["api_key"]
        url = self.api_base + "/index.php?" + urllib.parse.urlencode(params)
        data = http_request(url, cfg.get("proxy"))
        self._raise_api_error(data)
        return data

    def _raise_api_error(self, data):
        """Turn an explicit DAPI error object into a real task error."""
        if not isinstance(data, dict):
            return
        success = data.get("success")
        failed = success is False or str(success).lower() == "false"
        error = data.get("error")
        if failed or error:
            detail = data.get("reason") or data.get("message") or error or "unknown error"
            raise RuntimeError("%s API 返回错误：%s" % (self.label, detail))

    def _post_items(self, data):
        """Accept known empty/list response variants, reject malformed API pages."""
        if isinstance(data, list):
            posts = data
        elif isinstance(data, dict):
            if "post" not in data:
                if not data or "@attributes" in data:
                    return []
                raise RuntimeError("%s API 返回了无法识别的作品列表" % self.label)
            posts = data.get("post")
            if posts is None:
                return []
            if isinstance(posts, dict):
                posts = [posts]
            elif not isinstance(posts, list):
                raise RuntimeError("%s API 返回了无法识别的作品列表" % self.label)
        else:
            raise RuntimeError("%s API 返回了无法识别的作品列表" % self.label)
        if any(not isinstance(item, dict) for item in posts):
            raise RuntimeError("%s API 返回了损坏的作品数据" % self.label)
        return posts

    def test(self, cfg):
        try:
            data = self._api("post", "index", 0, None, cfg, limit=1)
            # data 是 {"@attributes":{...}, "post":[...] } 或包含属性
            if isinstance(data, (dict, list)):
                return True, "连接正常"
            return False, "Gelbooru 返回异常"
        except Exception as exc:
            return False, describe_error(exc)

    def resolve_artist(self, cfg, logger):
        raw = cfg["artist"].strip()
        # Gelbooru 画师通常以 "<artist_name>" 形式存在于 tag 中,等价于 "artist:<name>"
        # 用户输入名称即可,无需数字 ID
        return raw.replace(" ", "_")

    @staticmethod
    def _build_search(artist_key, cfg):
        if cfg.get("query_type") == "artist":
            search = "artist:%s" % artist_key
        else:
            search = artist_key
        if cfg.get("rating"):
            search += " rating:%s" % cfg["rating"]
        return search

    def search_artists(self, query, cfg, limit=10):
        if not query:
            return []
        query = query.strip()
        patterns = []
        for pattern in (query, "%" + query + "%", query.replace(" ", "_")):
            if pattern not in patterns:
                patterns.append(pattern)
        last_error = None
        had_valid_response = False
        for name_pattern in patterns:
            try:
                data = self._api(
                    "artist", "index", None, None, cfg,
                    limit=max(limit * 3, 20), extra_params={"name": name_pattern},
                )
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    if "artist" not in data:
                        if not data or "@attributes" in data:
                            items = []
                        else:
                            raise RuntimeError("%s API 返回了无法识别的画师列表" % self.label)
                    else:
                        artists = data.get("artist")
                        if artists is None:
                            items = []
                        elif isinstance(artists, list):
                            items = artists
                        elif isinstance(artists, dict):
                            items = [artists]
                        else:
                            raise RuntimeError("%s API 返回了无法识别的画师列表" % self.label)
                else:
                    raise RuntimeError("%s API 返回了无法识别的画师列表" % self.label)
                if any(not isinstance(item, dict) for item in items):
                    raise RuntimeError("%s API 返回了损坏的画师数据" % self.label)
                had_valid_response = True
            except Exception as exc:
                last_error = exc
                continue
            if not items:
                continue
            out = []
            for a in items[:limit]:
                out.append({
                    "id": str(a.get("id")),
                    "name": a.get("name", "") or a.get("title", ""),
                    "site": self.id,
                    "profile_url": "%s/index.php?page=post&s=list&tags=%s" % (
                        self.api_base, urllib.parse.quote(a.get("name", "").replace(" ", "_"))),
                    "post_count": a.get("post_count"),
                    "other_names": a.get("aliases", ""),
                    "is_banned": False,
                })
            if out:
                return out
        if not had_valid_response and last_error is not None:
            raise last_error
        return []

    def count_posts(self, artist_key, cfg):
        search = self._build_search(artist_key, cfg)
        try:
            data = self._api("post", "index", 0, search, cfg, limit=1)
        except Exception:
            return -1
        if isinstance(data, dict):
            # @attributes 中有 count
            attr = data.get("@attributes") or {}
            try:
                return int(attr.get("count", -1))
            except (TypeError, ValueError):
                return -1
        return -1

    def list_posts(self, artist_key, page, cfg):
        search = self._build_search(artist_key, cfg)
        data = self._api("post", "index", page - 1, search, cfg, limit=PAGE_LIMIT)
        out = []
        for item in self._post_items(data):
            out.append(self._normalize_post(item))
        return out

    def _normalize_post(self, item):
        url = item.get("file_url") or item.get("sample_url") or item.get("preview_url")
        ext = ""
        if url:
            ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
        is_video = ext in ("mp4", "webm", "zip", "gif")
        # Gelbooru 的 owner / source / tags
        return Post(
            id=str(item.get("id")),
            ext=ext,
            file_url=url,
            large_url=item.get("sample_url"),
            is_video=is_video,
            artist=item.get("owner", ""),
            raw=item,
        )

    def build_caption(self, post, cfg):
        p = post.get("raw", {})
        tags = (p.get("tags") or "").split()
        if cfg.get("include_artist"):
            owner = (p.get("owner") or "").replace(" ", "_")
            if owner and owner not in tags:
                tags = [owner] + tags
        if cfg["tag_format"] == "comma":
            pretty = [t.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
                      for t in tags]
            return ", ".join(pretty)
        return " ".join(tags)


class GelbooruSource(GelbooruLikeSource):
    id = "gelbooru"
    label = "Gelbooru"
    api_base = "https://gelbooru.com"
    needs_auth = True

    def normalize_cfg(self, body):
        cfg = super().normalize_cfg(body)
        if isinstance(cfg, str):
            return cfg
        if not cfg.get("user_id") or not cfg.get("api_key"):
            return "Gelbooru 当前要求填写 User ID 与 API Key"
        return cfg


class SafebooruSource(GelbooruLikeSource):
    """Safebooru 用 Gelbooru dapi,但站点没有 rating/owner 重叠。"""
    id = "safebooru"
    label = "Safebooru.org"
    api_base = "https://safebooru.org"
    RATING_VALUES = ("",)
    can_count = False
    supports_artist_search = True

    def normalize_cfg(self, body):
        cfg = super().normalize_cfg(body)
        if isinstance(cfg, str):
            return cfg
        cfg["rating"] = ""  # Safebooru 没有评级筛选
        return cfg

    def _normalize_post(self, item):
        # Safebooru 字段:image, directory, hash, height, width, id, sample, sample_height ...
        # URL: https://safebooru.org/images/<directory>/<image>  (legacy) or <cdn>
        # file_url 取 sample ? image
        base = self.api_base
        directory = item.get("directory")
        img = item.get("image")
        sample = item.get("sample")
        cdn = item.get("image_cdna") or item.get("cdn")  # 新版可能用 cdn
        if cdn and img in cdn:
            url = cdn
        elif directory and img:
            url = "%s/images/%s/%s" % (base, directory, img)
        elif img:
            url = base + "/images/" + img
        else:
            url = item.get("file_url")
        ext = (img or url or "").rsplit(".", 1)[-1].lower().split("?")[0]
        is_video = ext in ("mp4", "webm", "zip", "gif")
        return Post(
            id=str(item.get("id")),
            ext=ext,
            file_url=url,
            large_url=sample,
            is_video=is_video,
            artist="",  # safebooru tags 是混合的,无 owner 字段
            raw=item,
            extra_headers={"Referer": base + "/"},
        )

    def search_artists(self, query, cfg, limit=10):
        if not query:
            return []
        params = {
            "page": "dapi", "s": "tag", "q": "index",
            # Safebooru uses SQL-style wildcards here; '*' returns an empty tag array.
            "name_pattern": "%%%s%%" % query.strip().replace(" ", "_"),
            "limit": max(limit * 3, 20),
        }
        url = self.api_base + "/index.php?" + urllib.parse.urlencode(params)
        status, raw = http_request(url, cfg.get("proxy"), raw=True)
        if status < 200 or status >= 300:
            raise RuntimeError("%s 画师搜索请求失败：HTTP %s" % (self.label, status))
        root = ET.fromstring(raw)
        if root.tag.lower() == "error" or str(root.get("success") or "").lower() == "false":
            detail = root.get("reason") or root.get("message") or (root.text or "unknown error")
            raise RuntimeError("%s API 返回错误：%s" % (self.label, detail.strip()))
        out = []
        for tag in root.findall(".//tag"):
            if str(tag.get("type") or "") != "1":
                continue
            name = tag.get("name") or ""
            out.append({
                "id": name,
                "name": name,
                "site": self.id,
                "profile_url": "%s/index.php?page=post&s=list&tags=%s" % (
                    self.api_base, urllib.parse.quote(name)),
                "post_count": int(tag.get("count") or 0),
                "other_names": "",
                "is_banned": False,
            })
        out.sort(key=lambda x: (-int(x.get("post_count") or 0), x.get("name", "")))
        return out[:limit]

    def count_posts(self, artist_key, cfg):
        try:
            data = self._api("post", "index", 0, artist_key, cfg, limit=1)
        except Exception:
            return -1
        if isinstance(data, dict):
            attr = data.get("@attributes") or {}
            try:
                return int(attr.get("count", -1))
            except (TypeError, ValueError):
                return -1
        return -1

    def list_posts(self, artist_key, page, cfg):
        # safebooru 直接以 tag 搜索
        data = self._api("post", "index", page - 1, artist_key, cfg, limit=PAGE_LIMIT)
        out = []
        for item in self._post_items(data):
            out.append(self._normalize_post(item))
        return out

    def build_caption(self, post, cfg):
        p = post.get("raw", {})
        tags = (p.get("tags") or "").split()
        if cfg["tag_format"] == "comma":
            pretty = [t.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
                      for t in tags]
            return ", ".join(pretty)
        return " ".join(tags)
