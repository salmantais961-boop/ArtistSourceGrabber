# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import app
from image_processing import apply_solid_background, normalize_hex_color


class ImageProcessingTests(unittest.TestCase):
    def test_normalize_hex_color_accepts_short_and_long_forms(self):
        self.assertEqual(normalize_hex_color("#AbC"), "#aabbcc")
        self.assertEqual(normalize_hex_color("#102030"), "#102030")
        with self.assertRaises(ValueError):
            normalize_hex_color("red")

    def test_transparent_png_is_flattened_to_rgb(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transparent.png"
            image = Image.new("RGBA", (2, 2), (255, 0, 0, 0))
            image.putpixel((0, 0), (255, 0, 0, 255))
            image.save(path)

            self.assertTrue(apply_solid_background(str(path), "#123456"))
            with Image.open(path) as flattened:
                self.assertEqual(flattened.mode, "RGB")
                self.assertEqual(flattened.getpixel((0, 0)), (255, 0, 0))
                self.assertEqual(flattened.getpixel((1, 1)), (18, 52, 86))

    def test_opaque_image_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opaque.jpg"
            Image.new("RGB", (2, 2), (1, 2, 3)).save(path)
            before = path.read_bytes()
            self.assertFalse(apply_solid_background(str(path), "#ffffff"))
            self.assertEqual(path.read_bytes(), before)

    def test_background_config_is_added_to_normalized_task(self):
        class Source:
            id = "fake"
            label = "Fake"
            needs_auth = False

            def normalize_cfg(self, body):
                return {"artist": body.get("artist", "")}

        with mock.patch.object(app, "get_source", return_value=Source()):
            runs, _, error = app.normalize_task_configs({
                "source": "fake", "artist": "demo", "background_enabled": True,
                "background_color": "#abc", "start_page": 2, "end_page": 4,
                "tagger_type": "none",
            })
        self.assertIsNone(error)
        self.assertEqual(runs[0]["cfg"]["background_color"], "#aabbcc")
        self.assertTrue(runs[0]["cfg"]["background_enabled"])
        self.assertEqual(runs[0]["cfg"]["start_page"], 2)
        self.assertEqual(runs[0]["cfg"]["end_page"], 4)


if __name__ == "__main__":
    unittest.main()
