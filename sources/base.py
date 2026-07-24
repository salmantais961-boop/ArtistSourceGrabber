# -*- coding: utf-8 -*-
"""Source 抽象基类:统一各图站与 X 的抓取接口。

一个 Source 描述如何:
- 校验用户配置(normalize_cfg)
- 测试连接(test)
- 解析画师入口(resolve_artist)
- 模糊搜索画师候选(search_artists)——用于 X 同名画师匹配 Danbooru
- 列出作品 / 下载作品 / 构造同名 txt 标注
"""
import os
import re


VIDEO_EXTS = {"mp4", "webm", "zip", "gif"}


def split_search_tags(value, limit=50):
    """Parse comma/space/newline separated booru tags with stable deduping."""

    text = str(value or "").strip()
    if not text:
        return []
    tags = []
    seen = set()
    for raw in re.split(r"[\s,，、;；]+", text):
        tag = raw.strip().lstrip("#")
        if not tag:
            continue
        tag = tag[:120]
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def normalize_search_tags(value, limit=50):
    """Return a booru AND expression where tags are separated by spaces."""

    return " ".join(split_search_tags(value, limit=limit))


class SourceResult:
    """normalize_cfg 的合法返回值"""


class Post(dict):
    """规范化作品字典。常用字段:
        id            该站点内作品唯一 ID(str)
        ext           文件后缀(无点)
        file_url      原图直链
        large_url     备选大图 URL
        is_video      是否视频/动图
        artist        该站点的画师名(用于打标匹配)
        score
        rating
        raw           站点原始 dict(供 build_caption 使用)
    """


class Source:
    id = ""
    label = ""
    needs_auth = False        # 是否需要 Key/Token 才能用基本功能
    supports_artist_search = False  # 是否支持 search_artists 模糊搜索
    fields = []               # 该源需要用户额外填写的字段名
    can_count = True           # 是否能预知总条数

    def normalize_cfg(self, body):
        """校验前端配置;失败返回错误字符串,成功返回 dict。"""
        raise NotImplementedError

    def test(self, cfg):
        """返回 (ok:bool, message:str)。"""
        raise NotImplementedError

    def resolve_artist(self, cfg, logger):
        """把用户输入解析为搜索关键字 / 画师标识。返回标量。"""
        raise NotImplementedError

    def search_artists(self, query, cfg, limit=10):
        """模糊搜索画师,返回 list[{id,name,profile_url,site?}]。"""
        return []

    def count_posts(self, artist_key, cfg):
        """该 artist 总作品数。无法统计返回 -1。"""
        return -1

    def list_posts(self, artist_key, page, cfg):
        """分页列出作品,返回 list[Post]。page 从 1 开始。"""
        return []

    def build_caption(self, post, cfg):
        """用站点自带 tag 构造 txt 标注(空字符串表示不写原生标注,转交给 tagging 模块)。"""
        return ""

    def skip_post(self, post, cfg):
        """是否要跳过(视频 / 无 URL 等)。"""
        if cfg.get("skip_video") and post.get("is_video"):
            return True
        return False

    def make_filename(self, post, cfg):
        """组装最终文件名(无目录)。默认:<id>.<ext>"""
        return "%s.%s" % (post.get("id"), post.get("ext", "").lower())
