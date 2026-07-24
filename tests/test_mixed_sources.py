# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

import app
from sources.base import Source


class FakeSource(Source):
    def __init__(self, source_id, posts=None, test_ok=True):
        self.id = source_id
        self.label = source_id.title()
        self.posts = list(posts or [])
        self.test_ok = test_ok

    def normalize_cfg(self, body):
        return dict(body)

    def test(self, cfg):
        return (self.test_ok, "ok" if self.test_ok else "boom")

    def resolve_artist(self, cfg, logger):
        return cfg["artist"]

    def count_posts(self, artist_key, cfg):
        return len(self.posts)

    def list_posts(self, artist_key, page, cfg):
        return self.posts if page == 1 else []

    def build_caption(self, post, cfg):
        return post.get("caption", "")


def make_run(source, artist="canonical", **overrides):
    cfg = {
        "artist": artist, "canonical_artist": "canonical", "count": 0,
        "tag_format": "comma", "include_artist": True, "skip_video": True,
        "proxy": "",
    }
    cfg.update(overrides)
    return {"source": source, "cfg": cfg, "skip": False}


class FakeTagger:
    def __init__(self, tags):
        self.id = "local_onnx"
        self.tags = tags
        self.contexts = []

    def tag(self, image_path, context):
        self.contexts.append(context)
        return list(self.tags)

    def test(self):
        return True, "ok"


class FailingTagger(FakeTagger):
    def tag(self, image_path, context):
        self.contexts.append(context)
        raise RuntimeError(
            "provider rejected api_key=verysecretvalue auth_token=abc123")


class MixedSourceTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.downloads_patch = mock.patch.object(app, "DOWNLOAD_DIR", self.tmp.name)
        self.file_interval_patch = mock.patch.object(app, "FILE_INTERVAL", 0)
        self.list_interval_patch = mock.patch.object(app, "LIST_INTERVAL", 0)
        self.downloads_patch.start()
        self.file_interval_patch.start()
        self.list_interval_patch.start()

    def tearDown(self):
        self.list_interval_patch.stop()
        self.file_interval_patch.stop()
        self.downloads_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def fake_download(url, path, cfg, headers=None):
        payload = url.split("memory://", 1)[-1].encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(payload)

    def test_one_source_succeeds_and_one_fails_without_stopping_task(self):
        good = FakeSource("good", [{"id": "1", "ext": "jpg",
                                    "file_url": "memory://one", "caption": "tag_one"}])
        bad = FakeSource("bad", test_ok=False)
        task = app.Task([make_run(good), make_run(bad)], tagger=None)
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)

        snapshot = task.snapshot()
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["done"], 1)
        self.assertEqual(snapshot["failed"], 1)
        self.assertEqual([state["status"] for state in snapshot["sources"]], ["done", "error"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "canonical", "good__1.jpg")))

    def test_identical_content_is_deduplicated_and_caption_tags_are_merged(self):
        first = FakeSource("first", [{"id": "10", "ext": "jpg",
                                      "file_url": "memory://same", "caption": "tag_a"}])
        second = FakeSource("second", [{"id": "20", "ext": "jpg",
                                        "file_url": "memory://same", "caption": "tag_b"}])
        task = app.Task([make_run(first), make_run(second)], tagger=None)
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)

        folder = os.path.join(self.tmp.name, "canonical")
        media = [name for name in os.listdir(folder) if name.endswith(".jpg")]
        self.assertEqual(media, ["first__10.jpg"])
        self.assertEqual(task.done, 1)
        self.assertEqual(task.skipped, 1)
        with open(os.path.join(folder, "first__10.txt"), encoding="utf-8") as fh:
            tags = fh.read()
        self.assertIn("tag a", tags)
        self.assertIn("tag b", tags)
        items = task.snapshot()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["status"], "duplicate")
        self.assertEqual(items[1]["filename"], "first__10.jpg")
        self.assertEqual(items[1]["image_url"], items[0]["image_url"])
        self.assertEqual(items[1]["caption_url"], items[0]["caption_url"])
        self.assertIn("tag a", items[0]["final_tags"])
        self.assertIn("tag b", items[0]["final_tags"])
        self.assertEqual(items[1]["final_tags"], items[0]["final_tags"])

    def test_same_post_id_with_different_content_is_not_deduplicated(self):
        first = FakeSource("first", [{"id": "7", "ext": "jpg",
                                      "file_url": "memory://alpha", "caption": "a"}])
        second = FakeSource("second", [{"id": "7", "ext": "jpg",
                                        "file_url": "memory://beta", "caption": "b"}])
        task = app.Task([make_run(first), make_run(second)], tagger=None)
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)

        folder = os.path.join(self.tmp.name, "canonical")
        media = sorted(name for name in os.listdir(folder) if name.endswith(".jpg"))
        self.assertEqual(media, ["first__7.jpg", "second__7.jpg"])
        self.assertEqual(task.done, 2)
        self.assertEqual(task.skipped, 0)

    def test_page_range_limits_source_pagination(self):
        class PagedSource(FakeSource):
            def __init__(self):
                super().__init__("paged")
                self.pages = []

            def count_posts(self, artist_key, cfg):
                return -1

            def list_posts(self, artist_key, page, cfg):
                self.pages.append(page)
                if page > 3:
                    return []
                return [{"id": str(page), "ext": "jpg",
                         "file_url": "memory://page-%s" % page,
                         "caption": "page_%s" % page}]

        source = PagedSource()
        task = app.Task([make_run(source, start_page=2, end_page=3)], tagger=None)
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)
        self.assertEqual(source.pages, [2, 3])
        self.assertEqual(task.done, 2)
        self.assertEqual(task.target, 2)

    def test_page_range_does_not_use_whole_site_count_as_target(self):
        class CountedPagedSource(FakeSource):
            def __init__(self):
                super().__init__("counted-paged")

            def count_posts(self, artist_key, cfg):
                return 100

            def list_posts(self, artist_key, page, cfg):
                return [{"id": str(page), "ext": "jpg",
                         "file_url": "memory://counted-page-%s" % page,
                         "caption": "page_%s" % page}]

        source = CountedPagedSource()
        task = app.Task([make_run(source, count=0, start_page=2, end_page=3)],
                        tagger=None)
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)
        self.assertEqual(task.done, 2)
        self.assertEqual(task.target, 2)

    def test_pixiv_onnx_tagger_only_excludes_native_tags(self):
        source = FakeSource("pixiv", [{"id": "42", "ext": "jpg",
                                       "file_url": "memory://pixiv",
                                       "caption": "pixiv_native source_meta"}])
        tagger = FakeTagger(["1girl", "blue_eyes"])
        task = app.Task([make_run(source, tag_merge_mode="tagger_only")], tagger=tagger)
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)
        with open(os.path.join(self.tmp.name, "canonical", "pixiv__42.txt"), encoding="utf-8") as fh:
            caption = fh.read()
        self.assertEqual(caption, "canonical, 1girl, blue eyes")
        self.assertEqual(tagger.contexts[0]["native_tags"], ["pixiv_native source_meta"])
        item = task.snapshot()["items"][0]
        self.assertEqual(item["filename"], "pixiv__42.jpg")
        self.assertEqual(item["preview_url"], item["image_url"])
        self.assertEqual(item["url"], item["image_url"])
        self.assertEqual(item["caption_url"], "/files/canonical/pixiv__42.txt")
        self.assertEqual(item["native_tags"], ["pixiv_native source_meta"])
        self.assertEqual(item["generated_tags"], ["1girl", "blue_eyes"])
        self.assertEqual(item["final_tags"], ["canonical", "1girl", "blue eyes"])
        self.assertEqual(item["tag_merge_mode"], "tagger_only")
        self.assertEqual(item["tagger_id"], "local_onnx")
        self.assertEqual(item["tag_status"], "generated")
        self.assertEqual(item["tag_error"], "")

    def test_skipped_post_has_safe_empty_tagging_details(self):
        source = FakeSource("pixiv", [{"id": "skip", "ext": "jpg",
                                       "caption": "native_tag"}])
        task = app.Task([make_run(source, tag_merge_mode="native_only")], tagger=None)
        app.run_task(task)
        item = task.snapshot()["items"][0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["filename"], "pixiv__skip.jpg")
        self.assertEqual(item["native_tags"], ["native_tag"])
        self.assertEqual(item["generated_tags"], [])
        self.assertEqual(item["final_tags"], [])
        self.assertEqual(item["tag_status"], "skipped")
        self.assertEqual(item["image_url"], "")
        self.assertEqual(item["caption_url"], "")

    def test_tagger_error_is_redacted_in_item_details(self):
        source = FakeSource("pixiv", [{"id": "badtag", "ext": "jpg",
                                       "file_url": "memory://badtag",
                                       "caption": "native_tag"}])
        task = app.Task([make_run(source, tag_merge_mode="native_plus_tagger")],
                        tagger=FailingTagger([]))
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)
        item = task.snapshot()["items"][0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["tag_status"], "failed")
        self.assertIn("[redacted", item["tag_error"])
        self.assertNotIn("verysecretvalue", item["tag_error"])
        self.assertNotIn("abc123", item["tag_error"])
        self.assertEqual(item["generated_tags"], [])
        self.assertEqual(item["final_tags"], ["canonical", "native tag"])

    def test_snapshot_whitelists_item_fields(self):
        source = FakeSource("safe")
        task = app.Task([make_run(source)], tagger=None)
        task.add_item({
            "id": "1", "source": "safe", "status": "ok",
            "native_tags": ["tag"], "raw_llm_response": {"secret": "value"},
            "api_key": "example-private-key", "cookie": "test-cookie-value",
        })
        item = task.snapshot()["items"][0]
        self.assertEqual(item["native_tags"], ["tag"])
        self.assertNotIn("raw_llm_response", item)
        self.assertNotIn("api_key", item)
        self.assertNotIn("cookie", item)

    def test_native_plus_llm_is_consistent_for_source_with_native_tags(self):
        source = FakeSource("pixiv", [{"id": "43", "ext": "jpg",
                                       "file_url": "memory://llm",
                                       "caption": "native_tag"}])
        task = app.Task([make_run(source, tag_merge_mode="native_plus_tagger")],
                        tagger=FakeTagger(["generated_tag"]))
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)
        with open(os.path.join(self.tmp.name, "canonical", "pixiv__43.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "canonical, native tag, generated tag")

    def test_x_without_native_tags_uses_generated_tags(self):
        source = FakeSource("twitter", [{"id": "44", "ext": "jpg",
                                         "file_url": "memory://x", "caption": ""}])
        task = app.Task([make_run(source, tag_merge_mode="tagger_only")],
                        tagger=FakeTagger(["solo"]))
        with mock.patch.object(app, "_download", self.fake_download):
            app.run_task(task)
        with open(os.path.join(self.tmp.name, "canonical", "twitter__44.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "canonical, solo")

    def test_existing_caption_is_replaced_under_tagger_only_policy(self):
        source = FakeSource("pixiv", [{"id": "45", "ext": "jpg",
                                       "file_url": "memory://stale", "caption": "native"}])
        folder = os.path.join(self.tmp.name, "canonical")
        os.makedirs(folder)
        with open(os.path.join(folder, "pixiv__45.jpg"), "wb") as fh:
            fh.write(b"stale")
        with open(os.path.join(folder, "pixiv__45.txt"), "w", encoding="utf-8") as fh:
            fh.write("old_pixiv_tag")
        task = app.Task([make_run(source, tag_merge_mode="tagger_only")],
                        tagger=FakeTagger(["fresh_tag"]))
        app.run_task(task)
        with open(os.path.join(folder, "pixiv__45.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "canonical, fresh tag")

    def test_mixed_payload_keeps_per_source_artist_mapping(self):
        first = FakeSource("first")
        second = FakeSource("second")
        registry = {"first": first, "second": second}
        body = {
            "sources": ["first", "second"],
            "source_configs": {
                "first": {"artist": "artist-one"},
                "second": {"artist": "artist-two"},
            },
            "canonical_artist": "canonical", "count": 2,
            "tag_format": "comma", "tagger_type": "none",
        }
        with mock.patch.object(app, "get_source", side_effect=registry.get):
            runs, _tagger, error = app.normalize_task_configs(body)
        self.assertIsNone(error)
        self.assertEqual([run["cfg"]["artist"] for run in runs],
                         ["artist-one", "artist-two"])
        self.assertEqual([run["cfg"]["canonical_artist"] for run in runs],
                         ["canonical", "canonical"])
        self.assertTrue(all(run["cfg"]["tag_merge_mode"] == "native_only" for run in runs))

    def test_legacy_single_source_payload_remains_supported(self):
        source = FakeSource("single")
        body = {"source": "single", "artist": "artist", "count": 1,
                "tag_format": "comma", "tagger_type": "none"}
        with mock.patch.object(app, "get_source", return_value=source):
            runs, _tagger, error = app.normalize_task_configs(body)
        self.assertIsNone(error)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["cfg"]["artist"], "artist")

    def test_enabled_tagger_defaults_to_tagger_only_for_every_source(self):
        source = FakeSource("pixiv")
        body = {"source": "pixiv", "artist": "123", "tagger_type": "onnx",
                "onnx_model_path": "model.onnx", "onnx_tags_path": "tags.csv"}
        with mock.patch.object(app, "get_source", return_value=source), \
                mock.patch("tagging.local_onnx.os.path.isfile", return_value=True):
            runs, _tagger, error = app.normalize_task_configs(body)
        self.assertIsNone(error)
        self.assertEqual(runs[0]["cfg"]["tag_merge_mode"], "tagger_only")

    def test_frontend_submits_source_configs_and_renders_source_states(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(root, "static", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn('id="sourceChecklist"', html)
        self.assertIn('id="sourceStates"', html)
        self.assertIn("source_configs", script)
        self.assertIn("selectedSourceIds", script)
        self.assertIn("d.sources.map", script)
        self.assertIn("pixiv_user_ids", script)
        self.assertIn('source.id==="pixiv"', script)
        self.assertIn('id="tagMergeMode"', html)
        self.assertIn("tag_merge_mode:els.tagMergeMode.value", script)


if __name__ == "__main__":
    unittest.main()
