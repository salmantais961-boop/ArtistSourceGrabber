# -*- coding: utf-8 -*-
"""Managed, persistent browser session dedicated to Pixiv authentication.

The browser owns a separate profile under LOCALAPPDATA and exposes DevTools
only on loopback.  Cookie values are read from the live browser when needed;
they are never written to the state file or included in status/error output.
"""

from __future__ import annotations

import math
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from typing import Iterable, List, Mapping, Optional

from .chrome_cookies import (
    BrowserCookie,
    ChromeCookieError,
    _cdp_call,
    _normalize_domains,
    _read_all_cookies,
    _reserve_local_port,
    _secure_unlink,
    _target_cookies,
)
from .x_browser_session import (
    XBrowserSession,
    XBrowserSessionError,
    _browser_id,
    _fetch_version,
    _find_browser_executable,
    _has_cookie_control_chars,
    _request_browser_close,
    _stop_owned_process,
    _wait_for_endpoint,
)


DEFAULT_LOGIN_URL = "https://accounts.pixiv.net/login"
DEFAULT_TIMEOUT = 20.0
STATE_FILENAME = "PixivBrowserSession.json"
DEFAULT_DOMAINS = ("pixiv.net", "accounts.pixiv.net")


class PixivBrowserSessionError(RuntimeError):
    """A sanitized managed-browser failure that never contains credentials."""

    def __init__(self, message: str, code: str = "pixiv_browser_error"):
        super().__init__(message)
        self.code = code


class PixivBrowserSession(XBrowserSession):
    """Manage one visible Chrome instance backed by a Pixiv-only profile."""

    def __init__(
            self, profile_dir: str = "", browser: str = "chrome",
            executable: str = "", timeout: float = DEFAULT_TIMEOUT):
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        else:
            local = os.environ.get("XDG_DATA_HOME") or os.path.join(
                os.path.expanduser("~"), ".local", "share")
        app_dir = os.path.join(local, "DanbooruGrabber")
        dedicated = profile_dir or os.path.join(app_dir, "PixivBrowserProfile")
        try:
            super().__init__(dedicated, browser, executable, timeout)
        except XBrowserSessionError as exc:
            raise PixivBrowserSessionError(
                "专用 Pixiv 浏览器配置无效", exc.code) from None
        self.state_path = os.path.join(os.path.dirname(self.profile_dir), STATE_FILENAME)

    def launch(
            self, url: str = DEFAULT_LOGIN_URL, *,
            headless: bool = False) -> Mapping[str, object]:
        """Start/reuse the profile in visible or background mode."""
        safe_url = _validate_pixiv_url(url)
        wanted_mode = "headless" if headless else "visible"
        with self._lock:
            connection = self._connection()
            if connection is not None and connection.get("mode") == wanted_mode:
                if not headless:
                    self._ensure_pixiv_target(connection["websocket_url"], safe_url)
                message = ("专用 Pixiv 浏览器正在后台运行" if headless else
                           "专用 Pixiv 浏览器已打开")
                return self._status_dict(True, connection, message)
            if connection is not None:
                self._shutdown_connection(connection)

            self._remove_state()
            try:
                executable = self.executable or _find_browser_executable(self.browser)
                os.makedirs(self.profile_dir, exist_ok=True)
                try:
                    os.chmod(
                        self.profile_dir,
                        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                except OSError:
                    pass
                port = _reserve_local_port()
                process = _launch_browser(
                    executable, self.profile_dir, port, safe_url, headless=headless)
                self._process = process
                websocket_url = _wait_for_endpoint(process, port, self.timeout)
                connection = {
                    "pid": int(process.pid),
                    "port": port,
                    "browser_id": _browser_id(websocket_url),
                    "websocket_url": websocket_url,
                    "mode": wanted_mode,
                }
                self._write_state(connection)
                message = ("专用 Pixiv 浏览器正在后台运行" if headless else
                           "请在专用浏览器中登录 Pixiv")
                return self._status_dict(True, connection, message)
            except PixivBrowserSessionError:
                _stop_owned_process(self._process)
                self._process = None
                self._remove_state()
                raise
            except Exception as exc:
                _stop_owned_process(self._process)
                self._process = None
                self._remove_state()
                code = getattr(exc, "code", "launch_failed")
                raise PixivBrowserSessionError(
                    "无法启动专用 Pixiv 浏览器", code) from None

    def open_login(self, url: str = DEFAULT_LOGIN_URL) -> Mapping[str, object]:
        # Explicit login/account switching always uses a visible window.
        return self.launch(url, headless=False)

    def check_login(self) -> Mapping[str, object]:
        """Check the saved login and stop Chrome once it is confirmed."""
        with self._lock:
            started_temporarily = False
            try:
                connection = self._connection()
                if connection is None:
                    self.launch(DEFAULT_LOGIN_URL, headless=True)
                    started_temporarily = True
                    connection = self._connection()
                if connection is None:
                    raise PixivBrowserSessionError(
                        "专用 Pixiv 浏览器启动后无法连接", "not_running")
                cookies = self._read_cookies_from_connection(
                    connection, DEFAULT_DOMAINS, ())
            except PixivBrowserSessionError as exc:
                connection = self._connection()
                if started_temporarily and connection is not None:
                    self._shutdown_connection(connection)
                    connection = None
                if connection is None:
                    return {
                        "ok": False,
                        "running": False,
                        "logged_in": False,
                        "message": str(exc),
                        "code": exc.code,
                    }
                return self._status_dict(
                    False, connection, str(exc), logged_in=False, code=exc.code)
            names = {cookie.name for cookie in cookies}
            logged_in = "PHPSESSID" in names
            if logged_in:
                self._shutdown_connection(connection)
                return {
                    "ok": True,
                    "running": False,
                    "logged_in": True,
                    "message": "Pixiv 登录已保存，抓取时按需启动",
                    "profile_dir": self.profile_dir,
                }
            if started_temporarily:
                self._shutdown_connection(connection)
                return {
                    "ok": True,
                    "running": False,
                    "logged_in": False,
                    "message": "请打开专用浏览器完成 Pixiv 登录",
                    "profile_dir": self.profile_dir,
                }
            return self._status_dict(
                True, connection, "请在专用浏览器中完成 Pixiv 登录",
                logged_in=False)

    def read_cookies(
            self, domains: Iterable[str] = DEFAULT_DOMAINS,
            required_names: Iterable[str] = ("PHPSESSID",)) -> List[BrowserCookie]:
        """Read live cookies and never leave a temporary browser resident."""
        with self._lock:
            started_temporarily = False
            connection = self._connection()
            if connection is None:
                self.launch(DEFAULT_LOGIN_URL, headless=True)
                started_temporarily = True
                connection = self._connection()
            if connection is None:
                raise PixivBrowserSessionError(
                    "专用 Pixiv 浏览器未运行", "not_running")
            try:
                return self._read_cookies_from_connection(
                    connection, domains, required_names)
            finally:
                if started_temporarily:
                    current = self._connection() or connection
                    self._shutdown_connection(current)

    def _read_cookies_from_connection(
            self, connection: Mapping[str, object], domains: Iterable[str],
            required_names: Iterable[str]) -> List[BrowserCookie]:
        target_domains = _normalize_domains(domains)
        required = {str(name).strip() for name in required_names if str(name).strip()}
        try:
            raw = _read_all_cookies(connection["websocket_url"], self.timeout)
            cookies = _target_cookies(raw, target_domains)
        except ChromeCookieError:
            raise PixivBrowserSessionError(
                "无法从专用 Pixiv 浏览器读取登录会话",
                "cookie_read_failed") from None
        except Exception:
            raise PixivBrowserSessionError(
                "专用 Pixiv 浏览器 Cookie 读取失败",
                "cookie_read_failed") from None
        present = {cookie.name for cookie in cookies}
        missing = sorted(required - present)
        if missing:
            raise PixivBrowserSessionError(
                "专用浏览器尚未完成 Pixiv 登录，缺少会话 Cookie：%s"
                % ", ".join(missing),
                "not_logged_in")
        return cookies

    def read_phpsessid(self) -> str:
        """Return PHPSESSID for immediate request construction only."""
        cookies = self.read_cookies(required_names=("PHPSESSID",))
        value = next(
            (cookie.value for cookie in cookies if cookie.name == "PHPSESSID"), "")
        if not value:
            raise PixivBrowserSessionError(
                "专用浏览器尚未完成 Pixiv 登录", "not_logged_in")
        return value

    @contextmanager
    def cookie_file(
            self, domains: Iterable[str] = DEFAULT_DOMAINS,
            required_names: Iterable[str] = ("PHPSESSID",)):
        """Yield a short-lived Netscape file and securely delete it on exit."""
        cookies = self.read_cookies(domains, required_names)
        fd, path = tempfile.mkstemp(
            prefix="pixiv-managed-session-", suffix=".cookies.txt")
        try:
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write("# Netscape HTTP Cookie File\n")
                for cookie in cookies:
                    if _has_cookie_control_chars(cookie):
                        raise PixivBrowserSessionError(
                            "Pixiv Cookie 包含无法安全写入 cookies.txt 的字符",
                            "invalid_cookie")
                    domain = cookie.domain or ".pixiv.net"
                    prefix = "#HttpOnly_" if cookie.http_only else ""
                    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
                    secure = "TRUE" if cookie.secure else "FALSE"
                    expires = (int(cookie.expires)
                               if cookie.expires > 0 and math.isfinite(cookie.expires) else 0)
                    handle.write(
                        "%s%s\t%s\t%s\t%s\t%d\t%s\t%s\n" % (
                            prefix, domain, include_subdomains, cookie.path or "/",
                            secure, expires, cookie.name, cookie.value))
                handle.flush()
                os.fsync(handle.fileno())
            yield path
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            _secure_unlink(path)

    def close(self) -> Mapping[str, object]:
        """Close Chrome while preserving its encrypted Pixiv profile."""
        with self._lock:
            connection = self._connection()
            if connection is not None:
                self._shutdown_connection(connection)
            else:
                _stop_owned_process(self._process)
                self._process = None
                self._remove_state()
            return {
                "ok": True,
                "running": False,
                "logged_in": False,
                "message": "专用 Pixiv 浏览器已关闭，登录 Profile 已保留",
            }

    def _connection(self) -> Optional[Mapping[str, object]]:
        connection = super()._connection()
        if connection is None:
            return None
        state = self._read_state() or {}
        mode = str(state.get("mode") or "visible")
        if mode not in ("visible", "headless"):
            return None
        result = dict(connection)
        result["mode"] = mode
        return result

    def _write_state(self, connection: Mapping[str, object]) -> None:
        parent = os.path.dirname(self.state_path)
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="pixiv-browser-state-", suffix=".json", dir=parent)
        try:
            try:
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            payload = {
                "pid": int(connection["pid"]),
                "port": int(connection["port"]),
                "browser_id": str(connection["browser_id"]),
                "profile_dir": os.path.realpath(self.profile_dir),
                "mode": str(connection.get("mode") or "visible"),
            }
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except OSError:
            raise PixivBrowserSessionError(
                "无法保存专用 Pixiv 浏览器状态", "state_write_failed") from None
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def _shutdown_connection(self, connection: Mapping[str, object]) -> None:
        port = int(connection.get("port") or 0)
        pid = int(connection.get("pid") or 0)
        _request_browser_close(str(connection.get("websocket_url") or ""), self.timeout)
        closed = _wait_until_endpoint_closed(port, min(5.0, self.timeout))
        if not closed:
            if os.name == "nt" and pid > 0:
                # The connection was validated against this module's state,
                # browser id, loopback endpoint, and dedicated profile before
                # reaching here.  Never enumerate or target default Chrome.
                _stop_process_tree_pid(pid)
            elif self._process is not None:
                _stop_owned_process_tree(self._process)
            closed = _wait_until_endpoint_closed(port, min(3.0, self.timeout))
        if not closed:
            raise PixivBrowserSessionError(
                "专用 Pixiv 浏览器未能完全退出", "close_failed")
        _stop_owned_process(self._process)
        self._process = None
        self._remove_state()

    def _ensure_pixiv_target(self, websocket_url: str, url: str) -> None:
        try:
            import websocket
            connection = websocket.create_connection(
                websocket_url, timeout=self.timeout, enable_multithread=False)
            try:
                targets = _cdp_call(
                    connection, 301, "Target.getTargets", timeout=self.timeout)
                infos = targets.get("targetInfos") if isinstance(targets, Mapping) else []
                existing = next(
                    (item for item in (infos or []) if isinstance(item, Mapping) and
                     item.get("type") == "page" and
                     _is_pixiv_url(str(item.get("url") or ""))),
                    None)
                if existing and existing.get("targetId"):
                    _cdp_call(
                        connection, 302, "Target.activateTarget",
                        {"targetId": str(existing["targetId"])}, timeout=self.timeout)
                else:
                    _cdp_call(
                        connection, 303, "Target.createTarget", {"url": url},
                        timeout=self.timeout)
            finally:
                connection.close()
        except Exception:
            raise PixivBrowserSessionError(
                "专用 Pixiv 浏览器已运行，但无法打开登录页面",
                "open_login_failed") from None


def _launch_browser(
        executable: str, profile_dir: str, port: int, url: str, *,
        headless: bool):
    args = [
        executable,
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=http://127.0.0.1:%d" % port,
        "--user-data-dir=%s" % profile_dir,
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.extend((
            "--headless=new", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking", "--window-position=-32000,-32000",
            "--window-size=1,1"))
    else:
        args.extend(("--new-window", url))
    if headless:
        args.append(url)
    creationflags = 0
    startupinfo = None
    if headless and os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        # 独立进程组:X 模块 _kill_process_tree 的 killpg 才不会波及应用自身(POSIX;Windows 忽略)
        return subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=creationflags, startupinfo=startupinfo,
            start_new_session=True)
    except OSError:
        raise PixivBrowserSessionError(
            "无法启动专用 Pixiv 浏览器", "launch_failed") from None


def _stop_owned_process_tree(process) -> None:
    """Terminate only the dedicated process tree started by this module."""
    if process is None:
        return
    if os.name == "nt":
        try:
            pid = int(process.pid)
            if pid > 0:
                _stop_process_tree_pid(pid)
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass
                return
        except Exception:
            pass
    _stop_owned_process(process)


def _stop_process_tree_pid(pid: int) -> None:
    """Kill one already-verified dedicated Windows process tree by PID."""
    if os.name != "nt" or int(pid) <= 0:
        return
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=10, check=False,
        creationflags=creationflags)


def _wait_until_endpoint_closed(port: int, timeout: float) -> bool:
    if not port:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        try:
            _fetch_version(port, timeout=0.25)
        except Exception:
            return True
        time.sleep(0.05)
    return False


def _validate_pixiv_url(url: str) -> str:
    value = str(url or DEFAULT_LOGIN_URL).strip()
    if not _is_pixiv_url(value):
        raise PixivBrowserSessionError(
            "专用登录窗口仅允许打开 Pixiv 页面", "invalid_url")
    return value


def _is_pixiv_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and
                host in ("pixiv.net", "www.pixiv.net", "accounts.pixiv.net"))
    except (TypeError, ValueError):
        return False


_DEFAULT_SESSION = PixivBrowserSession()


def open_login_window(url: str = DEFAULT_LOGIN_URL) -> Mapping[str, object]:
    return _DEFAULT_SESSION.open_login(url)


def check_login() -> Mapping[str, object]:
    return _DEFAULT_SESSION.check_login()


def read_phpsessid() -> str:
    return _DEFAULT_SESSION.read_phpsessid()


@contextmanager
def cookie_file(
        domains: Iterable[str] = DEFAULT_DOMAINS,
        required_names: Iterable[str] = ("PHPSESSID",)):
    with _DEFAULT_SESSION.cookie_file(domains, required_names) as path:
        yield path


def close() -> Mapping[str, object]:
    return _DEFAULT_SESSION.close()


__all__ = [
    "PixivBrowserSession", "PixivBrowserSessionError", "open_login_window",
    "check_login", "read_phpsessid", "cookie_file", "close",
]
