# -*- coding: utf-8 -*-
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from sources.gelbooru import GelbooruSource, SafebooruSource
from sources.moebooru import KonachanSource, YandereSource


GELBOORU_CFG = {
    "user_id": "12345",
    "api_key": "test-api-key",
    "proxy": "",
    "rating": "",
}
PUBLIC_CFG = {"proxy": "", "rating": ""}


def query_params(mock_request):
    url = mock_request.call_args.args[0]
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class GelbooruAdapterTests(unittest.TestCase):
    def test_artist_search_sends_auth_in_single_request(self):
        source = GelbooruSource()
        response = {"artist": [{"id": 7, "name": "kantoku", "post_count": 42}]}
        with patch("sources.gelbooru.http_request", return_value=response) as request:
            artists = source.search_artists("kantoku", GELBOORU_CFG, limit=5)

        self.assertEqual([item["name"] for item in artists], ["kantoku"])
        request.assert_called_once()
        params = query_params(request)
        self.assertEqual(params["s"], ["artist"])
        self.assertEqual(params["name"], ["kantoku"])
        self.assertEqual(params["user_id"], ["12345"])
        self.assertEqual(params["api_key"], ["test-api-key"])

    def test_artist_search_deduplicates_identical_patterns(self):
        source = GelbooruSource()
        with patch("sources.gelbooru.http_request", return_value={"artist": []}) as request:
            self.assertEqual(source.search_artists("kantoku", GELBOORU_CFG), [])

        self.assertEqual(request.call_count, 2)

    def test_normalize_post_repairs_double_slash_and_sets_hotlink_headers(self):
        source = GelbooruSource()
        post = source._normalize_post({
            "id": 10210536,
            "file_url": "https://img4.gelbooru.com//images/18/53/abc.jpeg",
            "sample_url": "https://img4.gelbooru.com//samples/18/53/sample_abc.jpg",
            "owner": "someone",
        })
        self.assertEqual(
            post["file_url"], "https://img4.gelbooru.com/images/18/53/abc.jpeg")
        self.assertEqual(
            post["large_url"], "https://img4.gelbooru.com/samples/18/53/sample_abc.jpg")
        self.assertIn("id=10210536", post["extra_headers"]["Referer"])
        self.assertIn("Mozilla/", post["extra_headers"]["User-Agent"])

    def test_count_limit_is_a_query_parameter_not_a_tag(self):
        cases = (
            (GelbooruSource(), GELBOORU_CFG, "kantoku"),
            (SafebooruSource(), PUBLIC_CFG, "kantoku"),
        )
        for source, cfg, expected_tags in cases:
            with self.subTest(source=source.id):
                response = {"@attributes": {"count": "42"}, "post": []}
                with patch("sources.gelbooru.http_request", return_value=response) as request:
                    self.assertEqual(source.count_posts("kantoku", cfg), 42)
                params = query_params(request)
                self.assertEqual(params["tags"], [expected_tags])
                self.assertEqual(params["limit"], ["1"])
                self.assertNotIn("limit:1", params["tags"][0])

    def test_list_rejects_malformed_response_instead_of_ending_normally(self):
        for source, cfg in (
            (GelbooruSource(), GELBOORU_CFG),
            (SafebooruSource(), PUBLIC_CFG),
        ):
            with self.subTest(source=source.id):
                source._api = Mock(return_value="<html>upstream failure</html>")
                with self.assertRaisesRegex(RuntimeError, "作品列表"):
                    source.list_posts("kantoku", 1, cfg)

    def test_valid_empty_list_response_remains_compatible(self):
        for source, cfg in (
            (GelbooruSource(), GELBOORU_CFG),
            (SafebooruSource(), PUBLIC_CFG),
        ):
            with self.subTest(source=source.id):
                source._api = Mock(return_value={"@attributes": {"count": "0"}})
                self.assertEqual(source.list_posts("kantoku", 1, cfg), [])

    def test_artist_search_request_errors_are_not_hidden(self):
        source = GelbooruSource()
        source._api = Mock(side_effect=OSError("offline"))
        with self.assertRaisesRegex(OSError, "offline"):
            source.search_artists("kantoku", GELBOORU_CFG)
        self.assertEqual(source._api.call_count, 2)

    def test_safebooru_artist_search_errors_are_not_hidden(self):
        source = SafebooruSource()
        with patch("sources.gelbooru.http_request", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                source.search_artists("kantoku", PUBLIC_CFG)

    def test_safebooru_artist_search_uses_supported_fuzzy_wildcard(self):
        source = SafebooruSource()
        xml = (
            b'<?xml version="1.0"?><tags type="array">'
            b'<tag type="1" count="1761" name="kantoku" id="215"/>'
            b'<tag type="0" count="99" name="kantoku_topic" id="216"/>'
            b'</tags>'
        )
        with patch("sources.gelbooru.http_request", return_value=(200, xml)) as request:
            artists = source.search_artists("kantoku", PUBLIC_CFG)

        self.assertEqual([item["name"] for item in artists], ["kantoku"])
        self.assertEqual(query_params(request)["name_pattern"], ["%kantoku%"])


class MoebooruAdapterTests(unittest.TestCase):
    def test_uploader_is_not_used_as_artist_or_artist_caption_tag(self):
        source = KonachanSource()
        source._api = Mock(return_value=[{
            "id": 9,
            "file_url": "https://example.test/9.jpg",
            "tags": "1girl",
            "author": "site_uploader",
        }])

        post = source.list_posts("real_artist", 1, PUBLIC_CFG)[0]
        caption = source.build_caption(post, {
            "include_artist": True,
            "tag_format": "comma",
        })

        self.assertEqual(post["artist"], "real_artist")
        self.assertEqual(caption, "real artist, 1girl")
        self.assertNotIn("site uploader", caption)

    def test_list_rejects_malformed_response_for_both_sites(self):
        for source in (KonachanSource(), YandereSource()):
            with self.subTest(source=source.id):
                source._api = Mock(return_value={"unexpected": True})
                with self.assertRaisesRegex(RuntimeError, "作品列表"):
                    source.list_posts("kantoku", 1, PUBLIC_CFG)

    def test_valid_empty_list_response_remains_compatible(self):
        for source in (KonachanSource(), YandereSource()):
            with self.subTest(source=source.id):
                source._api = Mock(return_value=[])
                self.assertEqual(source.list_posts("kantoku", 1, PUBLIC_CFG), [])

    def test_artist_search_request_errors_are_not_hidden_for_both_sites(self):
        for source in (KonachanSource(), YandereSource()):
            with self.subTest(source=source.id):
                source._api = Mock(side_effect=OSError("offline"))
                with self.assertRaisesRegex(OSError, "offline"):
                    source.search_artists("kantoku", PUBLIC_CFG)
                self.assertEqual(source._api.call_count, 2)


if __name__ == "__main__":
    unittest.main()
