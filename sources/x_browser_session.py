# -*- coding: utf-8 -*-
"""Managed, persistent browser session dedicated to X authentication.

Unlike browser-cookie database importers, this module never copies or starts a
user's normal Chrome profile.  It owns a separate persistent User Data
directory under LOCALAPPDATA, opens a visible browser for interactive X login,
and reads cookies from that live browser through loopback-only CDP.

No cookie value is returned by status functions, written to the state file,
included in exceptions, or exposed through object representations.  The only
on-disk plaintext credential is a short-lived Netscape cookie file created by
``cookie_file()`` and overwritten on context exit.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
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


DEFAULT_LOGIN_URL = "https://x.com/home"
DEFAULT_TIMEOUT = 20.0
STATE_FILENAME = "XBrowserSession.json"


class XBrowserSessionError(RuntimeError):
    """A sanitized managed-browser failure."""

    def __init__(self, message: str, code: str = "x_browser_error"):
        super().__init__(message)
        self.code = code


class XBrowserSession:
    """Manage one visible Chrome instance backed by a dedicated profile."""

    def __init__(
            self, profile_dir: str = "", browser: str = "chrome",
            executable: str = "", timeout: float = DEFAULT_TIMEOUT):
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        else:
            local = os.environ.get("XDG_DATA_HOME") or os.path.join(
                os.path.expanduser("~"), ".local", "share")
        app_dir = os.path.join(local, "DanbooruGrabber")
        self.profile_dir = os.path.abspath(os.path.expanduser(
            profile_dir or os.path.join(app_dir, "XBrowserProfile")))
        self.state_path = os.path.join(os.path.dirname(self.profile_dir), STATE_FILENAME)
        self.browser = str(browser or "chrome").strip().lower()
        self.executable = os.path.abspath(os.path.expanduser(executable)) if executable else ""
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            raise XBrowserSessionError("专用 X 浏览器超时时间无效", "invalid_timeout") from None
        if self.timeout <= 0:
            raise XBrowserSessionError("专用 X 浏览器超时时间必须大于 0", "invalid_timeout")
        self._process = None
        self._lock = threading.RLock()

    def launch(
            self, url: str = DEFAULT_LOGIN_URL, *,
            headless: bool = False) -> Mapping[str, object]:
        """Start or reuse the dedicated browser in visible/headless mode."""
        safe_url = _validate_x_url(url)
        with self._lock:
            connection = self._connection()
            if connection is not None:
                if bool(connection.get("headless")) == bool(headless):
                    self._ensure_x_target(connection["websocket_url"], safe_url)
                    message = "X 后台会话已运行" if headless else "专用 X 浏览器已打开"
                    return self._status_dict(True, connection, message)
                self._shutdown_connection(connection)

            self._remove_state()
            executable = self.executable or _find_browser_executable(self.browser)
            os.makedirs(self.profile_dir, exist_ok=True)
            try:
                os.chmod(
                    self.profile_dir,
                    stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                pass

            port = _reserve_local_port()
            launcher = _launch_headless_browser if headless else _launch_visible_browser
            process = launcher(executable, self.profile_dir, port, safe_url)
            self._process = process
            try:
                websocket_url = _wait_for_endpoint(process, port, self.timeout)
                connection = {
                    "pid": int(process.pid),
                    "port": port,
                    "browser_id": _browser_id(websocket_url),
                    "websocket_url": websocket_url,
                    "headless": bool(headless),
                }
                self._write_state(connection)
                message = "X 后台会话已恢复" if headless else "请在专用浏览器中登录 X"
                return self._status_dict(True, connection, message)
            except Exception:
                _stop_owned_process(process)
                self._process = None
                self._remove_state()
                raise

    def open_login(self, url: str = DEFAULT_LOGIN_URL) -> Mapping[str, object]:
        """Alias used by UI integrations."""
        return self.launch(url, headless=False)

    def check_login(self) -> Mapping[str, object]:
        """Check the persistent login, leaving no background Chrome running."""
        with self._lock:
            try:
                connection, restored = self._connection_with_autostart()
            except XBrowserSessionError as exc:
                return {
                    "ok": False,
                    "running": False,
                    "logged_in": False,
                    "message": str(exc),
                    "code": exc.code,
                }
            try:
                raw = _read_all_cookies(connection["websocket_url"], self.timeout)
                cookies = _target_cookies(raw, _normalize_domains(("x.com",)))
            except Exception:
                self.close()
                return {
                    "ok": False, "running": False, "logged_in": False,
                    "message": "无法检查专用 X 登录会话", "code": "cookie_read_failed",
                }
            names = {cookie.name for cookie in cookies}
            logged_in = "auth_token" in names and "ct0" in names
            if logged_in:
                self.close()
                return {
                    "ok": True, "running": False, "logged_in": True,
                    "message": "X 登录已保存；抓取时将按需启动并自动退出",
                }
            if restored or bool(connection.get("headless")):
                self.close()
                try:
                    self.launch(DEFAULT_LOGIN_URL, headless=False)
                except XBrowserSessionError as exc:
                    return {
                        "ok": False, "running": False, "logged_in": False,
                        "message": str(exc), "code": exc.code,
                    }
            return {
                "ok": True, "running": True, "logged_in": False,
                "message": "请在专用浏览器中完成 X 登录",
            }

    def read_cookies(
            self, domains: Iterable[str] = ("x.com",),
            required_names: Iterable[str] = ("auth_token", "ct0")) -> List[BrowserCookie]:
        """Read target-domain cookies from the live dedicated browser."""
        target_domains = _normalize_domains(domains)
        required = {str(name).strip() for name in required_names if str(name).strip()}
        with self._lock:
            connection, restored = self._connection_with_autostart()
            try:
                raw = _read_all_cookies(connection["websocket_url"], self.timeout)
                cookies = _target_cookies(raw, target_domains)
            except ChromeCookieError:
                if restored or bool(connection.get("headless")):
                    self.close()
                raise XBrowserSessionError(
                    "无法从专用 X 浏览器读取登录会话", "cookie_read_failed") from None
            except Exception:
                if restored or bool(connection.get("headless")):
                    self.close()
                raise XBrowserSessionError(
                    "专用 X 浏览器 Cookie 读取失败", "cookie_read_failed") from None
            present = {cookie.name for cookie in cookies}
            missing = sorted(required - present)
            if missing:
                if restored or bool(connection.get("headless")):
                    self.close()
                    self.launch(DEFAULT_LOGIN_URL, headless=False)
                raise XBrowserSessionError(
                    "专用浏览器尚未完成 X 登录，缺少会话 Cookie：%s"
                    % ", ".join(missing),
                    "not_logged_in")
            # Cookie values are now in memory.  The browser process is no
            # longer needed and must not remain resident in the background.
            self.close()
            return cookies

    def _connection_with_autostart(self):
        """Return the live connection, reopening the same persistent profile."""
        connection = self._connection()
        if connection is not None:
            return connection, False
        self.launch(DEFAULT_LOGIN_URL, headless=True)
        connection = self._connection()
        if connection is None:
            raise XBrowserSessionError(
                "专用 X 浏览器无法自动恢复", "auto_restore_failed")
        return connection, True

    def _shutdown_connection(self, connection: Mapping[str, object]) -> None:
        """Stop the current managed mode without deleting the persistent profile."""
        _request_browser_close(connection["websocket_url"], self.timeout)
        if not _wait_for_shutdown(int(connection.get("port") or 0), 3.0):
            _kill_process_tree(int(connection.get("pid") or 0))
            _wait_for_shutdown(int(connection.get("port") or 0), 3.0)
        _stop_owned_process(self._process)
        self._process = None
        self._remove_state()
        _wait_for_shutdown(int(connection.get("port") or 0), min(self.timeout, 8.0))

    @contextmanager
    def cookie_file(
            self, domains: Iterable[str] = ("x.com",),
            required_names: Iterable[str] = ("auth_token", "ct0")):
        """Yield a temporary Netscape cookie file and securely delete it."""
        cookies = self.read_cookies(domains, required_names)
        fd, path = tempfile.mkstemp(prefix="x-managed-session-", suffix=".cookies.txt")
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
                        raise XBrowserSessionError(
                            "X Cookie 包含无法安全写入 cookies.txt 的字符",
                            "invalid_cookie")
                    domain = cookie.domain or ".x.com"
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
        """Close the managed browser while preserving its dedicated profile."""
        with self._lock:
            connection = self._connection()
            if connection is not None:
                _request_browser_close(connection["websocket_url"], self.timeout)
                if not _wait_for_shutdown(int(connection.get("port") or 0), 3.0):
                    _kill_process_tree(int(connection.get("pid") or 0))
                    _wait_for_shutdown(int(connection.get("port") or 0), 3.0)
            _stop_owned_process(self._process)
            self._process = None
            self._remove_state()
            return {
                "ok": True,
                "running": False,
                "logged_in": False,
                "message": "专用 X 浏览器已关闭，登录 Profile 已保留",
            }

    def _connection(self) -> Optional[Mapping[str, object]]:
        state = self._read_state()
        if state is None:
            return None
        try:
            version = _fetch_version(int(state["port"]), timeout=1.5)
            websocket_url = str(version.get("webSocketDebuggerUrl") or "")
            if (_browser_id(websocket_url) != state.get("browser_id") or
                    not websocket_url.startswith(("ws://127.0.0.1:", "ws://localhost:"))):
                return None
            return {
                "pid": int(state.get("pid") or 0),
                "port": int(state["port"]),
                "browser_id": str(state["browser_id"]),
                "websocket_url": websocket_url,
                "headless": bool(state.get("headless")),
            }
        except (OSError, ValueError, TypeError, KeyError, XBrowserSessionError):
            return None

    def _ensure_x_target(self, websocket_url: str, url: str) -> None:
        try:
            import websocket
            connection = websocket.create_connection(
                websocket_url, timeout=self.timeout, enable_multithread=False)
            try:
                targets = _cdp_call(
                    connection, 101, "Target.getTargets", timeout=self.timeout)
                infos = targets.get("targetInfos") if isinstance(targets, Mapping) else []
                existing = next(
                    (item for item in (infos or []) if isinstance(item, Mapping) and
                     item.get("type") == "page" and _is_x_url(str(item.get("url") or ""))),
                    None)
                if existing and existing.get("targetId"):
                    _cdp_call(
                        connection, 102, "Target.activateTarget",
                        {"targetId": str(existing["targetId"])}, timeout=self.timeout)
                else:
                    _cdp_call(
                        connection, 103, "Target.createTarget", {"url": url},
                        timeout=self.timeout)
            finally:
                connection.close()
        except Exception:
            raise XBrowserSessionError(
                "专用 X 浏览器已运行，但无法打开登录页面", "open_login_failed") from None

    def _read_state(self) -> Optional[Mapping[str, object]]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, Mapping):
                return None
            port = int(state.get("port") or 0)
            pid = int(state.get("pid") or 0)
            browser_id = str(state.get("browser_id") or "")
            profile_dir = os.path.realpath(str(state.get("profile_dir") or ""))
            if (not 1 <= port <= 65535 or pid <= 0 or not browser_id or
                    profile_dir != os.path.realpath(self.profile_dir)):
                return None
            return state
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _write_state(self, connection: Mapping[str, object]) -> None:
        parent = os.path.dirname(self.state_path)
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="x-browser-state-", suffix=".json", dir=parent)
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
                "headless": bool(connection.get("headless")),
            }
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except OSError:
            raise XBrowserSessionError(
                "无法保存专用 X 浏览器状态", "state_write_failed") from None
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

    def _remove_state(self) -> None:
        try:
            os.remove(self.state_path)
        except OSError:
            pass

    def _status_dict(
            self, ok: bool, connection: Mapping[str, object], message: str,
            *, logged_in: Optional[bool] = None, code: str = "") -> Mapping[str, object]:
        result = {
            "ok": bool(ok),
            "running": True,
            "message": message,
            "pid": int(connection.get("pid") or 0),
            "port": int(connection.get("port") or 0),
            "profile_dir": self.profile_dir,
            "headless": bool(connection.get("headless")),
        }
        if logged_in is not None:
            result["logged_in"] = bool(logged_in)
        if code:
            result["code"] = code
        return result


def _find_browser_executable(browser: str) -> str:
    name = str(browser or "chrome").strip().lower()
    if name not in ("chrome", "edge", "brave", "chromium"):
        raise XBrowserSessionError("不支持该专用浏览器类型", "unsupported_browser")
    local = os.environ.get("LOCALAPPDATA") or ""
    program_files = os.environ.get("PROGRAMFILES") or ""
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)") or ""
    linux_chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or ""
    linux_chromium = shutil.which("chromium-browser") or shutil.which("chromium") or ""
    candidates = {
        "chrome": (
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/opt/google/chrome/chrome",
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            linux_chrome or shutil.which("chrome") or "",
        ),
        "edge": (
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/microsoft-edge",
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
            shutil.which("microsoft-edge") or shutil.which("msedge") or "",
        ),
        "brave": (
            "/usr/bin/brave-browser",
            os.path.join(program_files, "BraveSoftware", "Brave-Browser", "Application",
                         "brave.exe"),
            os.path.join(program_files_x86, "BraveSoftware", "Brave-Browser", "Application",
                         "brave.exe"),
            os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            shutil.which("brave-browser") or shutil.which("brave") or "",
        ),
        "chromium": (
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            os.path.join(program_files, "Chromium", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Chromium", "Application", "chrome.exe"),
            os.path.join(local, "Chromium", "Application", "chrome.exe"),
            linux_chromium or "",
        ),
    }
    executable = next(
        (os.path.abspath(path) for path in candidates[name] if path and os.path.isfile(path)), "")
    if not executable:
        raise XBrowserSessionError("未找到可用于专用 X 会话的浏览器", "executable_not_found")
    return executable


def _launch_visible_browser(executable: str, profile_dir: str, port: int, url: str):
    args = [
        executable,
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=http://127.0.0.1:%d" % port,
        "--user-data-dir=%s" % profile_dir,
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    try:
        return subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        raise XBrowserSessionError("无法启动专用 X 浏览器", "launch_failed") from None


def _launch_headless_browser(executable: str, profile_dir: str, port: int, url: str):
    args = [
        executable,
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=*",
        "--user-data-dir=%s" % profile_dir,
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--window-position=-32000,-32000",
        "--window-size=1,1",
        url,
    ]
    creationflags = 0
    startupinfo = None
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        return subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=creationflags, startupinfo=startupinfo)
    except OSError:
        raise XBrowserSessionError("无法启动 X 后台浏览器", "launch_failed") from None


def _fetch_version(port: int, timeout: float) -> Mapping[str, object]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://127.0.0.1:%d/json/version" % int(port)
    with opener.open(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise XBrowserSessionError("专用浏览器 DevTools 响应无效", "invalid_devtools")
    return payload


def _wait_for_endpoint(process, port: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise XBrowserSessionError("专用 X 浏览器启动后立即退出", "launch_failed")
        try:
            websocket_url = str(
                _fetch_version(port, min(1.0, max(0.1, deadline - time.monotonic()))).get(
                    "webSocketDebuggerUrl") or "")
            if websocket_url.startswith(("ws://127.0.0.1:", "ws://localhost:")):
                return websocket_url
        except (OSError, ValueError, TypeError, XBrowserSessionError):
            pass
        time.sleep(0.1)
    raise XBrowserSessionError("等待专用 X 浏览器启动超时", "launch_timeout")


def _wait_for_shutdown(port: int, timeout: float) -> bool:
    if not port:
        return True
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        try:
            _fetch_version(port, min(0.4, max(0.1, deadline - time.monotonic())))
        except Exception:
            return True
        time.sleep(0.1)
    return False


def _browser_id(websocket_url: str) -> str:
    marker = "/devtools/browser/"
    value = str(websocket_url or "")
    if marker not in value:
        raise XBrowserSessionError("专用浏览器 DevTools 标识无效", "invalid_devtools")
    browser_id = value.rsplit(marker, 1)[1].strip("/")
    if not browser_id or any(char in browser_id for char in ("/", "?", "#")):
        raise XBrowserSessionError("专用浏览器 DevTools 标识无效", "invalid_devtools")
    return browser_id


def _request_browser_close(websocket_url: str, timeout: float) -> None:
    try:
        import websocket
        connection = websocket.create_connection(
            websocket_url, timeout=timeout, enable_multithread=False)
        try:
            # Browser.close commonly closes the socket before its response;
            # sending the command is sufficient and avoids exposing errors.
            connection.send(json.dumps({"id": 201, "method": "Browser.close"}))
        finally:
            connection.close()
    except Exception:
        pass


def _stop_owned_process(process) -> None:
    if process is None:
        return
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    _kill_process_tree(getattr(process, "pid", 0))
    try:
        process.wait(timeout=3)
    except Exception:
        pass


def _kill_process_tree(pid: int) -> None:
    """Kill only the validated/owned managed Chrome process tree."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=8,
                creationflags=creationflags, check=False)
        except Exception:
            pass
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _validate_x_url(url: str) -> str:
    value = str(url or DEFAULT_LOGIN_URL).strip()
    if not _is_x_url(value):
        raise XBrowserSessionError("专用登录窗口仅允许打开 X 页面", "invalid_url")
    return value


def _is_x_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and
                (host in ("x.com", "twitter.com") or
                 host.endswith(".x.com") or host.endswith(".twitter.com")))
    except (TypeError, ValueError):
        return False


def _has_cookie_control_chars(cookie: BrowserCookie) -> bool:
    fields = (cookie.name, cookie.value, cookie.domain, cookie.path)
    return any(any(char in str(value) for char in ("\r", "\n", "\t", "\0"))
               for value in fields)


_DEFAULT_SESSION = XBrowserSession()


def open_login_window(url: str = DEFAULT_LOGIN_URL) -> Mapping[str, object]:
    return _DEFAULT_SESSION.open_login(url)


def check_login() -> Mapping[str, object]:
    return _DEFAULT_SESSION.check_login()


@contextmanager
def cookie_file(
        domains: Iterable[str] = ("x.com",),
        required_names: Iterable[str] = ("auth_token", "ct0")):
    with _DEFAULT_SESSION.cookie_file(domains, required_names) as path:
        yield path


def close() -> Mapping[str, object]:
    return _DEFAULT_SESSION.close()


__all__ = [
    "XBrowserSession", "XBrowserSessionError", "open_login_window",
    "check_login", "cookie_file", "close",
]
