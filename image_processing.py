# -*- coding: utf-8 -*-
"""Small, optional image transformations used by the downloader.

The downloader itself intentionally keeps Pillow as a lazy dependency.  The
solid-background feature calls :func:`apply_solid_background` only when the
user enables it, so existing non-image/video workflows remain lightweight.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Iterable, Tuple


HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ImageProcessingError(RuntimeError):
    """A user-facing image transformation failure."""


def normalize_hex_color(value: Any, default: str = "#ffffff") -> str:
    """Return a canonical ``#rrggbb`` value or raise ``ValueError``."""

    text = str(value or default).strip()
    if not HEX_COLOR_RE.fullmatch(text):
        raise ValueError("背景颜色必须是 #RRGGBB 或 #RGB 格式")
    if len(text) == 4:
        text = "#" + "".join(char * 2 for char in text[1:])
    return text.lower()


def _rgb_from_hex(value: str) -> Tuple[int, int, int]:
    value = normalize_hex_color(value)
    return tuple(int(value[offset:offset + 2], 16) for offset in (1, 3, 5))


def _has_transparent_pixels(image: Any) -> bool:
    """Check actual alpha values, including palette transparency metadata."""

    try:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        extrema = alpha.getextrema()
        return bool(extrema and extrema[0] < 255)
    except Exception as exc:  # pragma: no cover - Pillow-specific edge cases
        raise ImageProcessingError("无法读取图片透明通道：%s" % exc) from exc


def _flatten_frame(image: Any, background: Tuple[int, int, int], Image: Any) -> Any:
    rgba = image.convert("RGBA")
    canvas = Image.new("RGBA", rgba.size, background + (255,))
    canvas.alpha_composite(rgba)
    return canvas.convert("RGB")


def _metadata_kwargs(image: Any, output_format: str) -> dict:
    """Keep safe, broadly supported metadata without copying transparency."""

    if output_format.upper() in {"GIF", "WEBP"}:
        return {}
    info = getattr(image, "info", {}) or {}
    kwargs = {}
    for key in ("icc_profile", "exif", "dpi"):
        value = info.get(key)
        if value:
            kwargs[key] = value
    return kwargs


def _save_flattened(
        image: Any,
        frames: Iterable[Any],
        output_format: str,
        destination: str,
        metadata: dict,
) -> None:
    """Save one or more RGB frames while retaining common animation timing."""

    frame_list = list(frames)
    if not frame_list:
        raise ImageProcessingError("图片没有可写入的帧")
    first = frame_list[0]
    format_name = output_format.upper()
    if len(frame_list) == 1 or not getattr(image, "is_animated", False):
        first.save(destination, format=format_name, **metadata)
        return

    durations = []
    for index in range(len(frame_list)):
        try:
            image.seek(index)
            durations.append(image.info.get("duration", 0))
        except Exception:
            durations.append(0)
    save_kwargs = {
        "format": format_name,
        "save_all": True,
        "append_images": frame_list[1:],
        "duration": durations,
        "loop": int((getattr(image, "info", {}) or {}).get("loop", 0) or 0),
    }
    if format_name == "GIF":
        # Pillow accepts one disposal value for an animated GIF; using the
        # first frame's setting avoids passing a list that newer Pillow builds
        # reject while still preserving the common case.
        save_kwargs["disposal"] = int((getattr(image, "info", {}) or {}).get(
            "disposal", 0) or 0)
    first.save(destination, **save_kwargs)


def apply_solid_background(path: str, color: str = "#ffffff") -> bool:
    """Flatten transparent pixels in *path* onto a solid RGB background.

    The file is replaced atomically only when at least one pixel has an alpha
    value below 255.  The return value tells callers whether a transformation
    was applied.  Animated GIF/WebP/APNG files are flattened frame by frame;
    video and unsupported formats are left to the caller's normal error path.
    """

    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise ImageProcessingError(
            "透明图片底色功能需要 Pillow，请运行 pip install Pillow"
        ) from exc

    if not path or not os.path.isfile(path):
        raise ImageProcessingError("图片文件不存在：%s" % path)
    background = _rgb_from_hex(color)
    temp_path = ""
    try:
        with Image.open(path) as image:
            output_format = str(image.format or "").upper()
            if not output_format:
                output_format = {
                    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
                    ".webp": "WEBP", ".gif": "GIF", ".tif": "TIFF",
                    ".tiff": "TIFF", ".bmp": "BMP",
                }.get(os.path.splitext(path)[1].lower(), "")
            if not output_format:
                return False

            animated = bool(getattr(image, "is_animated", False)
                            and getattr(image, "n_frames", 1) > 1)
            if animated:
                source_frames = [frame.copy() for frame in ImageSequence.Iterator(image)]
            else:
                source_frames = [image]
            if not any(_has_transparent_pixels(frame) for frame in source_frames):
                return False

            flattened = [
                _flatten_frame(frame, background, Image) for frame in source_frames
            ]
            metadata = _metadata_kwargs(image, output_format)
            directory = os.path.dirname(os.path.abspath(path)) or os.curdir
            fd, temp_path = tempfile.mkstemp(
                prefix=".%s.background-" % os.path.basename(path),
                suffix=".part", dir=directory)
            os.close(fd)
            _save_flattened(image, flattened, output_format, temp_path, metadata)
            # Windows keeps the source handle open until the Image object is
            # explicitly closed; release it before replacing the file.
            image.close()
            os.replace(temp_path, path)
            temp_path = ""
            return True
    except ImageProcessingError:
        raise
    except Exception as exc:
        raise ImageProcessingError("添加图片底色失败：%s" % exc) from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


__all__ = ["HEX_COLOR_RE", "ImageProcessingError", "normalize_hex_color",
           "apply_solid_background"]
