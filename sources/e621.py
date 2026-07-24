# -*- coding: utf-8 -*-
"""e621(e621.net)源。

e621 使用 Danbooru 风格 v2 API:
  GET /posts.json?tags=...&limit=...&page=...
  GET /artists.json?search[name_matches]=*foo*
鉴权:HTTP Basic `Authorization: Basic base64(login:api_key)`
"""
import base64
import re
import urllib.parse

from .base import Source, Post, normalize_search_tags
from http_util import http_request, describe_error


PAGE_LIMIT = 320  # e621 上限 320


class E621Source(Source):
    """不接入——e621 含 zoophilia 等争议内容,默认禁用以便后续判断。

    这里仅留壳以便扩展(NOT 在 registry 中注册)。"""
    pass


class E621RealSource(Source):
    id = "e621"
    label = "e621"
    api_base = "https://e621.net"
    needs_auth = False  # 匿名也能搜 posts
    supports_artist_search = True
    can_count = True
    RATING_VALUES = ("", "s", "q", "e")
    # e621 rating:safe / questionable / explicit

    def normalize_cfg(self, body):
        artist = str(body.get("artist") or "").strip()
        if not artist:
            return "请填写画师 tag / ID"
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

    def _auth_header(self, cfg):
        if cfg.get("login") and cfg.get("api_key"):
            token = "%s:%s" % (cfg["login"], cfg["api_key"])
            b64 = base64.b64encode(token.encode("utf-8")).decode("ascii")
            return {"Authorization": "Basic %s" % b64,
                    "User-Agent": "MultiBooruGrabber/2.0 (by personal archival tool)"}
        return {"User-Agent": "MultiBooruGrabber/2.0 (personal archival tool)"}

    def _api(self, path, params, cfg):
        url = self.api_base + path + "?" + urllib.parse.urlencode(params or {})
        return http_request(url, cfg.get("proxy"), headers=self._auth_header(cfg))

    def test(self, cfg):
        try:
            self._api("/posts.json", {"limit": 1}, cfg)
            return True, "连接正常" + ("(已登录)" if cfg.get("login") else "(匿名)")
        except Exception as exc:
            return False, describe_error(exc)

    def resolve_artist(self, cfg, logger):
        raw = cfg["artist"].strip()
        if cfg.get("query_type", "artist") != "artist":
            query = normalize_search_tags(raw)
            if not query:
                raise RuntimeError("请至少填写一个有效标签")
            return query
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
        return raw.replace(" ", "_")

    def search_artists(self, query, cfg, limit=10):
        if not query:
            return []
        query = query.strip()
        out = []
        for pat in (query, "*%s*" % query):
            try:
                data = self._api("/artists.json",
                                {"search[name_matches]": pat, "limit": limit}, cfg)
            except Exception:
                continue
            seen = set()
            if isinstance(data, list):
                for a in data:
                    aid = a.get("id")
                    if aid in seen:
                        continue
                    seen.add(aid)
                    out.append({
                        "id": str(aid),
                        "name": a.get("name", ""),
                        "site": self.id,
                        "profile_url": "%s/artists/%s" % (self.api_base, a.get("name", "")),
                        "post_count": a.get("post_count"),
                        "other_names": a.get("group_name", "") or "",
                        "is_banned": bool(a.get("is_banned")),
                    })
            if len(out) >= limit:
                break
        return out[:limit]

    def count_posts(self, artist_key, cfg):
        s = artist_key
        if cfg.get("rating"):
            s += " rating:%s" % cfg["rating"]
        try:
            from http_util import http_request as r
            # 用 posts.json 的 posts 数组 + total? e621 v2 在 /posts.json?limit=1 中有
            # 顶层 @attributes 不出现,e621 改用 /counts/posts.json?tags=
            counts = r(self.api_base + "/counts/posts.json?" + urllib.parse.urlencode({"tags": s}),
                       cfg.get("proxy"), headers=self._auth_header(cfg))
        except Exception:
            return -1
        if isinstance(counts, dict):
            return int((counts.get("counts") or {}).get("posts") or 0)
        return -1

    def list_posts(self, artist_key, page, cfg):
        s = artist_key
        if cfg.get("rating"):
            s += " rating:%s" % cfg["rating"]
        data = self._api("/posts.json", {"tags": s, "limit": PAGE_LIMIT, "page": page}, cfg)
        out = []
        if isinstance(data, dict):
            posts = data.get("posts") or []
            for p in posts:
                out.append(self._normalize_post(p))
        return out

    def _normalize_post(self, p):
        file_ = p.get("file") or {}
        url = file_.get("url") or (p.get("large_file_url") or "") or (p.get("preview_file_url") or "")
        ext = (file_.get("ext") or "").lower()
        if not ext and url:
            ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
        is_video = ext in ("mp4", "webm", "zip", "gif")
        tags = p.get("tags") or {}
        if isinstance(tags, dict):
            artist_list = tags.get("artist") or []
        else:
            artist_list = (p.get("tag_string_artists") or "").split()
        artist = " ".join(artist_list) if isinstance(artist_list, list) else str(artist_list)
        return Post(
            id=str(p.get("id")),
            ext=ext,
            file_url=url,
            large_url=p.get("large_file_url"),
            is_video=is_video,
            artist=artist,
            raw=p,
        )

    def build_caption(self, post, cfg):
        p = post.get("raw", {})
        tags = p.get("tags") or {}
        # e621 tags 按 group 分桶
        groups_order = ["artist", "character", "copyright", "species", "general", "meta"]
        out_tags = []
        if isinstance(tags, dict):
            for k in groups_order:
                if k == "meta" and not cfg.get("include_meta"):
                    continue
                if k == "artist" and not cfg.get("include_artist"):
                    continue
                v = tags.get(k) or []
                if isinstance(v, list):
                    out_tags.extend(v)
        else:
            out_tags = str(tags).split()
        if cfg["tag_format"] == "comma":
            pretty = [t.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
                      for t in out_tags]
            return ", ".join(pretty)
        return " ".join(out_tags)
