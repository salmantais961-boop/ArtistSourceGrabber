# -*- coding: utf-8 -*-
"""X (Twitter) source.

The primary extractor is gallery-dl.  It is actively maintained and absorbs
most X Web API changes for us.  Authentication is supplied through a short
lived Netscape cookie file; cookie values are never placed on the command line
or included in errors/log messages.  A small GraphQL implementation remains as
a best-effort fallback when gallery-dl is not installed or cannot run.
"""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from contextlib import contextmanager, nullcontext

from .base import Post, Source
from http_util import describe_error, http_request


HOST_API = "https://x.com"
PAGE_LIMIT = 20
GALLERY_DL_TIMEOUT = 90

# Public web-client token.  This is not a user secret.
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAtMh0ll3xNTxR8XV6JFnH0T7%2Fic6Z%2B"
    "QGmupHbKlB2MNuBxKtqg%3D"
)

# These IDs are intentionally only a fallback.  gallery-dl is preferred
# because X changes GraphQL operation IDs and feature flags frequently.
QL_FALLBACK = {
    "UserByScreenName": "sUVq0x6JgCOSntROQO66Kw",
    "UserTweets": "V1ze5q3ijDS1VeLwLYBvJg",
    "UserMedia": "YqiWRTxRwzm0bc3uO9ev6w",
    "Likes": "V1ze5q3ijDS1VeLwLYBvJg",
}

FEATURES_USER = {
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

FEATURES_TWEETS = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "tweets_with_timeline_results_posts_enabled": True,
}


class GalleryDLUnavailable(RuntimeError):
    """gallery-dl is not installed or could not be started."""


class GalleryDLError(RuntimeError):
    """gallery-dl ran, but extraction failed."""


class TwitterXSource(Source):
    id = "twitter"
    label = "X (Twitter)"
    api_base = HOST_API
    needs_auth = True
    supports_artist_search = True
    can_count = False
    RATING_VALUES = ("",)
    SCRAPE_MODES = ("media_user", "tweets_user", "likes")

    def normalize_cfg(self, body):
        auth = self._normalize_cookie_value(
            body.get("auth_token") or body.get("auth_key") or "", "auth_token")
        ct0 = self._normalize_cookie_value(body.get("ct0") or "", "ct0")
        cookie_mode = str(body.get("x_cookie_mode") or "").strip().lower()
        if cookie_mode not in ("", "legacy", "managed"):
            return "X Cookie 模式不受支持"
        cookies_file = os.path.abspath(os.path.expanduser(
            str(body.get("x_cookies_file") or "").strip())) if body.get("x_cookies_file") else ""
        browser = str(body.get("x_cookies_from_browser") or "").strip().lower()
        if browser not in ("", "auto", "chrome", "edge", "firefox", "brave", "chromium"):
            return "浏览器 Cookie 来源不支持"
        if cookie_mode != "managed" and not auth and not cookies_file and not browser:
            return "请填写 auth_token，或选择完整 cookies.txt / 浏览器 Cookie"
        if cookies_file and not os.path.isfile(cookies_file):
            return "X cookies.txt 文件不存在"
        if self._has_cookie_control_chars(auth) or self._has_cookie_control_chars(ct0):
            return "X Cookie 中不能包含换行、制表符或 NUL 字符"

        artist_raw = str(body.get("artist") or body.get("x_handle") or "").strip()
        try:
            artist = self._normalize_artist(artist_raw)
        except ValueError as exc:
            return str(exc)

        try:
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            return "下载数量必须是整数"
        if count < 0:
            return "下载数量不能为负数"

        mode = str(body.get("x_mode") or "media_user").strip()
        if mode not in self.SCRAPE_MODES:
            return "X 抓取模式不合法"
        tag_format = body.get("tag_format")
        if tag_format not in ("comma", "space"):
            tag_format = "comma"

        return {
            "auth_token": auth,
            "ct0": ct0,
            "x_cookie_mode": cookie_mode,
            "x_cookies_file": cookies_file,
            "x_cookies_from_browser": browser,
            "x_browser_profile": str(body.get("x_browser_profile") or "").strip(),
            "artist": artist,
            "count": count,
            "x_mode": mode,
            "rating": "",
            "tag_format": tag_format,
            "include_artist": bool(body.get("include_artist")),
            "include_meta": bool(body.get("include_meta")),
            "skip_video": bool(body.get("skip_video", True)),
            "proxy": str(body.get("proxy") or "").strip(),
        }

    @staticmethod
    def _has_cookie_control_chars(value):
        return any(char in value for char in ("\r", "\n", "\t", "\0"))

    @staticmethod
    def _normalize_cookie_value(value, name):
        value = str(value or "").strip().strip('"').strip("'")
        if not value:
            return ""
        # Accept a copied `auth_token=...`, `ct0=...`, or complete Cookie header.
        if ";" in value or value.startswith(name + "="):
            for part in value.split(";"):
                key, sep, item = part.strip().partition("=")
                if sep and key.strip().casefold() == name.casefold():
                    return item.strip().strip('"').strip("'")
        return value

    @staticmethod
    def _normalize_artist(value):
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("请填写 X 用户名或 Danbooru 关联的 X handle")

        if re.match(r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/", raw, re.I):
            url = raw if "://" in raw else "https://" + raw
            parsed = urllib.parse.urlsplit(url)
            parts = [part for part in parsed.path.split("/") if part]
            if not parts:
                raise ValueError("X 主页 URL 中没有用户名")
            if len(parts) >= 3 and parts[0] == "i" and parts[1] == "user" and parts[2].isdigit():
                return "id:" + parts[2]
            if len(parts) >= 2 and parts[0] == "intent" and parts[1] == "user":
                user_id = urllib.parse.parse_qs(parsed.query).get("user_id", [""])[0]
                if user_id.isdigit():
                    return "id:" + user_id
            raw = urllib.parse.unquote(parts[0])

        raw = raw.strip().lstrip("@").rstrip("/")
        if raw.lower().startswith("id:"):
            user_id = raw[3:]
            if not user_id.isdigit():
                raise ValueError("X 用户 ID 必须为纯数字")
            return "id:" + user_id
        if raw.isdigit():
            return "id:" + raw
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", raw):
            raise ValueError("X 用户名格式不正确（仅支持 1-15 位字母、数字或下划线）")
        return raw

    @staticmethod
    def _display_artist(artist):
        return artist[3:] if artist.startswith("id:") else "@" + artist

    @staticmethod
    def _gallery_url(artist, mode):
        suffix = {
            "media_user": "media",
            "tweets_user": "tweets",
            "likes": "likes",
            "info": "info",
        }[mode]
        return "%s/%s/%s" % (HOST_API, artist, suffix)

    @staticmethod
    def _gallery_dl_command():
        try:
            if importlib.util.find_spec("gallery_dl") is not None:
                return [sys.executable, "-m", "gallery_dl"]
        except (ImportError, AttributeError, ValueError):
            pass
        executable = shutil.which("gallery-dl")
        return [executable] if executable else None

    @contextmanager
    def _cookie_file(self, cfg):
        fd, path = tempfile.mkstemp(prefix="x-session-", suffix=".cookies.txt")
        try:
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write("# Netscape HTTP Cookie File\n")
                handle.write(".x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\t%s\n" % cfg["auth_token"])
                if cfg.get("ct0"):
                    handle.write(".x.com\tTRUE\t/\tTRUE\t2147483647\tct0\t%s\n" % cfg["ct0"])
                handle.flush()
                os.fsync(handle.fileno())
            yield path
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._secure_unlink(path)

    @contextmanager
    def _cookie_args(self, cfg):
        if cfg.get("x_cookie_mode") == "managed":
            # The application-managed X browser owns a dedicated, non-default
            # Chrome profile.  Reading cookies from its live CDP session avoids
            # copying Chrome v20 App-Bound encrypted cookie databases.
            from .x_browser_session import cookie_file as managed_cookie_file
            with managed_cookie_file(required_names=("auth_token", "ct0")) as path:
                yield ["--cookies", path]
            return
        if cfg.get("x_cookies_file"):
            yield ["--cookies", cfg["x_cookies_file"]]
            return
        if cfg.get("x_cookies_from_browser"):
            browser = cfg.get("_resolved_browser") or cfg["x_cookies_from_browser"]
            if browser == "auto":
                candidates = self._browser_candidates()
                if not candidates:
                    raise GalleryDLUnavailable("未检测到 Chrome、Edge、Firefox、Brave 或 Chromium 配置")
                browser = candidates[0]
            spec = browser + "/x.com"
            profile = cfg.get("_resolved_profile") or cfg.get("x_browser_profile")
            if profile:
                spec += ":" + profile
            yield ["--cookies-from-browser", spec]
            return
        with self._cookie_file(cfg) as path:
            yield ["--cookies", path]

    @staticmethod
    def _browser_candidates():
        local = os.environ.get("LOCALAPPDATA") or ""
        roaming = os.environ.get("APPDATA") or ""
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        xdg_local = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        candidates = (
            ("edge", os.path.join(local, "Microsoft", "Edge", "User Data"),
             os.path.join(xdg_config, "microsoft-edge")),
            ("chrome", os.path.join(local, "Google", "Chrome", "User Data"),
             os.path.join(xdg_config, "google-chrome")),
            ("brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
             os.path.join(xdg_config, "BraveSoftware", "Brave-Browser")),
            ("chromium", os.path.join(local, "Chromium", "User Data"),
             os.path.join(xdg_config, "chromium"),
             os.path.join(xdg_local, "chromium")),
            ("firefox", os.path.join(roaming, "Mozilla", "Firefox", "Profiles"),
             os.path.join(os.path.expanduser("~"), ".mozilla", "firefox")),
        )
        return [name for name, *paths in candidates
                if any(path and os.path.isdir(path) for path in paths)]

    @staticmethod
    def _browser_profile_candidates(selected=None):
        local = os.environ.get("LOCALAPPDATA") or ""
        roaming = os.environ.get("APPDATA") or ""
        xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        candidates = {
            "chrome": (os.path.join(local, "Google", "Chrome", "User Data"),
                       os.path.join(xdg_config, "google-chrome")),
            "edge": (os.path.join(local, "Microsoft", "Edge", "User Data"),
                     os.path.join(xdg_config, "microsoft-edge")),
            "brave": (os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
                      os.path.join(xdg_config, "BraveSoftware", "Brave-Browser")),
            "chromium": (os.path.join(local, "Chromium", "User Data"),
                         os.path.join(xdg_config, "chromium")),
            "firefox": (os.path.join(roaming, "Mozilla", "Firefox", "Profiles"),
                        os.path.join(os.path.expanduser("~"), ".mozilla", "firefox")),
        }
        roots = {}
        for browser, paths in candidates.items():
            found = next((p for p in paths if p and os.path.isdir(p)), "")
            if found:
                roots[browser] = found
        names = [selected] if selected and selected != "auto" else list(roots)
        result = []
        for browser in names:
            root = roots.get(browser) or ""
            if not os.path.isdir(root):
                continue
            last_used = ""
            state_path = os.path.join(root, "Local State")
            try:
                with open(state_path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                last_used = str((state.get("profile") or {}).get("last_used") or "")
            except Exception:
                pass
            if last_used and os.path.isdir(os.path.join(root, last_used)):
                result.append((browser, last_used))
            result.append((browser, ""))
            try:
                entries = os.listdir(root)
            except OSError:
                entries = []
            if browser == "firefox":
                profiles = [entry for entry in entries if os.path.isdir(os.path.join(root, entry))]
            else:
                profiles = [entry for entry in entries
                            if (entry == "Default" or entry.startswith("Profile ")) and
                            os.path.isdir(os.path.join(root, entry))]
            result.extend((browser, profile) for profile in profiles if profile != last_used)
        return list(dict.fromkeys(result))

    @staticmethod
    def _secure_unlink(path):
        """Best-effort overwrite followed by unlink.

        Filesystems and SSD wear levelling cannot guarantee physical erasure,
        but this prevents ordinary recovery and, importantly, always removes
        the temporary credential file even on timeout or parser failure.
        """
        if not path:
            return
        try:
            size = os.path.getsize(path)
            with open(path, "r+b", buffering=0) as handle:
                remaining = size
                block = b"\0" * 4096
                while remaining > 0:
                    chunk = block[:min(remaining, len(block))]
                    handle.write(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass

    def _redact(self, text, cfg):
        value = str(text or "")
        for secret in (cfg.get("auth_token"), cfg.get("ct0")):
            if secret:
                value = value.replace(str(secret), "<redacted>")
        return value

    def _run_gallery_dl(self, url, cfg, range_spec=None, use_cookies=True):
        command = self._gallery_dl_command()
        if not command:
            raise GalleryDLUnavailable(
                "未安装 gallery-dl；请运行 pip install gallery-dl"
            )

        cookie_context = self._cookie_args(cfg) if use_cookies else nullcontext([])
        with cookie_context as cookie_args:
            args = command + [
                "--config-ignore",
                "--dump-json",
                "-o", "output.jsonl=false",
                # X occasionally returns tweet objects without `user`/`core`.
                # gallery-dl's transformed mode raises KeyError; raw mode lets
                # our tolerant normalizer handle those entries instead.
                "-o", "extractor.twitter.transform=false",
                "-o", "extractor.twitter.csrf=auto",
                "-o", "extractor.twitter.size=orig",
                "-o", "extractor.twitter.cards=false",
                "-o", "extractor.twitter.articles=false",
                "-o", "extractor.twitter.previews=false",
                "-o", "extractor.twitter.retweets=false",
                "-o", "extractor.twitter.videos=%s" % (
                    "false" if cfg.get("skip_video") else "true"
                ),
            ] + cookie_args
            if cfg.get("proxy"):
                args.extend(("--proxy", cfg["proxy"]))
            if range_spec:
                args.extend(("--range", str(range_spec)))
            args.append(url)

            creationflags = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW
            try:
                completed = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                    timeout=GALLERY_DL_TIMEOUT,
                    creationflags=creationflags,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise GalleryDLUnavailable("gallery-dl 请求 X 超时")
            except OSError:
                raise GalleryDLUnavailable("无法启动 gallery-dl")

        messages = self._decode_gallery_output(completed.stdout)
        errors = []
        for message in messages:
            if isinstance(message, (list, tuple)) and message and message[0] == -1:
                detail = message[1] if len(message) > 1 and isinstance(message[1], dict) else {}
                name = detail.get("error") or "ExtractionError"
                info = detail.get("message") or "X extraction failed"
                errors.append("%s: %s" % (name, info))
        if errors:
            diagnostics = []
            for line in str(completed.stderr or "").splitlines():
                lower = line.casefold()
                if ("extracted 0 cookies" in lower or "decrypt" in lower or
                        "permission denied" in lower or
                        "cookie" in lower and "error" in lower):
                    diagnostics.append(line.strip())
            detail = "gallery-dl: " + "; ".join(errors)
            if diagnostics:
                detail += "；" + "；".join(diagnostics[-2:])
            raise GalleryDLError(self._redact(detail, cfg))
        if completed.returncode != 0:
            stderr = self._redact(completed.stderr, cfg).strip().splitlines()
            detail = stderr[-1] if stderr else "exit code %s" % completed.returncode
            raise GalleryDLError("gallery-dl 执行失败：%s" % detail)
        return messages

    def _resolve_public_artist(self, artist, cfg):
        if str(artist).startswith("id:"):
            return str(artist)
        public_cfg = dict(cfg)
        public_cfg["auth_token"] = ""
        public_cfg["ct0"] = ""
        public_cfg["x_cookies_file"] = ""
        public_cfg["x_cookies_from_browser"] = ""
        messages = self._run_gallery_dl(
            self._gallery_url(str(artist), "info"), public_cfg, use_cookies=False)
        profile = self._profile_from_gallery(messages)
        user_id = str(profile.get("id") or "")
        if not user_id.isdigit():
            raise GalleryDLError("无法解析目标 X 账号 @%s 的数字 ID" % artist)
        cfg["_target_handle"] = str(profile.get("name") or artist)
        cfg["_resolved_artist"] = "id:" + user_id
        return cfg["_resolved_artist"]

    @staticmethod
    def _decode_gallery_output(stdout):
        text = str(stdout or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            # Also accept output.jsonl=true for compatibility with older or
            # externally wrapped gallery-dl executables.
            data = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data.append(json.loads(line))
                except ValueError:
                    raise GalleryDLError("gallery-dl 返回了无法解析的 JSON")
        if not isinstance(data, list):
            raise GalleryDLError("gallery-dl JSON 顶层格式不正确")
        if data and isinstance(data[0], int):
            return [data]
        return data

    @staticmethod
    def _extension(url, metadata):
        ext = str(metadata.get("extension") or "").lower().lstrip(".")
        if not ext:
            parsed = urllib.parse.urlsplit(url)
            ext = os.path.splitext(parsed.path)[1].lower().lstrip(".")
            if not ext:
                ext = urllib.parse.parse_qs(parsed.query).get("format", [""])[0].lower()
        if ext == "jpeg":
            ext = "jpg"
        return ext or "jpg"

    def _posts_from_gallery(self, messages, cfg):
        posts = []
        seen = set()
        for message in messages:
            if not isinstance(message, (list, tuple)) or len(message) < 3 or message[0] != 3:
                continue
            url, meta = message[1], message[2]
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if not isinstance(meta, dict):
                meta = {}

            ext = self._extension(url, meta)
            media_type = str(meta.get("type") or "").lower()
            is_video = (
                media_type in ("video", "animated_gif", "article:video")
                or ext in ("mp4", "webm", "mkv", "m3u8")
            )
            if cfg.get("skip_video") and is_video:
                continue

            legacy = meta.get("legacy") if isinstance(meta.get("legacy"), dict) else meta
            tweet_id = str(meta.get("tweet_id") or meta.get("id") or meta.get("rest_id") or
                           legacy.get("id_str") or "")
            if not tweet_id:
                continue
            try:
                number = max(int(meta.get("num") or 1) - 1, 0)
            except (TypeError, ValueError):
                number = 0
            post_id = "%s_%d" % (tweet_id, number)
            if post_id in seen:
                continue
            seen.add(post_id)

            author = meta.get("author") if isinstance(meta.get("author"), dict) else {}
            target_user = meta.get("user") if isinstance(meta.get("user"), dict) else {}
            core_user = (((meta.get("core") or {}).get("user_results") or {}).get("result") or {}) \
                if isinstance(meta.get("core"), dict) else {}
            core_legacy = core_user.get("legacy") if isinstance(core_user.get("legacy"), dict) else {}
            artist = str(author.get("name") or author.get("screen_name") or
                         target_user.get("name") or target_user.get("screen_name") or
                         core_legacy.get("screen_name") or cfg.get("artist") or "")
            hashtags = meta.get("hashtags") or []
            if not hashtags:
                hashtags = [item.get("text") for item in
                            ((legacy.get("entities") or {}).get("hashtags") or [])
                            if isinstance(item, dict) and item.get("text")]
            if not isinstance(hashtags, list):
                hashtags = list(hashtags) if isinstance(hashtags, tuple) else []
            raw = {
                "id_str": tweet_id,
                "full_text": str(meta.get("content") or legacy.get("full_text") or
                                 legacy.get("text") or ""),
                "created_at": str(meta.get("date") or legacy.get("created_at") or ""),
                "hashtags": [str(tag) for tag in hashtags],
                "user": {
                    "id": author.get("id"),
                    "screen_name": artist,
                    "name": author.get("nick"),
                },
                "media_idx": number,
                "width": meta.get("width"),
                "height": meta.get("height"),
                "description": meta.get("description"),
                "sensitive": meta.get("sensitive"),
                "source": "gallery-dl",
            }
            posts.append(Post(
                id=post_id,
                ext=ext,
                file_url=url,
                large_url=None,
                is_video=is_video,
                artist=artist,
                raw=raw,
                extra_headers={"Referer": "https://x.com/"},
            ))
        return posts

    @staticmethod
    def _profile_from_gallery(messages):
        for message in messages:
            if (isinstance(message, (list, tuple)) and len(message) >= 2
                    and message[0] == 2 and isinstance(message[1], dict)):
                data = message[1]
                legacy = data.get("legacy") if isinstance(data.get("legacy"), dict) else {}
                core = data.get("core") if isinstance(data.get("core"), dict) else {}
                if data.get("rest_id") or legacy:
                    return {
                        "id": data.get("rest_id") or data.get("id_str") or data.get("id"),
                        "name": data.get("name") or core.get("screen_name") or legacy.get("screen_name"),
                        "nick": data.get("nick") or core.get("name") or legacy.get("name"),
                        "media_count": data.get("media_count") or legacy.get("media_count"),
                        "raw": data,
                    }
                return data
        return {}

    def test(self, cfg):
        artist = cfg["artist"]
        try:
            resolved_artist = self._resolve_public_artist(artist, cfg)
        except (GalleryDLUnavailable, GalleryDLError) as exc:
            return False, "目标画师解析失败：%s" % exc
        url = self._gallery_url(resolved_artist, cfg.get("x_mode", "media_user"))
        browser_source = cfg.get("x_cookies_from_browser")
        if browser_source:
            errors = []
            selected_profile = cfg.get("x_browser_profile")
            candidates = ([(browser_source, selected_profile)] if selected_profile and
                          browser_source != "auto" else
                          self._browser_profile_candidates(browser_source))
            for browser, profile_name in candidates:
                cfg["_resolved_browser"] = browser
                cfg["_resolved_profile"] = profile_name
                try:
                    self._run_gallery_dl(url, cfg, "1")
                    label = browser.title() + ((" / " + profile_name) if profile_name else "")
                    return True, "已从 %s 读取 Cookie 并验证 X 媒体访问：%s" % (
                        label, self._display_artist(cfg.get("_target_handle") or artist))
                except (GalleryDLUnavailable, GalleryDLError) as exc:
                    label = browser + (("/" + profile_name) if profile_name else "")
                    errors.append("%s: %s" % (label, self._friendly_gallery_error(exc)))
            cfg.pop("_resolved_browser", None)
            cfg.pop("_resolved_profile", None)
            if not errors:
                return False, "未检测到可读取的浏览器配置"
            return False, "读取浏览器 Cookie 失败：" + "；".join(errors[:6])
        try:
            self._run_gallery_dl(url, cfg, "1")
            return True, "gallery-dl 已验证 X 媒体访问：%s" % self._display_artist(
                cfg.get("_target_handle") or artist)
        except GalleryDLUnavailable as gallery_error:
            if cfg.get("x_cookie_mode") == "managed":
                return False, self._friendly_gallery_error(gallery_error)
            ok, message = self._test_graphql(cfg)
            if ok:
                return True, "gallery-dl 暂不可用，已切换 GraphQL：%s" % message
            return False, "%s；GraphQL 备用验证也失败：%s" % (gallery_error, message)
        except GalleryDLError as gallery_error:
            return False, self._friendly_gallery_error(gallery_error)

    @staticmethod
    def _friendly_gallery_error(error):
        message = str(error)
        lower = message.casefold()
        if "could not authenticate" in lower or "authrequired" in lower or "401" in lower:
            if "permission denied" in lower:
                return ("Chrome 正在占用登录 Profile 的 Cookie 数据库。请完全退出 Chrome（包括后台进程）"
                        "后再次选择“自动检测”；程序已识别到活动 Profile，无需重新登录。")
            if "extracted 0 cookies" in lower:
                return ("所选浏览器 Profile 中没有找到 x.com Cookie。请点击“打开 X 登录/画师页”，"
                        "在弹出的系统浏览器中登录，完全关闭浏览器后再测试；或填写正确的 Profile。")
            if "decrypt" in lower:
                return ("浏览器 Cookie 存在但无法解密。请完全关闭浏览器后重试，"
                        "或导出 Netscape cookies.txt 后填写其路径。")
            return ("X 拒绝了当前会话（401）。如果两个 Cookie 值确认无误，请改用完整 "
                    "cookies.txt 或“从浏览器读取 Cookie”；X 可能已轮换会话或绑定浏览器环境。")
        if "429" in lower or "rate" in lower:
            return "X 返回 429 限流，请稍后重试或保持与浏览器相同的代理出口。"
        if "403" in lower or "forbidden" in lower:
            return "X 返回 403，账号可能需要在浏览器完成验证，或当前代理/IP 被限制。"
        if "timeout" in lower:
            return "gallery-dl 请求 X 超时，请检查代理与网络出口。"
        return message

    def resolve_artist(self, cfg, logger):
        artist = cfg.get("_resolved_artist") or self._resolve_public_artist(cfg["artist"], cfg)
        label = cfg.get("_target_handle") or cfg["artist"]
        logger("X 目标画师：%s（user_id=%s）" % (
            self._display_artist(label), artist.replace("id:", "")))
        return artist

    def search_artists(self, query, cfg, limit=10):
        try:
            artist = self._normalize_artist(query)
        except ValueError:
            return []

        try:
            messages = self._run_gallery_dl(self._gallery_url(artist, "info"), cfg)
            profile = self._profile_from_gallery(messages)
            if profile.get("id"):
                name = str(profile.get("name") or artist.replace("id:", ""))
                return [{
                    "id": str(profile.get("id")),
                    "name": name,
                    "site": self.id,
                    "profile_url": "https://x.com/%s" % name,
                    "post_count": profile.get("media_count"),
                    "other_names": str(profile.get("nick") or ""),
                    "is_banned": False,
                }][:limit]
        except (GalleryDLUnavailable, GalleryDLError):
            pass
        return self._search_artist_graphql(artist, cfg)[:limit]

    def count_posts(self, artist_key, cfg):
        # X reports media tweet counts, while one tweet may contain 1-4 files.
        # Returning it as a file count would truncate downloads.
        return -1

    def list_posts(self, artist_key, page, cfg):
        start = (max(int(page), 1) - 1) * PAGE_LIMIT + 1
        end = start + PAGE_LIMIT - 1
        url = self._gallery_url(str(artist_key), cfg.get("x_mode", "media_user"))
        try:
            messages = self._run_gallery_dl(url, cfg, "%d-%d" % (start, end))
            return self._posts_from_gallery(messages, cfg)
        except GalleryDLError as exc:
            # Do not hide a real 401/403/429/timeout behind the obsolete fallback.
            raise RuntimeError(self._friendly_gallery_error(exc))
        except GalleryDLUnavailable as exc:
            posts = self._list_posts_graphql(artist_key, page, cfg)
            if posts:
                return posts
            raise RuntimeError("%s；GraphQL 后备也未返回作品" % exc)

    def make_filename(self, post, cfg):
        return "x_%s.%s" % (post.get("id"), post.get("ext", "").lower())

    def build_caption(self, post, cfg):
        # X captions are free-form prose rather than booru tags.  Leave tagging
        # to the configured LLM/ONNX tagger.
        return ""

    # ------------------------------------------------------------------
    # GraphQL fallback

    def auth_headers(self, cfg, with_csrf=True):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Authorization": "Bearer %s" % BEARER,
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        }
        if with_csrf:
            headers["x-csrf-token"] = cfg.get("ct0") or cfg["auth_token"]
        return headers

    @staticmethod
    def cookies(cfg):
        value = "auth_token=%s" % cfg["auth_token"]
        if cfg.get("ct0"):
            value += "; ct0=%s" % cfg["ct0"]
        return value

    def _gql(self, operation, variables, cfg, features=None):
        try:
            self._bootstrap(cfg)
        except Exception:
            pass
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(
                features or (FEATURES_USER if operation == "UserByScreenName" else FEATURES_TWEETS),
                separators=(",", ":"),
            ),
        }
        url = "%s/i/api/graphql/%s/%s?%s" % (
            HOST_API,
            QL_FALLBACK.get(operation, "x"),
            operation,
            urllib.parse.urlencode(params),
        )
        headers = self.auth_headers(cfg)
        headers["Cookie"] = self.cookies(cfg)
        return http_request(url, cfg.get("proxy"), headers=headers, timeout=30)

    def _bootstrap(self, cfg):
        if getattr(self, "_booted", False):
            return
        self._booted = True
        try:
            html = http_request(
                HOST_API + "/", cfg.get("proxy"),
                headers={"User-Agent": "Mozilla/5.0"}, raw=False,
            )
            if isinstance(html, bytes):
                html = html.decode("utf-8", "replace")
            match = re.search(
                r'((?:https?://)?abs\.twimg\.com/resourcexxx/client-web/'
                r'main\.[A-Za-z0-9]+\.js)', html,
            )
            if not match:
                return
            main_url = match.group(1)
            if main_url.startswith("//"):
                main_url = "https:" + main_url
            javascript = http_request(
                main_url, cfg.get("proxy"),
                headers={"User-Agent": "Mozilla/5.0"}, raw=False,
            )
            if isinstance(javascript, bytes):
                javascript = javascript.decode("utf-8", "replace")
            for operation in tuple(QL_FALLBACK):
                found = re.search(
                    r'queryId:"([A-Za-z0-9_-]+)",operationName:"%s"'
                    % operation,
                    javascript,
                )
                if found:
                    QL_FALLBACK[operation] = found.group(1)
        except Exception:
            return

    def _test_graphql(self, cfg):
        try:
            settings = http_request(
                "https://api.twitter.com/1.1/account/settings.json",
                cfg.get("proxy"),
                headers={
                    "Authorization": "Bearer %s" % BEARER,
                    "Cookie": self.cookies(cfg),
                    "x-csrf-token": cfg.get("ct0") or cfg["auth_token"],
                    "User-Agent": "Mozilla/5.0",
                },
            )
            if isinstance(settings, dict) and settings.get("screen_name"):
                return True, "认证成功：@%s" % settings["screen_name"]
            return False, "X 未返回 screen_name，Cookie 可能无效"
        except Exception as exc:
            return False, self._redact(describe_error(exc), cfg)

    def _user_graphql(self, artist, cfg):
        if str(artist).startswith("id:"):
            return {"rest_id": str(artist)[3:], "legacy": {}}
        data = self._gql(
            "UserByScreenName",
            {"screen_name": str(artist).lstrip("@"), "withSafetyModeUserThrottle": True},
            cfg,
            FEATURES_USER,
        )
        return (((data or {}).get("data") or {}).get("user") or {}).get("result") or {}

    def _search_artist_graphql(self, artist, cfg):
        if artist.startswith("id:"):
            return []
        try:
            user = self._user_graphql(artist, cfg)
        except Exception:
            return []
        legacy = user.get("legacy") or {}
        user_id = user.get("rest_id")
        if not user_id:
            return []
        name = legacy.get("screen_name") or artist
        return [{
            "id": str(user_id),
            "name": name,
            "site": self.id,
            "profile_url": "https://x.com/%s" % name,
            "post_count": legacy.get("media_count"),
            "other_names": legacy.get("name", ""),
            "is_banned": False,
        }]

    def _list_posts_graphql(self, artist_key, page, cfg):
        try:
            user = self._user_graphql(str(artist_key), cfg)
            user_id = user.get("rest_id")
            if not user_id:
                return []
        except Exception:
            return []

        if page == 1:
            cfg["_twitter_cursor"] = None
        elif "_twitter_cursor" not in cfg or not cfg.get("_twitter_cursor"):
            return []

        operation = {
            "media_user": "UserMedia",
            "tweets_user": "UserTweets",
            "likes": "Likes",
        }[cfg.get("x_mode", "media_user")]
        variables = {
            "userId": str(user_id),
            "count": PAGE_LIMIT,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cfg.get("_twitter_cursor"):
            variables["cursor"] = cfg["_twitter_cursor"]
        try:
            data = self._gql(operation, variables, cfg)
        except Exception:
            return []

        instructions = self._find_instructions((data or {}).get("data") or {})
        cfg["_twitter_cursor"] = self._bottom_cursor(instructions)
        posts = []
        seen_tweets = set()
        for result in self._tweet_results(instructions):
            result = self._unwrap_tweet_result(result)
            tweet_id = str(result.get("rest_id") or (result.get("legacy") or {}).get("id_str") or "")
            if not tweet_id or tweet_id in seen_tweets:
                continue
            seen_tweets.add(tweet_id)
            self._extract_graphql_media(result, posts, cfg)
        return posts

    @staticmethod
    def _find_instructions(root):
        queue = [root]
        while queue:
            node = queue.pop(0)
            if isinstance(node, dict):
                instructions = node.get("instructions")
                if isinstance(instructions, list):
                    return instructions
                queue.extend(node.values())
            elif isinstance(node, list):
                queue.extend(node)
        return []

    @staticmethod
    def _bottom_cursor(root):
        stack = list(root) if isinstance(root, list) else [root]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("cursorType") == "Bottom" and node.get("value"):
                    return node["value"]
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return None

    @staticmethod
    def _tweet_results(root):
        results = []
        stack = list(root) if isinstance(root, list) else [root]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                tweet_results = node.get("tweet_results")
                if isinstance(tweet_results, dict) and isinstance(tweet_results.get("result"), dict):
                    results.append(tweet_results["result"])
                    continue
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return results

    @staticmethod
    def _unwrap_tweet_result(result):
        node = result
        for _ in range(4):
            if not isinstance(node, dict):
                return {}
            if isinstance(node.get("tweet"), dict):
                node = node["tweet"]
            elif isinstance(node.get("result"), dict):
                node = node["result"]
            else:
                break
        return node if isinstance(node, dict) else {}

    def _extract_graphql_media(self, result, out, cfg):
        legacy = result.get("legacy") or {}
        tweet_id = str(legacy.get("id_str") or result.get("rest_id") or "")
        if not tweet_id:
            return

        retweeted = legacy.get("retweeted_status_result") or {}
        if isinstance(retweeted, dict) and retweeted.get("result"):
            result = self._unwrap_tweet_result(retweeted["result"])
            legacy = result.get("legacy") or {}
            tweet_id = str(legacy.get("id_str") or result.get("rest_id") or tweet_id)

        entities = legacy.get("extended_entities") or legacy.get("entities") or {}
        media_items = entities.get("media") or []
        user_result = ((((result.get("core") or {}).get("user_results") or {}).get("result")) or {})
        user_legacy = user_result.get("legacy") or {}
        artist = user_legacy.get("screen_name") or ""
        text_entities = legacy.get("entities") or {}
        hashtags = [
            item.get("text") for item in text_entities.get("hashtags") or []
            if isinstance(item, dict) and item.get("text")
        ]

        for index, media in enumerate(media_items):
            media_type = str(media.get("type") or "").lower()
            url = ""
            ext = "jpg"
            is_video = media_type in ("video", "animated_gif")
            if media_type == "photo":
                original = media.get("media_url_https") or media.get("media_url") or ""
                if not original:
                    continue
                path_ext = os.path.splitext(urllib.parse.urlsplit(original).path)[1].lower().lstrip(".")
                ext = "jpg" if path_ext == "jpeg" else (path_ext or "jpg")
                url = original.split("?", 1)[0] + "?format=%s&name=orig" % ext
            elif is_video:
                if cfg.get("skip_video"):
                    continue
                variants = (media.get("video_info") or {}).get("variants") or []
                candidates = [
                    item for item in variants
                    if str(item.get("content_type") or "").startswith("video/mp4") and item.get("url")
                ]
                if not candidates:
                    continue
                best = max(candidates, key=lambda item: item.get("bitrate") or 0)
                url, ext = best["url"], "mp4"
            else:
                continue

            out.append(Post(
                id="%s_%d" % (tweet_id, index),
                ext=ext,
                file_url=url,
                large_url=None,
                is_video=is_video,
                artist=artist,
                raw={
                    "id_str": tweet_id,
                    "full_text": legacy.get("full_text") or legacy.get("text") or "",
                    "created_at": legacy.get("created_at") or "",
                    "hashtags": hashtags,
                    "urls": text_entities.get("urls") or [],
                    "user_mentions": text_entities.get("user_mentions") or [],
                    "user": user_legacy,
                    "media_idx": index,
                    "source": "graphql-fallback",
                },
                extra_headers={"Referer": "https://x.com/"},
            ))
