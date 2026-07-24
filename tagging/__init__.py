# -*- coding: utf-8 -*-
"""Tagger registry and public tagging API."""
from dataclasses import replace
from typing import Mapping

from .base import (
    NoneTagger,
    TagContext,
    Tagger,
    TaggingError,
    TagResult,
    dedupe_tags,
    format_tags,
    merge_captions,
    split_tags,
)
from .local_onnx import LocalONNXTagger
from .openai_compatible import OpenAICompatibleTagger


TAGGER_REGISTRY = {
    "none": NoneTagger(),
    "openai_compatible": OpenAICompatibleTagger(),
    "local_onnx": LocalONNXTagger(),
}

TAGGER_ALIASES = {
    "none": "none",
    "openai": "openai_compatible",
    "openai_compatible": "openai_compatible",
    "onnx": "local_onnx",
    "local_onnx": "local_onnx",
}


def get_tagger(tagger_id):
    try:
        return TAGGER_REGISTRY[tagger_id]
    except KeyError as exc:
        raise TaggingError("未知 tagger: %s" % tagger_id) from exc


def list_taggers():
    return [
        {
            "id": tagger.id,
            "label": tagger.label,
            "needs_model": tagger.needs_model,
            "needs_network": tagger.needs_network,
        }
        for tagger in TAGGER_REGISTRY.values()
    ]


def normalize_tagger_config(body):
    """Normalize the public tagging configuration.

    Accepted selector keys are ``tagger_id`` or ``tagger``. Backend fields may
    be placed directly in *body* or under ``tagger_config``. The returned dict
    is suitable for :func:`create_tagger`; validation failures are returned as
    a user-facing string to match the downloader's existing config convention.
    """
    body = body or {}
    selector = (body.get("tagger_type") or body.get("tagger_id") or
                body.get("tagger") or "none")
    if isinstance(selector, Mapping):
        tagger_id = str(selector.get("type") or selector.get("id") or
                        selector.get("tagger_id") or "none")
        backend_body = dict(selector)
    else:
        tagger_id = str(selector)
        nested = body.get("tagger_config")
        backend_body = dict(nested) if isinstance(nested, Mapping) else dict(body)
    tagger_id = TAGGER_ALIASES.get(tagger_id, tagger_id)
    if tagger_id == "openai_compatible":
        aliases = {
            "llm_base_url": "endpoint",
            "llm_api_key": "api_key",
            "llm_model": "model",
            "llm_prompt": "prompt",
        }
    elif tagger_id == "local_onnx":
        aliases = {
            "onnx_model_path": "model_path",
            "onnx_tags_path": "tags_path",
            "onnx_threshold": "general_threshold",
            "onnx_general_threshold": "general_threshold",
            "onnx_character_threshold": "character_threshold",
        }
    else:
        aliases = {}
    for source_name, target_name in aliases.items():
        if target_name not in backend_body and source_name in body:
            backend_body[target_name] = body[source_name]
    try:
        backend = get_tagger(tagger_id)
    except TaggingError as exc:
        return str(exc)
    normalized = backend.normalize_cfg(backend_body)
    if isinstance(normalized, str):
        return normalized
    result = dict(normalized)
    result["tagger_id"] = tagger_id
    return result


class ConfiguredTagger:
    """A backend bound to validated config, used by the task runner."""

    def __init__(self, backend, cfg):
        self.backend = backend
        self.cfg = dict(cfg)
        self.id = backend.id
        self.label = backend.label

    def test(self):
        return self.backend.test(self.cfg)

    @staticmethod
    def _context(file_path, context):
        if isinstance(context, TagContext):
            return replace(context, image_path=file_path)
        values = dict(context or {})
        known = {
            "source_id", "post_id", "artist", "source_url", "native_tags", "metadata"
        }
        metadata = dict(values.get("metadata") or {})
        metadata.update({key: value for key, value in values.items() if key not in known})
        return TagContext(
            image_path=file_path,
            source_id=str(values.get("source_id") or ""),
            post_id=str(values.get("post_id") or ""),
            artist=str(values.get("artist") or ""),
            source_url=str(values.get("source_url") or ""),
            native_tags=values.get("native_tags") or [],
            metadata=metadata,
        )

    def tag_result(self, file_path, context=None):
        return self.backend.tag(self._context(file_path, context), self.cfg)

    def tag(self, file_path, context=None):
        """Tag one image and return only the stable ``list[str]`` surface."""
        return self.tag_result(file_path, context).tags


def create_tagger(cfg):
    """Create a configured object exposing ``test()`` and ``tag(path, ctx)``."""
    cfg = dict(cfg or {})
    tagger_id = str(cfg.pop("tagger_type",
                            cfg.pop("tagger_id", cfg.pop("tagger", "none"))))
    tagger_id = TAGGER_ALIASES.get(tagger_id, tagger_id)
    return ConfiguredTagger(get_tagger(tagger_id), cfg)


__all__ = [
    "TAGGER_REGISTRY", "TagContext", "Tagger", "TaggingError", "TagResult",
    "ConfiguredTagger", "create_tagger", "get_tagger", "list_taggers",
    "normalize_tagger_config", "dedupe_tags", "format_tags",
    "merge_captions", "split_tags",
]
