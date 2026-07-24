# -*- coding: utf-8 -*-
"""Local WD14-style ONNX tagger with lazy optional dependencies."""
from __future__ import annotations

import csv
import os
import threading
from typing import Any, Dict, Mapping, Tuple, Union

from .base import TagContext, Tagger, TaggingError, TagResult


_CACHE = {}
_CACHE_LOCK = threading.Lock()


class LocalONNXTagger(Tagger):
    id = "local_onnx"
    label = "本地 WD14 ONNX"
    needs_model = True
    needs_network = False

    def normalize_cfg(self, body: Mapping[str, Any]) -> Union[Dict[str, Any], str]:
        model_path = os.path.abspath(os.path.expanduser(
            str(body.get("model_path") or "").strip()))
        tags_path = os.path.abspath(os.path.expanduser(
            str(body.get("tags_path") or body.get("selected_tags_path") or "").strip()))
        if not str(body.get("model_path") or "").strip():
            return "请填写 ONNX 模型路径"
        if not str(body.get("tags_path") or body.get("selected_tags_path") or "").strip():
            return "请填写 selected_tags.csv 路径"
        try:
            threshold = float(body.get("threshold", 0.35))
            input_size = int(body.get("input_size") or 448)
        except (TypeError, ValueError):
            return "ONNX threshold/input_size 参数不合法"
        if not 0 <= threshold <= 1:
            return "ONNX threshold 必须在 0 到 1 之间"
        if input_size <= 0:
            return "ONNX input_size 必须大于 0"
        providers = body.get("providers") or ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if isinstance(providers, str):
            providers = [item.strip() for item in providers.split(",") if item.strip()]
        categories = body.get("categories", ("0", "4"))
        if isinstance(categories, str):
            categories = [item.strip() for item in categories.split(",") if item.strip()]
        return {
            "model_path": model_path,
            "tags_path": tags_path,
            "threshold": threshold,
            "input_size": input_size,
            "providers": list(providers) or ["CPUExecutionProvider"],
            "categories": {str(item) for item in categories},
        }

    @staticmethod
    def _dependencies():
        try:
            import numpy as np
            import onnxruntime as ort
            from PIL import Image
        except ImportError as exc:
            raise TaggingError(
                "本地 ONNX 需要可选依赖: pip install onnxruntime Pillow numpy"
            ) from exc
        return np, ort, Image

    @staticmethod
    def _read_labels(path: str):
        labels = []
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "name" not in reader.fieldnames:
                raise TaggingError("selected_tags.csv 缺少 name 列")
            for row in reader:
                name = str(row.get("name") or "").strip()
                if name:
                    labels.append((name, str(row.get("category") or "")))
        if not labels:
            raise TaggingError("selected_tags.csv 中没有标签")
        return labels

    def _load(self, cfg: Mapping[str, Any]):
        model_path = str(cfg["model_path"])
        tags_path = str(cfg["tags_path"])
        if not os.path.isfile(model_path):
            raise TaggingError("ONNX 模型不存在: %s" % model_path)
        if not os.path.isfile(tags_path):
            raise TaggingError("selected_tags.csv 不存在: %s" % tags_path)
        np, ort, Image = self._dependencies()
        providers = tuple(cfg.get("providers") or ["CPUExecutionProvider"])
        cache_key = (model_path, os.path.getmtime(model_path), tags_path,
                     os.path.getmtime(tags_path), providers)
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None:
                return (np, Image) + cached
            try:
                session = ort.InferenceSession(model_path, providers=list(providers))
            except Exception as exc:
                raise TaggingError("加载 ONNX 模型失败: %s" % exc) from exc
            labels = self._read_labels(tags_path)
            value = (session, labels)
            _CACHE.clear()
            _CACHE[cache_key] = value
            return (np, Image) + value

    def test(self, cfg: Mapping[str, Any]):
        try:
            _, _, session, labels = self._load(cfg)
            return True, "模型可用，标签 %d 个，provider: %s" % (
                len(labels), ", ".join(session.get_providers()))
        except Exception as exc:
            return False, str(exc)

    def tag(self, context: TagContext, cfg: Mapping[str, Any]) -> TagResult:
        if not os.path.isfile(context.image_path):
            raise TaggingError("图片文件不存在: %s" % context.image_path)
        np, Image, session, labels = self._load(cfg)
        input_info = session.get_inputs()[0]
        tensor = self._prepare_image(
            context.image_path, input_info.shape, int(cfg.get("input_size", 448)), np, Image)
        try:
            outputs = session.run(None, {input_info.name: tensor})
        except Exception as exc:
            raise TaggingError("ONNX 推理失败: %s" % exc) from exc
        if not outputs:
            raise TaggingError("ONNX 模型没有输出")
        scores_array = np.asarray(outputs[0]).reshape(-1)
        threshold = float(cfg.get("threshold", 0.35))
        categories = {str(item) for item in cfg.get("categories", ("0", "4"))}
        matched = []
        for index, (name, category) in enumerate(labels[:len(scores_array)]):
            score = float(scores_array[index])
            if score >= threshold and (not categories or category in categories):
                matched.append((name, score))
        matched.sort(key=lambda item: item[1], reverse=True)
        return TagResult(
            tags=[item[0] for item in matched],
            scores={item[0]: item[1] for item in matched},
            raw=None,
            model=os.path.basename(str(cfg["model_path"])),
        )

    @staticmethod
    def _prepare_image(path: str, shape, fallback_size: int, np, Image):
        dims = list(shape or [])
        is_nchw = len(dims) == 4 and dims[1] in (1, 3)
        if is_nchw:
            height = dims[2] if isinstance(dims[2], int) and dims[2] > 0 else fallback_size
            width = dims[3] if isinstance(dims[3], int) and dims[3] > 0 else fallback_size
        else:
            height = dims[1] if len(dims) == 4 and isinstance(dims[1], int) and dims[1] > 0 else fallback_size
            width = dims[2] if len(dims) == 4 and isinstance(dims[2], int) and dims[2] > 0 else fallback_size

        with Image.open(path) as source:
            image = source.convert("RGB")
            side = max(image.size)
            canvas = Image.new("RGB", (side, side), (255, 255, 255))
            canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            canvas = canvas.resize((width, height), resampling)
            array = np.asarray(canvas, dtype=np.float32)
        # WD14 models are commonly exported from TensorFlow and expect BGR NHWC.
        array = array[:, :, ::-1]
        if is_nchw:
            array = np.transpose(array, (2, 0, 1))
        return np.expand_dims(array, axis=0).astype(np.float32, copy=False)
