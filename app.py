#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-source artist media grabber with canonical Danbooru artist mapping."""

import json
import hashlib
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_util import build_opener, describe_error
from sources import get_source, list_sources
from tagging import create_tagger, normalize_tagger_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
HOST = "127.0.0.1"
PORT = 8710
LIST_INTERVAL = 0.55
FILE_INTERVAL = 0.25
MAX_PAGES = 1000

MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".mp4": "video/mp4", ".webm": "video/webm", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
}


class Task:
    def __init__(self, source, cfg=None, tagger=None):
        if isinstance(source, (list, tuple)):
            entries = list(source)
        else:
            entries = [{"source": source, "cfg": cfg, "skip": False}]
        self.runs = []
        for entry in entries:
            if isinstance(entry, tuple):
                entry = {"source": entry[0], "cfg": entry[1], "skip": False}
            run_source = entry["source"]
            run_cfg = entry.get("cfg") or {}
            self.runs.append({
                "source": run_source,
                "cfg": run_cfg,
                "skip": bool(entry.get("skip")),
                "skip_reason": str(entry.get("skip_reason") or "已跳过"),
                "state": {
                    "id": run_source.id, "label": run_source.label,
                    "artist": str(run_cfg.get("artist") or ""),
                    "artist_key": "", "status": "pending", "error": "",
                    "site_total": -1, "target": int(run_cfg.get("count") or 0),
                    "done": 0, "skipped": 0, "failed": 0,
                },
            })
        self.source = self.runs[0]["source"]
        self.cfg = self.runs[0]["cfg"]
        self.tagger = tagger
        self.lock = threading.Lock()
        self.status = "preparing"
        self.error = ""
        self.artist_input = self.cfg.get("artist") or ""
        self.artist_key = ""
        self.canonical_artist = self.cfg.get("canonical_artist") or ""
        self.folder = ""
        self.site_total = -1
        self.target = sum(run["state"]["target"] for run in self.runs)
        self.done = 0
        self.skipped = 0
        self.failed = 0
        self.logs = []
        self.items = []
        self.stop_flag = False
        self.hashes = {}
        self.active_source_id = ""

    def log(self, message):
        with self.lock:
            self.logs.append("[%s] %s" % (time.strftime("%H:%M:%S"), message))

    def add_item(self, item):
        with self.lock:
            self.items.append(item)

    def update_item(self, source_id, post_id, values, filename=""):
        """Update a previously emitted item without exposing internal paths."""
        with self.lock:
            for item in reversed(self.items):
                if str(item.get("source") or "") != str(source_id or ""):
                    continue
                if str(item.get("id") or "") != str(post_id or ""):
                    continue
                if filename and item.get("filename") != filename:
                    continue
                item.update(values)
                return True
        return False

    def snapshot(self, log_offset=0, item_offset=0):
        with self.lock:
            states = [dict(run["state"]) for run in self.runs]
            totals = [state["site_total"] for state in states]
            site_total = sum(totals) if totals and all(total >= 0 for total in totals) else -1
            return {
                "status": self.status,
                "error": self.error,
                "source": self.source.id,
                "source_label": self.source.label,
                "artist": self.artist_key or self.artist_input,
                "canonical_artist": self.canonical_artist,
                "folder": self.folder,
                "site_total": site_total,
                "target": self.target,
                "done": self.done,
                "skipped": self.skipped,
                "failed": self.failed,
                "logs": self.logs[log_offset:],
                "log_count": len(self.logs),
                "items": [_safe_task_item(item) for item in self.items[item_offset:]],
                "item_count": len(self.items),
                "active_source": self.active_source_id,
                "sources": states,
            }

    def increment(self, state, field, amount=1):
        with self.lock:
            state[field] += amount
            setattr(self, field, getattr(self, field) + amount)


def sanitize_name(name):
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", str(name or "")).strip(". ")
    return cleaned[:120] or "artist"


def safe_filename(name):
    name = os.path.basename(str(name or ""))
    name = re.sub(r'[^0-9A-Za-z._()\-]+', "_", name).strip(". ")
    return name[:180] or "media.bin"


def _split_caption(caption, tag_format):
    caption = str(caption or "").strip()
    if not caption:
        return []
    if tag_format == "comma":
        return [part.strip() for part in caption.split(",") if part.strip()]
    return [part.strip() for part in caption.split() if part.strip()]


def _format_tags(tags, tag_format):
    out = []
    seen = set()
    for tag in tags:
        tag = str(tag or "").strip().strip(",")
        tag = tag.replace("\\(", "(").replace("\\)", ")")
        if not tag:
            continue
        key = re.sub(r"[ _]+", "_", tag).casefold()
        if key in seen:
            continue
        seen.add(key)
        if tag_format == "comma":
            tag = tag.replace("_", " ").replace("(", "\\(").replace(")", "\\)")
        else:
            tag = tag.replace(" ", "_")
        out.append(tag)
    return (", " if tag_format == "comma" else " ").join(out)


TASK_ITEM_FIELDS = {
    "id", "source", "url", "ext", "status", "duplicate_of", "filename",
    "preview_url", "image_url", "native_tags", "generated_tags", "final_tags",
    "tag_merge_mode", "tagger_id", "tag_status", "tag_error", "caption_url",
}


def _safe_item_tags(tags, limit=500):
    """Normalize tag lists for JSON progress responses without raw metadata."""
    if isinstance(tags, str):
        values = [tags]
    elif isinstance(tags, (list, tuple, set)):
        values = tags
    else:
        values = []
    out = []
    for value in values:
        if not isinstance(value, (str, int, float)):
            continue
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
        if text:
            out.append(text[:200])
        if len(out) >= limit:
            break
    return out


def _safe_item_error(value):
    """Keep useful diagnostics while removing credentials and image payloads."""
    text = str(value or "")
    text = re.sub(r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+",
                  "[image data omitted]", text, flags=re.I)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
                  "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted key]", text)
    text = re.sub(
        r"(?i)((?:api[_ -]?key|authorization|auth_token|ct0|phpsessid)\s*[=:]\s*)"
        r"(?:[\"']?)[^\s,;\"'}]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _safe_task_item(item):
    """Whitelist the item payload returned by ``/api/progress``."""
    source = item if isinstance(item, dict) else {}
    safe = {key: source[key] for key in TASK_ITEM_FIELDS if key in source}
    for key in ("id", "source", "url", "ext", "status", "duplicate_of",
                "filename", "preview_url", "image_url", "tag_merge_mode",
                "tagger_id", "tag_status", "caption_url"):
        if key in safe:
            safe[key] = str(safe[key] or "")
    for key in ("native_tags", "generated_tags", "final_tags"):
        safe[key] = _safe_item_tags(safe.get(key))
    safe["tag_error"] = _safe_item_error(safe.get("tag_error"))
    return safe


def _task_file_url(task, filename):
    if not filename:
        return ""
    return "/files/%s/%s" % (
        urllib.parse.quote(task.folder), urllib.parse.quote(filename))


def _caption_filename(filename):
    return os.path.splitext(filename)[0] + ".txt" if filename else ""


def _task_tagger_id(task):
    if task.tagger is None:
        return "none"
    return str(getattr(task.tagger, "id", "") or "unknown")


def _task_item(task, source, cfg, pid, filename="", image_url="", **values):
    """Build the common, secret-free per-post progress item."""
    caption_url = _task_file_url(task, _caption_filename(filename)) if filename else ""
    item = {
        "id": str(pid or ""),
        "source": source.id,
        "url": image_url,
        "preview_url": image_url,
        "image_url": image_url,
        "ext": os.path.splitext(filename)[1].lstrip(".").lower() if filename else "",
        "status": "",
        "filename": filename,
        "native_tags": [],
        "generated_tags": [],
        "final_tags": [],
        "tag_merge_mode": str(cfg.get("tag_merge_mode") or "native_only"),
        "tagger_id": _task_tagger_id(task),
        "tag_status": "",
        "tag_error": "",
        "caption_url": caption_url,
    }
    item.update(values)
    return _safe_task_item(item)


def _best_effort_native_caption(source, post, cfg):
    try:
        return str(source.build_caption(post, cfg) or "")
    except Exception:
        return ""


def _best_effort_filename(source, post, cfg):
    try:
        source_filename = safe_filename(source.make_filename(post, cfg))
        return safe_filename("%s__%s" % (source.id, source_filename))
    except Exception:
        return ""


def _read_caption_tags(path, tag_format):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _split_caption(fh.read(), tag_format)
    except OSError:
        return []


def _write_text_atomic(path, content):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _merge_caption_file(path, tags, tag_format):
    existing = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = fh.read()
    except OSError:
        pass
    merged = _split_caption(existing, tag_format)
    merged.extend(tags)
    _write_text_atomic(path, _format_tags(merged, tag_format))


def _download(url, path, cfg, headers=None):
    request_headers = {"User-Agent": "MultiSourceArtistGrabber/2.0"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, headers=request_headers)
    tmp = path + ".part"
    try:
        with build_opener(cfg.get("proxy")).open(req, timeout=180) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _tag_context(task, post, native_caption, source=None, cfg=None):
    source = source or task.source
    cfg = cfg or task.cfg
    raw = post.get("raw") or {}
    return {
        "source_id": source.id,
        "artist": task.canonical_artist or post.get("artist") or task.artist_input,
        "post_id": str(post.get("id") or ""),
        "native_tags": _split_caption(native_caption, cfg.get("tag_format", "comma")),
        "text": raw.get("full_text") or raw.get("text") or raw.get("title") or "",
        "metadata": raw,
    }


TAG_MERGE_MODES = {"tagger_only", "native_plus_tagger", "native_only"}


def _normalize_tag_merge_mode(value, tagger_cfg=None):
    """Return an explicit, source-independent caption merge policy."""
    value = str(value or "").strip().lower()
    if value in TAG_MERGE_MODES:
        return value
    tagger_id = str((tagger_cfg or {}).get("tagger_id") or "none")
    return "native_only" if tagger_id == "none" else "tagger_only"


def _caption_base_tags(task, native_caption, cfg):
    mode = cfg.get("tag_merge_mode") or "native_only"
    tags = []
    if mode in {"native_only", "native_plus_tagger"}:
        tags.extend(_split_caption(native_caption, cfg.get("tag_format", "comma")))
    if cfg.get("include_artist") and task.canonical_artist:
        tags.insert(0, task.canonical_artist)
    return tags


def process_post(task, post, target_dir, run=None):
    run = run or task.runs[0]
    source = run["source"]
    state = run["state"]
    cfg = run["cfg"]
    pid = str(post.get("id") or "")
    if not pid:
        return False
    filename = ""
    ext = str(post.get("ext") or "").lower()
    file_path = ""
    txt_path = ""
    rel_url = ""
    native_caption = ""
    native_tags = []
    generated_tags = []
    tag_error = ""
    try:
        if source.skip_post(post, cfg):
            native_caption = _best_effort_native_caption(source, post, cfg)
            native_tags = _split_caption(native_caption, cfg.get("tag_format", "comma"))
            filename = _best_effort_filename(source, post, cfg)
            task.increment(state, "skipped")
            task.log("[%s] #%s 跳过（视频/动图或来源过滤）" % (source.label, pid))
            task.add_item(_task_item(
                task, source, cfg, pid, filename=filename, status="skipped",
                native_tags=native_tags, tag_status="skipped", caption_url=""))
            return False

        url = post.get("file_url") or post.get("large_url")
        if not url:
            native_caption = _best_effort_native_caption(source, post, cfg)
            native_tags = _split_caption(native_caption, cfg.get("tag_format", "comma"))
            filename = _best_effort_filename(source, post, cfg)
            task.increment(state, "skipped")
            task.log("[%s] #%s 跳过（无可下载文件 URL）" % (source.label, pid))
            task.add_item(_task_item(
                task, source, cfg, pid, filename=filename, status="skipped",
                native_tags=native_tags, tag_status="skipped", caption_url=""))
            return False

        filename = _best_effort_filename(source, post, cfg)
        if not filename:
            raise RuntimeError("无法生成安全文件名")
        ext = os.path.splitext(filename)[1].lstrip(".").lower() or ext
        file_path = os.path.join(target_dir, filename)
        txt_path = os.path.splitext(file_path)[0] + ".txt"
        rel_url = _task_file_url(task, filename)
        existed = os.path.exists(file_path)
        if not existed:
            _download(url, file_path, cfg, post.get("extra_headers"))

        digest = _sha256_file(file_path)
        native_caption = source.build_caption(post, cfg)
        native_tags = _split_caption(native_caption, cfg.get("tag_format", "comma"))
        tags = _caption_base_tags(task, native_caption, cfg)
        duplicate = task.hashes.get(digest)
        if duplicate and os.path.abspath(duplicate["file_path"]) != os.path.abspath(file_path):
            own_txt = os.path.splitext(file_path)[0] + ".txt"
            if cfg.get("tag_merge_mode") != "tagger_only":
                try:
                    with open(own_txt, "r", encoding="utf-8") as fh:
                        tags.extend(_split_caption(fh.read(), cfg.get("tag_format", "comma")))
                except OSError:
                    pass
            _merge_caption_file(duplicate["txt_path"], tags, cfg.get("tag_format", "comma"))
            try:
                os.remove(file_path)
            except OSError:
                pass
            try:
                os.remove(own_txt)
            except OSError:
                pass
            final_tags = _read_caption_tags(
                duplicate["txt_path"], cfg.get("tag_format", "comma"))
            task.update_item(
                duplicate["source"], duplicate["id"],
                {"final_tags": final_tags,
                 "caption_url": _task_file_url(task, _caption_filename(duplicate["filename"]))},
                filename=duplicate["filename"],
            )
            task.increment(state, "skipped")
            task.log("[%s] #%s 与 %s 内容相同，已合并 txt 标签" % (
                source.label, pid, duplicate["filename"]))
            task.add_item(_task_item(
                task, source, cfg, pid, filename=duplicate["filename"],
                image_url=duplicate["rel_url"], ext=duplicate["ext"],
                status="duplicate", duplicate_of=duplicate["id"],
                native_tags=native_tags, generated_tags=[], final_tags=final_tags,
                tag_status="merged"))
            return False

        task.hashes[digest] = {
            "file_path": file_path, "txt_path": txt_path, "filename": filename,
            "rel_url": rel_url, "ext": ext, "id": pid, "source": source.id,
        }
        tag_status = "native"
        if task.tagger is not None and cfg.get("tag_merge_mode") != "native_only":
            try:
                generated = task.tagger.tag(
                    file_path, _tag_context(task, post, native_caption, source, cfg))
                if generated:
                    generated_tags = _safe_item_tags(generated)
                    tags.extend(generated)
                    tag_status = "generated"
            except Exception as exc:
                tag_error = _safe_item_error(describe_error(exc))
                task.log("[%s] #%s AI 打标失败，保留来源标签：%s" % (
                    source.label, pid, tag_error))
                tag_status = "failed"
        final_caption = _format_tags(tags, cfg.get("tag_format", "comma"))
        _write_text_atomic(txt_path, final_caption)
        final_tags = _split_caption(final_caption, cfg.get("tag_format", "comma"))
        task.increment(state, "done")
        task.add_item(_task_item(
            task, source, cfg, pid, filename=filename, image_url=rel_url, ext=ext,
            status="exists" if existed else "ok", native_tags=native_tags,
            generated_tags=generated_tags, final_tags=final_tags,
            tag_status=tag_status, tag_error=tag_error))
        return True
    except Exception as exc:
        error = _safe_item_error(describe_error(exc))
        task.increment(state, "failed")
        task.log("[%s] #%s 处理失败：%s" % (source.label, pid, error))
        if not native_tags:
            native_caption = _best_effort_native_caption(source, post, cfg)
            native_tags = _split_caption(native_caption, cfg.get("tag_format", "comma"))
        image_url = rel_url if file_path and os.path.isfile(file_path) else ""
        caption_url = (_task_file_url(task, _caption_filename(filename))
                       if txt_path and os.path.isfile(txt_path) else "")
        task.add_item(_task_item(
            task, source, cfg, pid, filename=filename, image_url=image_url,
            ext=ext, status="failed", native_tags=native_tags,
            generated_tags=generated_tags, final_tags=_read_caption_tags(
                txt_path, cfg.get("tag_format", "comma")) if txt_path else [],
            tag_status="failed", tag_error=tag_error or error,
            caption_url=caption_url))
        return False


def _run_source(task, run, target_dir):
    cfg = run["cfg"]
    source = run["source"]
    state = run["state"]
    try:
        with task.lock:
            task.source = source
            task.cfg = cfg
            task.artist_input = cfg.get("artist") or ""
            task.artist_key = ""
            task.active_source_id = source.id
            state["status"] = "testing"
        ok, message = source.test(cfg)
        if not ok:
            raise RuntimeError(message)
        task.log("[%s] 来源连接：%s" % (source.label, message))

        artist_key = source.resolve_artist(cfg, task.log)
        with task.lock:
            task.artist_key = str(artist_key)
            state["artist_key"] = str(artist_key)
        total = source.count_posts(artist_key, cfg)
        with task.lock:
            state["site_total"] = int(total) if total is not None else -1
        if total == 0:
            raise RuntimeError("该来源没有找到作品，请重新确认画师候选或 X 账号")
        if cfg.get("count", 0) > 0:
            target = cfg["count"]
            if total and total > 0:
                target = min(target, total)
        else:
            target = total if total and total > 0 else 0
        with task.lock:
            previous_target = state["target"]
            state["target"] = int(target)
            task.target += int(target) - previous_target
            state["status"] = "running"
        if total and total > 0:
            task.log("[%s] 站内约 %s 个作品，本次目标 %s" % (
                source.label, total, target or "全部"))
        else:
            task.log("[%s] 不提供可靠总数，将持续到来源分页结束" % source.label)

        seen = set()
        page = 1
        while not task.stop_flag and page <= MAX_PAGES:
            if target and state["done"] >= target:
                break
            posts = source.list_posts(artist_key, page, cfg)
            if not posts:
                break
            new_posts = 0
            for post in posts:
                post_key = "%s:%s" % (source.id, post.get("id"))
                if post_key in seen:
                    continue
                seen.add(post_key)
                new_posts += 1
                if task.stop_flag or (target and state["done"] >= target):
                    break
                process_post(task, post, target_dir, run)
                time.sleep(FILE_INTERVAL)
            if new_posts == 0:
                task.log("[%s] 分页未返回新作品，停止以避免循环" % source.label)
                break
            page += 1
            time.sleep(LIST_INTERVAL)

        with task.lock:
            if state["target"] <= 0:
                task.target += state["done"] - state["target"]
                state["target"] = state["done"]
            state["status"] = "stopped" if task.stop_flag else "done"
        task.log("[%s] 来源结束：成功 %d，跳过 %d，失败 %d" % (
            source.label, state["done"], state["skipped"], state["failed"]))
        return True
    except Exception as exc:
        msg = describe_error(exc)
        with task.lock:
            state["status"] = "error"
            state["error"] = msg
        task.increment(state, "failed")
        with task.lock:
            processed = state["done"] + state["skipped"] + state["failed"]
            task.target += processed - state["target"]
            state["target"] = processed
        task.log("[%s] 来源失败，继续后续来源：%s" % (source.label, msg))
        return False


def run_task(task):
    folder_artist = task.canonical_artist or task.artist_input or "artist"
    folder = sanitize_name(folder_artist)
    target_dir = os.path.join(DOWNLOAD_DIR, folder)
    os.makedirs(target_dir, exist_ok=True)
    with task.lock:
        task.folder = folder
        task.status = "running"
    try:
        if task.tagger is not None:
            ok, message = task.tagger.test()
            if not ok:
                raise RuntimeError("打标器不可用：%s" % message)
            task.log("打标器：%s" % message)

        completed = 0
        attempted = 0
        for run in task.runs:
            if task.stop_flag:
                break
            state = run["state"]
            if run["skip"]:
                with task.lock:
                    state["status"] = "skipped"
                    task.target -= state["target"]
                    state["target"] = 0
                task.log("[%s] %s" % (run["source"].label, run["skip_reason"]))
                continue
            attempted += 1
            if _run_source(task, run, target_dir):
                completed += 1

        with task.lock:
            task.active_source_id = ""
            if task.stop_flag:
                task.status = "stopped"
            elif attempted and completed == 0:
                task.status = "error"
                task.error = "所有已启用来源均失败"
            else:
                task.status = "done"
        task.log("混合任务结束：成功 %d，跳过 %d，失败 %d" % (
            task.done, task.skipped, task.failed))
        task.log("保存目录：%s" % target_dir)
    except Exception as exc:
        msg = describe_error(exc)
        with task.lock:
            task.status = "error"
            task.error = msg
        task.log("任务出错：%s" % msg)


def normalize_config(body):
    source_id = str(body.get("source") or "danbooru").strip().lower()
    source = get_source(source_id)
    if source is None:
        return None, None, "不支持的数据来源：%s" % source_id
    source_body = dict(body)
    if source_id == "twitter":
        x_user_id = str(body.get("x_user_id") or "").strip()
        if x_user_id.isdigit():
            source_body["artist"] = "id:" + x_user_id
        elif body.get("x_handle"):
            source_body["artist"] = str(body.get("x_handle")).lstrip("@")
    cfg = source.normalize_cfg(source_body)
    if isinstance(cfg, str):
        return None, None, cfg
    cfg["source"] = source_id
    cfg["canonical_artist"] = str(body.get("canonical_artist") or "").strip()
    cfg["canonical_artist_id"] = str(body.get("canonical_artist_id") or "").strip()
    cfg["query_type"] = str(body.get("query_type") or "artist")
    tagger_cfg = normalize_tagger_config(body)
    if isinstance(tagger_cfg, str):
        return None, None, tagger_cfg
    cfg["tagger"] = tagger_cfg
    cfg["tag_merge_mode"] = _normalize_tag_merge_mode(
        body.get("tag_merge_mode"), tagger_cfg)
    return source, cfg, None


def normalize_task_configs(body):
    """Normalize both the legacy single-source and the mixed-source payloads."""
    selected = body.get("sources")
    if not isinstance(selected, list) or not selected:
        source, cfg, error = normalize_config(body)
        if error:
            return None, None, error
        return [{"source": source, "cfg": cfg, "skip": False}], cfg.get("tagger"), None

    source_configs = body.get("source_configs")
    if not isinstance(source_configs, dict):
        source_configs = {}
    runs = []
    seen = set()
    tagger_cfg = normalize_tagger_config(body)
    if isinstance(tagger_cfg, str):
        return None, None, tagger_cfg
    for selected_entry in selected:
        entry_cfg = selected_entry if isinstance(selected_entry, dict) else {}
        source_id = str(entry_cfg.get("id") or entry_cfg.get("source") or selected_entry).strip().lower()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        source = get_source(source_id)
        if source is None:
            return None, None, "不支持的数据来源：%s" % source_id
        override = source_configs.get(source_id)
        if not isinstance(override, dict):
            override = {}
        merged = dict(body)
        merged.pop("sources", None)
        merged.pop("source_configs", None)
        merged.update(entry_cfg)
        merged.update(override)
        merged["source"] = source_id
        if merged.get("skip") or merged.get("enabled") is False:
            cfg = {
                "source": source_id,
                "artist": str(merged.get("artist") or ""),
                "canonical_artist": str(body.get("canonical_artist") or "").strip(),
                "canonical_artist_id": str(body.get("canonical_artist_id") or "").strip(),
                "query_type": str(body.get("query_type") or "artist"),
                "count": int(body.get("count") or 0),
                "tag_format": str(body.get("tag_format") or "comma"),
                "tagger": tagger_cfg,
                "tag_merge_mode": _normalize_tag_merge_mode(
                    body.get("tag_merge_mode"), tagger_cfg),
            }
            runs.append({"source": source, "cfg": cfg, "skip": True,
                         "skip_reason": str(merged.get("skip_reason") or "未配置认证或画师映射，已跳过")})
            continue
        normalized_source, cfg, error = normalize_config(merged)
        if error:
            return None, None, "%s：%s" % (source.label, error)
        cfg["tagger"] = tagger_cfg
        runs.append({"source": normalized_source, "cfg": cfg, "skip": False})
    if not runs:
        return None, None, "请至少选择一个数据来源"
    return runs, tagger_cfg, None


def source_config_for_request(source, body, artist=None):
    payload = dict(body or {})
    payload["artist"] = artist or payload.get("artist") or "test"
    payload.setdefault("count", 1)
    payload.setdefault("tag_format", "comma")
    cfg = source.normalize_cfg(payload)
    return cfg


def extract_x_handles(urls):
    out = []
    for value in urls or []:
        if isinstance(value, dict):
            if value.get("is_active") is False:
                continue
            value = value.get("url") or ""
        try:
            parsed = urllib.parse.urlparse(str(value))
        except Exception:
            continue
        host = parsed.netloc.casefold().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host not in ("x.com", "twitter.com", "mobile.twitter.com"):
            continue
        handle = parsed.path.strip("/").split("/", 1)[0].lstrip("@")
        if handle and handle.casefold() not in ("home", "intent", "share", "i") and handle not in out:
            out.append(handle)
    return out


def extract_x_user_ids(urls):
    out = []
    for value in urls or []:
        if isinstance(value, dict):
            if value.get("is_active") is False:
                continue
            value = value.get("url") or ""
        try:
            parsed = urllib.parse.urlparse(str(value))
        except Exception:
            continue
        host = parsed.netloc.casefold().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host not in ("x.com", "twitter.com", "mobile.twitter.com"):
            continue
        match = re.fullmatch(r"/i/user/(\d+)/?", parsed.path)
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def extract_pixiv_user_ids(urls):
    """Extract stable numeric Pixiv user IDs from Danbooru artist URLs."""
    out = []
    for value in urls or []:
        if isinstance(value, dict):
            if value.get("is_active") is False:
                continue
            value = value.get("url") or ""
        try:
            parsed = urllib.parse.urlparse(str(value))
        except Exception:
            continue
        host = parsed.netloc.casefold().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host != "pixiv.net":
            continue
        match = re.fullmatch(r"/(?:[a-z]{2}/)?users/(\d+)/?", parsed.path)
        if not match:
            match = re.fullmatch(r"/fanbox/creator/(\d+)/?", parsed.path)
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


TASK_LOCK = threading.Lock()
CURRENT_TASK = None
LAST_SOURCE_TEST = {"status": "none"}


def _x_browser_session_module():
    """Load the managed X browser lazily so other sources still work without it."""
    from sources import x_browser_session
    return x_browser_session


def _safe_x_session_result(result, default_message=""):
    """Whitelist session state returned to the UI; never expose cookie data or paths."""
    source = result if isinstance(result, dict) else {}
    payload = {
        "ok": bool(source.get("ok")),
        "running": bool(source.get("running")),
        "logged_in": bool(source.get("logged_in")),
        "message": str(source.get("message") or default_message),
    }
    if source.get("error"):
        payload["error"] = str(source.get("error"))
    return payload


def open_x_browser_session(artist=""):
    raw = str(artist or "").strip().lstrip("@")
    url = "https://x.com/%s/media" % raw if re.fullmatch(
        r"[A-Za-z0-9_]{1,15}", raw) else "https://x.com/home"
    result = _x_browser_session_module().open_login_window(url=url)
    return _safe_x_session_result(result, "专用 X 登录窗口已打开")


def check_x_browser_session():
    result = _x_browser_session_module().check_login()
    return _safe_x_session_result(result, "已检查专用 X 登录状态")


def _pixiv_browser_session_module():
    """Load the managed Pixiv browser lazily so other sources remain independent."""
    from sources import pixiv_browser_session
    return pixiv_browser_session


def open_pixiv_browser_session():
    result = _pixiv_browser_session_module().open_login_window()
    return _safe_x_session_result(result, "专用 Pixiv 登录窗口已打开")


def check_pixiv_browser_session():
    result = _pixiv_browser_session_module().check_login()
    return _safe_x_session_result(result, "已检查专用 Pixiv 登录状态")


def close_managed_browser_sessions():
    """Best-effort cleanup without returning or logging credential-related state."""
    for loader in (_x_browser_session_module, _pixiv_browser_session_module):
        try:
            loader().close()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "MultiSourceArtistGrabber/2.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, payload, code=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        if not path or not os.path.isfile(path):
            self.send_json({"error": "not found"}, 404)
            return
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME_TYPES.get(os.path.splitext(path)[1].lower(),
                                                       "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return {}

    @staticmethod
    def safe_join(root, rel):
        path = os.path.abspath(os.path.normpath(os.path.join(root, rel)))
        root = os.path.abspath(root)
        try:
            return path if os.path.commonpath([path, root]) == root else None
        except ValueError:
            return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_file(os.path.join(STATIC_DIR, "index.html"))
        elif path == "/api/sources":
            self.send_json({"sources": list_sources()})
        elif path == "/api/progress":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                lo = int(qs.get("logs", ["0"])[0]); io = int(qs.get("items", ["0"])[0])
            except ValueError:
                lo = io = 0
            task = CURRENT_TASK
            self.send_json(task.snapshot(lo, io) if task else {"status": "idle"})
        elif path == "/api/source/test-result":
            self.send_json(LAST_SOURCE_TEST)
        elif path.startswith("/static/"):
            self.send_file(self.safe_join(STATIC_DIR,
                                         urllib.parse.unquote(path[len("/static/"):])))
        elif path.startswith("/files/"):
            self.send_file(self.safe_join(DOWNLOAD_DIR,
                                         urllib.parse.unquote(path[len("/files/"):])))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        global CURRENT_TASK, LAST_SOURCE_TEST
        path = urllib.parse.urlparse(self.path).path
        body = self.read_json()

        if path == "/api/start":
            runs, tagger_cfg, error = normalize_task_configs(body)
            if error:
                self.send_json({"ok": False, "error": error}, 400)
                return
            try:
                tagger = create_tagger(tagger_cfg)
            except Exception as exc:
                self.send_json({"ok": False, "error": describe_error(exc)}, 400)
                return
            with TASK_LOCK:
                if CURRENT_TASK is not None and CURRENT_TASK.status in ("preparing", "running"):
                    self.send_json({"ok": False, "error": "已有任务在运行，请先停止"}, 409)
                    return
                CURRENT_TASK = Task(runs, tagger=tagger)
                task = CURRENT_TASK
            threading.Thread(target=run_task, args=(task,), daemon=True).start()
            self.send_json({"ok": True})

        elif path == "/api/stop":
            task = CURRENT_TASK
            if task and task.status in ("preparing", "running"):
                task.stop_flag = True
                task.log("收到停止指令，正在收尾…")
            self.send_json({"ok": True})

        elif path == "/api/shutdown":
            task = CURRENT_TASK
            if task and task.status in ("preparing", "running"):
                task.stop_flag = True
                task.log("收到关闭程序指令，正在停止任务…")
            close_managed_browser_sessions()
            self.send_json({"ok": True, "message": "后台已关闭，可关闭页面"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        elif path == "/api/source/test":
            source = get_source(body.get("source"))
            if source is None:
                self.send_json({"ok": False, "error": "未知来源"}, 400)
                return
            cfg = source_config_for_request(source, body)
            if isinstance(cfg, str):
                self.send_json({"ok": False, "error": cfg}, 400)
                return
            try:
                ok, message = source.test(cfg)
                LAST_SOURCE_TEST = {
                    "status": "done", "source": source.id, "ok": bool(ok),
                    "message": message if ok else "", "error": "" if ok else message,
                    "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.send_json({"ok": ok, "message": message} if ok else {"ok": False, "error": message})
            except Exception as exc:
                error = describe_error(exc)
                LAST_SOURCE_TEST = {
                    "status": "done", "source": source.id, "ok": False,
                    "message": "", "error": error,
                    "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.send_json({"ok": False, "error": error}, 500)

        elif path == "/api/x/session/open":
            try:
                result = open_x_browser_session(body.get("artist"))
                self.send_json(result, 200 if result.get("ok") else 500)
            except Exception as exc:
                self.send_json({"ok": False, "running": False, "logged_in": False,
                                "error": describe_error(exc)}, 500)

        elif path == "/api/x/session/check":
            try:
                result = check_x_browser_session()
                self.send_json(result, 200 if result.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "running": False, "logged_in": False,
                                "error": describe_error(exc)}, 500)

        elif path == "/api/pixiv/session/open":
            try:
                result = open_pixiv_browser_session()
                self.send_json(result, 200 if result.get("ok") else 500)
            except Exception as exc:
                self.send_json({"ok": False, "running": False, "logged_in": False,
                                "error": describe_error(exc)}, 500)

        elif path == "/api/pixiv/session/check":
            try:
                result = check_pixiv_browser_session()
                self.send_json(result, 200 if result.get("ok") else 400)
            except Exception as exc:
                self.send_json({"ok": False, "running": False, "logged_in": False,
                                "error": describe_error(exc)}, 500)

        elif path == "/api/x/open":
            raw = str(body.get("artist") or "").strip().lstrip("@")
            if re.fullmatch(r"[A-Za-z0-9_]{1,15}", raw):
                url = "https://x.com/%s/media" % raw
            else:
                url = "https://x.com/login"
            try:
                opened = webbrowser.open(url, new=2)
                self.send_json({"ok": bool(opened), "message": (
                    "已在系统浏览器打开"
                    if opened else "系统未确认浏览器已打开，请手动访问 " + url)})
            except Exception as exc:
                self.send_json({"ok": False, "error": describe_error(exc)}, 500)

        elif path == "/api/artists/search":
            source = get_source(body.get("source") or "danbooru")
            query = str(body.get("query") or "").strip()
            if source is None or not source.supports_artist_search:
                self.send_json({"ok": False, "error": "该来源不支持画师搜索"}, 400)
                return
            if not query:
                self.send_json({"ok": False, "error": "请输入搜索词"}, 400)
                return
            cfg = source_config_for_request(source, body, query)
            if isinstance(cfg, str):
                self.send_json({"ok": False, "error": cfg}, 400)
                return
            cfg["query_type"] = str(body.get("query_type") or "artist")
            try:
                artists = source.search_artists(query, cfg, limit=12)
                for artist in artists:
                    artist["x_handles"] = extract_x_handles(artist.get("urls"))
                    artist["x_user_ids"] = extract_x_user_ids(artist.get("urls"))
                    artist["pixiv_user_ids"] = extract_pixiv_user_ids(artist.get("urls"))
                self.send_json({"ok": True, "artists": artists})
            except Exception as exc:
                self.send_json({"ok": False, "error": describe_error(exc)})

        elif path == "/api/tagger/test":
            cfg = normalize_tagger_config(body)
            if isinstance(cfg, str):
                self.send_json({"ok": False, "error": cfg}, 400)
                return
            try:
                tagger = create_tagger(cfg)
                if tagger is None:
                    self.send_json({"ok": True, "message": "未启用额外打标"})
                else:
                    ok, message = tagger.test()
                    self.send_json({"ok": ok, "message": message} if ok
                                   else {"ok": False, "error": message})
            except Exception as exc:
                self.send_json({"ok": False, "error": describe_error(exc)})
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    server = None
    port = PORT
    for offset in range(10):
        try:
            server = ThreadingHTTPServer((HOST, PORT + offset), Handler)
            port = PORT + offset
            break
        except OSError:
            continue
    if server is None:
        print("[错误] 端口 %d~%d 均被占用" % (PORT, PORT + 9))
        return 1
    url = "http://%s:%d" % (HOST, port)
    print("=" * 58)
    print("  Multi-source Artist Grabber 已启动")
    print("  界面地址：%s" % url)
    print("  下载目录：%s" % DOWNLOAD_DIR)
    print("=" * 58)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
