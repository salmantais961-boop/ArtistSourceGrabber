# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

import app
from sources import SOURCES
from sources.danbooru import DanbooruSource


class DownloadHtmlGuardTests(unittest.TestCase):
    def test_download_rejects_html_masquerading_as_media(self):
        resp = mock.MagicMock()
        resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
        resp.read.side_effect = [b"<!DOCTYPE html><html></html>", b""]
        resp.__enter__.return_value = resp
        opener = mock.Mock()
        opener.open.return_value = resp
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "post.jpg")
            with mock.patch.object(app, "build_opener", return_value=opener):
                with self.assertRaisesRegex(RuntimeError, "网页"):
                    app._download("https://img.example/x.jpg", target, {"proxy": ""})
            self.assertEqual(os.listdir(tmpdir), [])


class CoreTests(unittest.TestCase):
    def test_enabled_registry_excludes_disabled_sources(self):
        self.assertIn("twitter", SOURCES)
        self.assertIn("danbooru", SOURCES)
        self.assertIn("openverse", SOURCES)
        self.assertNotIn("e621", SOURCES)
        self.assertNotIn("atfbooru", SOURCES)

    def test_extract_x_handles_uses_only_active_profile_links(self):
        urls = [
            {"url": "https://x.com/old_name", "is_active": False},
            {"url": "https://twitter.com/Artist_Name", "is_active": True},
            {"url": "https://x.com/i/user/123", "is_active": True},
            "https://example.com/not-x",
        ]
        self.assertEqual(app.extract_x_handles(urls), ["Artist_Name"])

    def test_extract_x_numeric_user_ids(self):
        urls = [{"url": "https://x.com/i/user/776240313467756544", "is_active": True}]
        self.assertEqual(app.extract_x_user_ids(urls), ["776240313467756544"])

    def test_extract_pixiv_numeric_user_ids(self):
        urls = [
            {"url": "https://www.pixiv.net/users/1565632", "is_active": True},
            {"url": "https://www.pixiv.net/fanbox/creator/1565632", "is_active": True},
            {"url": "https://www.pixiv.net/stacc/5th-year", "is_active": True},
        ]
        self.assertEqual(app.extract_pixiv_user_ids(urls), ["1565632"])

    def test_tag_format_deduplicates_native_and_generated(self):
        text = app._format_tags(["1girl", "1girl", "blue_hair", "foo \\(bar\\)"], "comma")
        self.assertEqual(text, "1girl, blue hair, foo \\(bar\\)")

    def test_danbooru_canonical_name_ranks_above_alias(self):
        source = DanbooruSource()
        source._api = lambda path, params, cfg: [
            {"id": 1, "name": "other", "other_names": ["kantoku"], "urls": []},
            {"id": 2, "name": "kantoku", "other_names": [], "urls": []},
        ]
        result = source.search_artists("kantoku", {"proxy": ""}, 2)
        self.assertEqual([item["name"] for item in result], ["kantoku", "other"])
        self.assertEqual(result[0]["match_reason"], "name_exact")

    def test_danbooru_url_exact_is_highest_confidence(self):
        source = DanbooruSource()
        source._api = lambda path, params, cfg: [{
            "id": 2, "name": "kantoku", "other_names": [],
            "urls": [{"url": "https://x.com/kantoku_5th", "is_active": True}],
        }]
        result = source.search_artists("https://x.com/kantoku_5th", {"proxy": ""}, 1)
        self.assertEqual(result[0]["match_reason"], "url_exact")


if __name__ == "__main__":
    unittest.main()
