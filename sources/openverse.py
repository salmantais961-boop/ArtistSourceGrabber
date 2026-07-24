# -*- coding: utf-8 -*-
"""Openverse image source with explicit open-license metadata.

Openverse aggregates metadata from many providers.  A matching ``creator``
string is therefore a useful discovery hint, but not proof that two records
refer to the same person.  Artist candidates returned here are deliberately
marked as low-confidence and should be confirmed by their source profile.
"""
import os
import urllib.parse

from .base import Post, Source
from http_util import describe_error, http_request


class OpenverseSource(Source):
    id = "openverse"
    label = "Openverse"
    api_base = "https://api.openverse.org/v1"
    needs_auth = False
    supports_artist_search = True
    can_count = True
    PAGE_LIMIT = 20  # anonymous API limit

    def normalize_cfg(self, body):
        artist = str(body.get("artist") or "").strip()
        if not artist:
            return "请填写 Openverse creator 名称"
        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            return "下载数量必须是整数"
        if count < 0:
            return "下载数量不能为负数"
        license_type = str(body.get("openverse_license") or "all").strip()
        if license_type not in ("all", "commercial", "modification"):
            return "Openverse 许可筛选不合法"
        tag_format = body.get("tag_format") if body.get("tag_format") in ("comma", "space") else "comma"
        return {
            "artist": artist, "count": count, "rating": "", "tag_format": tag_format,
            "include_artist": bool(body.get("include_artist")),
            "include_meta": bool(body.get("include_meta")),
            "skip_video": True, "proxy": str(body.get("proxy") or "").strip(),
            "openverse_license": license_type,
        }

    @staticmethod
    def _raise_api_error(data):
        if not isinstance(data, dict):
            raise RuntimeError("Openverse API 返回了无法识别的数据")
        # urllib may successfully decode the JSON body of an HTTP error.  Do
        # not mistake that body for a successful, empty result page.
        detail = data.get("detail") or data.get("error")
        if detail and "results" not in data:
            if isinstance(detail, (dict, list)):
                detail = str(detail)
            raise RuntimeError("Openverse API 返回错误：%s" % detail)
        if "results" not in data:
            raise RuntimeError("Openverse API 返回了无法识别的结果页")

    def _api(self, params, cfg):
        url = self.api_base + "/images/?" + urllib.parse.urlencode(params)
        data = http_request(url, cfg.get("proxy"), timeout=45)
        self._raise_api_error(data)
        return data

    def _params(self, creator, page, cfg, page_size=None):
        params = {"page": page, "page_size": page_size or self.PAGE_LIMIT}
        if cfg.get("query_type") == "artist":
            params["creator"] = creator
        else:
            params["q"] = creator
        if cfg.get("openverse_license") != "all":
            params["license_type"] = cfg["openverse_license"]
        return params

    @staticmethod
    def _results(data):
        results = data.get("results")
        if not isinstance(results, list):
            raise RuntimeError("Openverse API 返回了损坏的结果列表")
        if any(not isinstance(item, dict) for item in results):
            raise RuntimeError("Openverse API 返回了损坏的图片数据")
        return results

    def test(self, cfg):
        try:
            self._api({"q": "illustration", "page_size": 1}, cfg)
            return True, "Openverse API 连接正常"
        except Exception as exc:
            return False, describe_error(exc)

    def resolve_artist(self, cfg, logger):
        return cfg["artist"].strip()

    def search_artists(self, query, cfg, limit=10):
        data = self._api({"q": query, "page_size": self.PAGE_LIMIT}, cfg)
        creators = {}
        for item in self._results(data):
            name = str(item.get("creator") or "").strip()
            if not name:
                continue
            key = name.casefold()
            entry = creators.setdefault(key, {
                "id": name, "name": name, "site": self.id,
                "profile_url": item.get("creator_url") or item.get("foreign_landing_url") or "",
                "post_count": 0,
                "other_names": "低置信度：仅按 Openverse creator 文本匹配，请核对原始主页",
                "match_confidence": "low",
                "match_note": "Openverse 聚合元数据不能唯一确认作者身份",
                "is_banned": False,
            })
            entry["post_count"] += 1
        return sorted(creators.values(), key=lambda x: (-x["post_count"], x["name"]))[:limit]

    def count_posts(self, artist_key, cfg):
        data = self._api(self._params(artist_key, 1, cfg, 1), cfg)
        value = data.get("result_count")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise RuntimeError("Openverse API 未返回有效的作品总数")

    def list_posts(self, artist_key, page, cfg):
        data = self._api(self._params(artist_key, page, cfg), cfg)
        results = self._results(data)
        out = []
        for item in results:
            if not item.get("id"):
                raise RuntimeError("Openverse API 返回了缺少 ID 的图片数据")
            url = item.get("url") or item.get("thumbnail")
            if not url:
                continue
            ext = str(item.get("filetype") or "").lower().lstrip(".")
            if not ext:
                ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lstrip(".").lower()
            if ext not in ("jpg", "jpeg", "png", "gif", "webp", "avif"):
                ext = "jpg"
            out.append(Post(
                id=str(item.get("id") or ""), ext=ext, file_url=url,
                large_url=item.get("thumbnail"), is_video=False,
                artist=item.get("creator") or "", raw=item,
                page_url=item.get("foreign_landing_url") or "",
            ))
        if results and not out:
            raise RuntimeError("Openverse 返回了作品，但没有可下载的图片 URL")
        return out

    def make_filename(self, post, cfg):
        return "openverse_%s.%s" % (post.get("id"), post.get("ext") or "jpg")

    def build_caption(self, post, cfg):
        item = post.get("raw") or {}
        tags = [str(t.get("name") or "") for t in item.get("tags") or []
                if isinstance(t, dict) and t.get("name")]
        if cfg.get("include_artist") and item.get("creator"):
            tags.insert(0, str(item["creator"]).replace(" ", "_"))
        if cfg.get("include_meta"):
            for value in (item.get("license"), item.get("source"), item.get("provider")):
                if value:
                    tags.append(str(value).replace(" ", "_"))
        if cfg.get("tag_format") == "comma":
            return ", ".join(t.replace("_", " ") for t in tags)
        return " ".join(t.replace(" ", "_") for t in tags)
