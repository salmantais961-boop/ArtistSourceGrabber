# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

from tagging import (
    TagContext,
    create_tagger,
    dedupe_tags,
    format_tags,
    get_tagger,
    list_taggers,
    merge_captions,
    normalize_tagger_config,
)
from tagging.local_onnx import LocalONNXTagger
from tagging.openai_compatible import (
    DEFAULT_PROMPT,
    OpenAICompatibleTagger,
    _LLMHTTPError,
    _safe_summary,
)


class BaseTaggingTests(unittest.TestCase):
    def test_dedupes_spaces_underscores_and_case(self):
        self.assertEqual(
            dedupe_tags(["blue_eyes", "1girl"], "Blue Eyes, smile"),
            ["blue_eyes", "1girl", "smile"],
        )

    def test_formats_and_merges(self):
        self.assertEqual(format_tags(["blue_eyes", "foo_(bar)"], "comma"),
                         "blue eyes, foo \\(bar\\)")
        self.assertEqual(format_tags(["blue eyes", "smile"], "space"),
                         "blue_eyes smile")
        self.assertEqual(merge_captions("1girl blue_eyes", ["Blue Eyes", "smile"]),
                         "1girl, blue eyes, smile")

    def test_none_registry(self):
        result = get_tagger("none").tag(TagContext(image_path="unused"), {})
        self.assertEqual(result.tags, [])
        self.assertEqual({item["id"] for item in list_taggers()},
                         {"none", "openai_compatible", "local_onnx"})

    def test_stable_configured_facade(self):
        cfg = normalize_tagger_config({"tagger_id": "none"})
        self.assertEqual(cfg, {"tagger_id": "none"})
        tagger = create_tagger(cfg)
        self.assertEqual(tagger.test(), (True, "可用"))
        self.assertEqual(tagger.tag("unused", {"artist": "example"}), [])

    def test_frontend_field_aliases(self):
        llm_cfg = normalize_tagger_config({
            "tagger_type": "openai",
            "llm_base_url": "http://localhost:1234",
            "llm_api_key": "secret",
            "llm_model": "vision",
            "llm_prompt": "return tags",
        })
        self.assertEqual(llm_cfg["tagger_id"], "openai_compatible")
        self.assertEqual(llm_cfg["endpoint"], "http://localhost:1234")
        self.assertEqual(llm_cfg["api_key"], "secret")

        onnx_cfg = normalize_tagger_config({
            "tagger_type": "onnx",
            "onnx_model_path": "model.onnx",
            "onnx_tags_path": "selected_tags.csv",
            "onnx_threshold": "0.42",
        })
        self.assertEqual(onnx_cfg["tagger_id"], "local_onnx")
        self.assertEqual(onnx_cfg["threshold"], 0.42)


class OpenAITests(unittest.TestCase):
    def test_endpoint_normalization(self):
        tagger = OpenAICompatibleTagger()
        self.assertEqual(tagger._chat_url("http://localhost:11434"),
                         "http://localhost:11434/v1/chat/completions")
        self.assertEqual(tagger._chat_url("https://example.test/v1"),
                         "https://example.test/v1/chat/completions")

    def test_parses_json_and_plain_tags(self):
        tagger = OpenAICompatibleTagger()
        self.assertEqual(tagger.parse_tags('```json\n["1girl", "blue eyes"]\n```'),
                         ["1girl", "blue eyes"])
        self.assertEqual(tagger.parse_tags("- 1girl\n- smile\nblue eyes"),
                         ["1girl", "smile", "blue eyes"])

    def test_parses_common_structured_variants(self):
        tagger = OpenAICompatibleTagger()
        self.assertEqual(
            tagger.parse_tags('Result:\n```json\n{"tags":["1girl","solo"]}\n```'),
            ["1girl", "solo"],
        )
        self.assertEqual(tagger.parse_tags('{tags: ["blue_eyes", "smile"]}'),
                         ["blue_eyes", "smile"])
        self.assertEqual(tagger.parse_tags('{"keywords":"1girl, upper_body"}'),
                         ["1girl", "upper_body"])

    def test_plain_fallback_rejects_explanatory_prose(self):
        tagger = OpenAICompatibleTagger()
        self.assertEqual(
            tagger.parse_tags("The image depicts a girl wearing blue, blue_eyes, smile"),
            ["blue_eyes", "smile"],
        )
        self.assertEqual(tagger.parse_tags("This image shows a character in a room."), [])
        self.assertEqual(tagger.parse_tags("Here are the tags:\n1girl\nlooking_at_viewer"),
                         ["1girl", "looking_at_viewer"])
        self.assertEqual(tagger.parse_tags("Sure, here are the tags:\nsolo"), ["solo"])

    def test_default_prompt_declares_required_schema(self):
        self.assertIn('{"tags": ["tag_one", "tag_two"]}', DEFAULT_PROMPT)
        self.assertIn("Danbooru/WD14", DEFAULT_PROMPT)
        self.assertIn("visible evidence", DEFAULT_PROMPT)
        self.assertIn("mutually conflicting", DEFAULT_PROMPT)

    def test_key_is_optional(self):
        cfg = OpenAICompatibleTagger().normalize_cfg({
            "endpoint": "http://localhost:1234", "model": "vision-model"
        })
        self.assertIsInstance(cfg, dict)
        self.assertNotIn("Authorization", OpenAICompatibleTagger._headers(cfg))

    def test_tags_an_image_with_mocked_compatible_response(self):
        tagger = OpenAICompatibleTagger()
        cfg = tagger.normalize_cfg({
            "endpoint": "http://localhost:1234", "model": "vision-model"
        })
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image_file:
            image_file.write(b"not-decoded-by-the-remote-client")
            image_path = image_file.name
        try:
            with mock.patch.object(tagger, "_read_json", return_value={
                "choices": [{"message": {"content": '["1girl", "smile"]'}}]
            }):
                result = tagger.tag(TagContext(image_path=image_path), cfg)
        finally:
            os.remove(image_path)
        self.assertEqual(result.tags, ["1girl", "smile"])

    def test_requests_schema_and_appends_contract_to_custom_prompt(self):
        tagger = OpenAICompatibleTagger()
        cfg = tagger.normalize_cfg({
            "endpoint": "http://localhost:1234", "model": "vision-model",
            "prompt": "Focus on clothing.",
        })
        captured = []

        def fake_read(req, _timeout):
            captured.append(__import__("json").loads(req.data.decode("utf-8")))
            return {"choices": [{"message": {"content": '{"tags":["dress"]}'}}]}

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image_file:
            image_file.write(b"image")
            image_path = image_file.name
        try:
            with mock.patch.object(tagger, "_read_json", side_effect=fake_read):
                result = tagger.tag(TagContext(
                    image_path=image_path, native_tags=["blue_hair"]), cfg)
        finally:
            os.remove(image_path)
        self.assertEqual(result.tags, ["dress"])
        request = captured[0]
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        prompt = request["messages"][0]["content"][0]["text"]
        self.assertIn("Focus on clothing.", prompt)
        self.assertIn('{"tags": ["tag_one", "tag_two"]}', prompt)
        self.assertIn('["blue_hair"]', prompt)

    def test_falls_back_when_server_rejects_response_format(self):
        tagger = OpenAICompatibleTagger()
        cfg = tagger.normalize_cfg({
            "endpoint": "http://localhost:1234", "model": "vision-model"
        })
        formats = []

        def fake_read(req, _timeout):
            payload = __import__("json").loads(req.data.decode("utf-8"))
            formats.append(payload.get("response_format"))
            if len(formats) < 3:
                raise _LLMHTTPError(400, "unsupported", format_unsupported=True)
            return {"choices": [{"message": {"content": '["solo"]'}}]}

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image_file:
            image_file.write(b"image")
            image_path = image_file.name
        try:
            with mock.patch.object(tagger, "_read_json", side_effect=fake_read):
                result = tagger.tag(TagContext(image_path=image_path), cfg)
        finally:
            os.remove(image_path)
        self.assertEqual(result.tags, ["solo"])
        self.assertEqual(formats[0]["type"], "json_schema")
        self.assertEqual(formats[1], {"type": "json_object"})
        self.assertIsNone(formats[2])

    def test_accepts_sse_and_redacts_error_summaries(self):
        streamed = OpenAICompatibleTagger._decode_streaming_json(
            'data: {"choices":[{"delta":{"content":"{\\"tags\\":["}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"\\"solo\\"]}"}}]}\n\n'
            'data: [DONE]\n'
        )
        self.assertEqual(streamed["choices"][0]["message"]["content"],
                         '{"tags":["solo"]}')
        summary = _safe_summary(
            "Incorrect api_key=verysecretvalue auth_token=abc123")
        self.assertNotIn("verysecretvalue", summary)
        self.assertNotIn("abc123", summary)


class LocalONNXConfigTests(unittest.TestCase):
    def test_normalizes_config_without_importing_optional_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = LocalONNXTagger().normalize_cfg({
                "model_path": os.path.join(directory, "model.onnx"),
                "tags_path": os.path.join(directory, "selected_tags.csv"),
                "threshold": "0.4",
            })
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg["threshold"], 0.4)
        self.assertEqual(cfg["general_threshold"], 0.4)
        self.assertEqual(cfg["character_threshold"], 0.85)
        self.assertEqual(cfg["categories"], {"0", "4"})

    def test_uses_separate_general_and_character_thresholds(self):
        import numpy as np
        from PIL import Image

        class Input:
            name = "input"
            shape = [1, 3, 2, 2]

        class Session:
            def get_inputs(self):
                return [Input()]

            def run(self, _outputs, _feed):
                return [np.asarray([0.4, 0.8], dtype=np.float32)]

        tagger = LocalONNXTagger()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image_file.write(b"not decoded here")
            image_path = image_file.name
        try:
            cfg = {
                "model_path": "model.onnx", "tags_path": "tags.csv",
                "general_threshold": 0.35, "character_threshold": 0.85,
                "threshold": 0.35, "categories": {"0", "4"},
            }
            with mock.patch.object(tagger, "_load", return_value=(
                    np, Image, Session(),
                    [("general", "0"), ("character", "4")])):
                with mock.patch.object(
                        tagger, "_prepare_image",
                        return_value=np.zeros((1, 3, 2, 2), dtype=np.float32)):
                    result = tagger.tag(TagContext(image_path=image_path), cfg)
            self.assertEqual(result.tags, ["general"])
        finally:
            os.remove(image_path)

    def test_reads_wd14_selected_tags_csv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", encoding="utf-8",
                                         newline="", delete=False) as handle:
            handle.write("tag_id,name,category,count\n0,1girl,0,1\n1,alice,4,1\n")
            path = handle.name
        try:
            self.assertEqual(LocalONNXTagger._read_labels(path),
                             [("1girl", "0"), ("alice", "4")])
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
