# -*- coding: utf-8 -*-
import unittest
from unittest import mock

from sources.base import normalize_search_tags, split_search_tags
from sources.danbooru import DanbooruSource
from sources.gelbooru import GelbooruSource
from sources.moebooru import KonachanSource


class MultiTagSearchTests(unittest.TestCase):
    def test_tag_parser_accepts_common_separators_and_deduplicates(self):
        self.assertEqual(
            split_search_tags("1girl, blue_eyes\n1girl  solo"),
            ["1girl", "blue_eyes", "solo"],
        )
        self.assertEqual(normalize_search_tags("#1girl，blue_eyes"), "1girl blue_eyes")

    def test_danbooru_returns_one_validated_combination_candidate(self):
        source = DanbooruSource()
        tag_responses = [
            [{"name": "1girl", "post_count": 100, "is_deprecated": False}],
            [{"name": "blue_eyes", "post_count": 80, "is_deprecated": False}],
        ]
        with mock.patch("sources.danbooru.http_request", side_effect=tag_responses), \
                mock.patch.object(source, "_api", return_value={
                    "counts": {"posts": 42}
                }):
            result = source.search_artists(
                "1girl, blue_eyes", {"query_type": "tag", "proxy": ""}, limit=10)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "1girl blue_eyes")
        self.assertEqual(result[0]["post_count"], 42)
        self.assertEqual(result[0]["query_tags"], ["1girl", "blue_eyes"])

    def test_danbooru_keeps_anonymous_null_count_unknown(self):
        source = DanbooruSource()
        tag_responses = [
            [{"name": "1girl", "is_deprecated": False}],
            [{"name": "blue_eyes", "is_deprecated": False}],
        ]
        with mock.patch("sources.danbooru.http_request", side_effect=tag_responses), \
                mock.patch.object(source, "_api", return_value={
                    "counts": {"posts": None}
                }):
            result = source.search_artists(
                "1girl, blue_eyes", {"query_type": "tag", "proxy": ""}, limit=10)
        self.assertIsNone(result[0]["post_count"])

    def test_tag_sources_preserve_and_semantics(self):
        cfg = {"query_type": "tag", "rating": ""}
        self.assertEqual(GelbooruSource()._build_search("1girl blue_eyes", cfg),
                         "1girl blue_eyes")
        self.assertEqual(KonachanSource().resolve_artist(
            {"artist": "1girl, blue_eyes", "query_type": "tag"}, lambda _msg: None),
            "1girl blue_eyes")


if __name__ == "__main__":
    unittest.main()
