# -*- coding: utf-8 -*-
"""Read Chromium cookies through a short-lived, isolated Chrome process.

Chrome's ``v20`` App-Bound Encryption cookies cannot be decrypted by reading
the SQLite database directly.  This module takes a minimal snapshot of a
closed Chromium profile, starts the matching browser against that snapshot,
and asks Chrome itself for the cookies over the DevTools Protocol.

This is intentionally a best-effort compatibility probe, not a promise that a
``v20`` cookie can be recovered.  Current Chrome builds can reject cookies
whose database was copied to a different profile path.  The original profile
is therefore treated as read-only and is never launched, mounted, or passed as
``--user-data-dir``.  When required cookies are absent, callers receive an
explicit App-Bound Encryption compatibility error.

Credential safety is deliberately part of the API contract:

* cookie values are never passed on a command line or included in exceptions;
* returned cookie objects hide their value from ``repr``/``str``;
* Chrome output is redirected to ``DEVNULL``;
* the temporary profile is overwritten and removed on success and failure.

The caller is still responsible for keeping returned values in memory only as
long as necessary.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple


TEMP_PREFIX = "x-chrome-cdp-"
DEFAULT_TIMEOUT = 20.0


class ChromeCookieError(RuntimeError):
    """A sanitized failure raised while extracting browser cookies."""

    def __init__(self, message: str, code: str = "chrome_cookie_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BrowserCookie:
    """A Chromium cookie whose representation intentionally hides its value."""

    name: str
    value: str = field(repr=False)
    domain: str
    path: str = "/"
    expires: float = 0.0
    secure: bool = False
    http_only: bool = False
    same_site: str = ""


@dataclass(frozen=True)
class _BrowserPaths:
    executable: str
    user_data_dir: str


def extract_cookies(
        browser: str = "chrome",
        profile: str = "",
        domains: Iterable[str] = ("x.com",),
        *,
        executable: str = "",
        user_data_dir: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        required_names: Iterable[str] = ()) -> List[BrowserCookie]:
    """Return cookies for *domains* using Chrome's own decryption context.

    ``profile`` is a Chromium profile directory name such as ``Default`` or
    ``Profile 7``.  When omitted, the profile marked ``last_used`` in Local
    State is selected.  ``required_names`` can be used by an integration to
    require cookies such as ``auth_token`` and ``ct0`` without exposing their
    values in an error.

    The source browser must be fully closed while its minimal profile snapshot
    is copied.  The temporary browser is always terminated before cleanup.
    """
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ChromeCookieError("Chrome Cookie 提取超时时间无效", "invalid_timeout") from None
    if timeout <= 0:
        raise ChromeCookieError("Chrome Cookie 提取超时时间必须大于 0", "invalid_timeout")

    target_domains = _normalize_domains(domains)
    required = {str(name).strip() for name in required_names if str(name).strip()}
    paths = _resolve_browser_paths(browser, executable, user_data_dir)
    profile_name = _resolve_profile_name(paths.user_data_dir, profile)

    snapshot_root = ""
    process = None
    cookies = None
    pending_error = None
    cleanup_failed = False

    try:
        snapshot_root = _create_snapshot(paths.user_data_dir, profile_name)
        port = _reserve_local_port()
        process = _launch_chrome(
            paths.executable, snapshot_root, profile_name, port)
        websocket_url = _wait_for_devtools(process, port, timeout)
        raw_cookies = _read_all_cookies(websocket_url, timeout)
        cookies = _target_cookies(raw_cookies, target_domains)

        present = {cookie.name for cookie in cookies}
        missing = sorted(required - present)
        if missing:
            raise ChromeCookieError(
                "临时 Chrome 无法读取所需的 X 会话 Cookie（%s）；当前 Chrome v20 "
                "App-Bound Encryption 可能拒绝复制的 Profile，原 Profile 未被启动或修改"
                % ", ".join(missing),
                "app_bound_cookie_unavailable")
    except ChromeCookieError as exc:
        pending_error = exc
    except Exception:
        # Never interpolate an arbitrary exception here: websocket/protocol
        # exceptions can contain request or response fragments.
        pending_error = ChromeCookieError(
            "Chrome Cookie 提取发生未预期错误", "unexpected_error")
    finally:
        _stop_process(process)
        if snapshot_root:
            cleanup_failed = not _secure_remove_tree(snapshot_root)

    if cleanup_failed:
        raise ChromeCookieError(
            "临时 Chrome 配置未能完全清理；请关闭残留 Chrome 进程后删除临时目录",
            "cleanup_failed") from None
    if pending_error is not None:
        raise pending_error from None
    return cookies or []


def _normalize_domains(domains: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(domains, str):
        domains = (domains,)
    normalized = []
    for domain in domains:
        value = str(domain or "").strip().lower().lstrip(".").rstrip(".")
        if value and value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ChromeCookieError("至少需要一个目标 Cookie 域名", "invalid_domains")
    return tuple(normalized)


def _resolve_browser_paths(
        browser: str, executable: str, user_data_dir: str) -> _BrowserPaths:
    name = str(browser or "chrome").strip().lower()
    if name not in ("chrome", "edge", "brave", "chromium"):
        raise ChromeCookieError(
            "CDP Cookie 提取仅支持 Chrome、Edge、Brave 和 Chromium",
            "unsupported_browser")

    local = os.environ.get("LOCALAPPDATA") or ""
    program_files = os.environ.get("PROGRAMFILES") or ""
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)") or ""

    if os.name == "nt":
        data_home = local
    else:
        data_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")

    roots = {
        "chrome": (
            os.path.join(data_home, "google-chrome"),
            os.path.join(local, "Google", "Chrome", "User Data"),
        ),
        "edge": (
            os.path.join(data_home, "microsoft-edge"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
        ),
        "brave": (
            os.path.join(data_home, "BraveSoftware", "Brave-Browser"),
            os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
        ),
        "chromium": (
            os.path.join(data_home, "chromium"),
            os.path.join(local, "Chromium", "User Data"),
        ),
    }
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
            linux_chromium or shutil.which("chrome") or "",
        ),
    }

    selected_executable = os.path.abspath(os.path.expanduser(executable)) if executable else ""
    if not selected_executable:
        selected_executable = next(
            (os.path.abspath(path) for path in candidates[name] if path and os.path.isfile(path)),
            "")
    if not selected_executable or not os.path.isfile(selected_executable):
        raise ChromeCookieError("未找到浏览器可执行文件", "executable_not_found")

    selected_root = (os.path.abspath(os.path.expanduser(user_data_dir))
                     if user_data_dir else next(
                         (os.path.abspath(path) for path in roots[name]
                          if path and os.path.isdir(path)),
                         ""))
    if not selected_root or not os.path.isdir(selected_root):
        raise ChromeCookieError("未找到浏览器 User Data 目录", "user_data_not_found")
    return _BrowserPaths(selected_executable, selected_root)


def _resolve_profile_name(user_data_dir: str, profile: str) -> str:
    selected = str(profile or "").strip()
    if not selected:
        try:
            with open(os.path.join(user_data_dir, "Local State"), "r", encoding="utf-8") as handle:
                state = json.load(handle)
            selected = str((state.get("profile") or {}).get("last_used") or "").strip()
        except (OSError, ValueError, TypeError, AttributeError):
            selected = ""
    selected = selected or "Default"

    # Profile names are single directory components.  Rejecting separators
    # prevents a caller-controlled path from escaping the intended snapshot.
    if (selected in (".", "..") or os.path.basename(selected) != selected or
            "/" in selected or "\\" in selected):
        raise ChromeCookieError("Chrome Profile 名称无效", "invalid_profile")
    if not os.path.isdir(os.path.join(user_data_dir, selected)):
        raise ChromeCookieError("找不到指定的 Chrome Profile", "profile_not_found")
    return selected


def _create_snapshot(user_data_dir: str, profile_name: str) -> str:
    root = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    try:
        try:
            os.chmod(root, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass

        local_state = os.path.join(user_data_dir, "Local State")
        if not os.path.isfile(local_state):
            raise ChromeCookieError("Chrome Local State 不存在", "local_state_not_found")
        shutil.copy2(local_state, os.path.join(root, "Local State"))

        source_profile = os.path.join(user_data_dir, profile_name)
        target_profile = os.path.join(root, profile_name)
        os.makedirs(target_profile, exist_ok=True)

        optional_files = (
            "Preferences",
            "Secure Preferences",
            os.path.join("Network", "Network Persistent State"),
        )
        for relative in optional_files:
            source = os.path.join(source_profile, relative)
            if os.path.isfile(source):
                target = os.path.join(target_profile, relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(source, target)

        cookie_relative = ""
        for candidate in (os.path.join("Network", "Cookies"), "Cookies"):
            if os.path.isfile(os.path.join(source_profile, candidate)):
                cookie_relative = candidate
                break
        if not cookie_relative:
            raise ChromeCookieError("指定 Profile 中没有 Chrome Cookie 数据库",
                                    "cookie_db_not_found")

        for suffix in ("", "-wal", "-shm", "-journal"):
            relative = cookie_relative + suffix
            source = os.path.join(source_profile, relative)
            if not os.path.isfile(source):
                continue
            target = os.path.join(target_profile, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
        return root
    except ChromeCookieError:
        _secure_remove_tree(root)
        raise
    except (PermissionError, OSError):
        _secure_remove_tree(root)
        raise ChromeCookieError(
            "无法复制 Chrome Cookie 数据库；请完全退出 Chrome 后重试",
            "profile_locked") from None


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _launch_chrome(
        executable: str, snapshot_root: str, profile_name: str, port: int):
    # This invariant is intentionally enforced at the process boundary.  Even
    # if a caller accidentally supplies its live User Data path, this module
    # will not launch Chrome against it.  It also rejects symlinks/junctions
    # resolving outside the private temporary snapshot root.
    if not _is_safe_snapshot_root(snapshot_root):
        raise ChromeCookieError(
            "拒绝启动非临时 Chrome Profile，以免修改原浏览器数据",
            "unsafe_snapshot")
    args = [
        executable,
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=%d" % port,
        "--remote-allow-origins=*",
        "--user-data-dir=%s" % snapshot_root,
        "--profile-directory=%s" % profile_name,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-service-autorun",
        "about:blank",
    ]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError:
        raise ChromeCookieError("无法启动临时 Chrome", "launch_failed") from None


def _wait_for_devtools(process, port: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://127.0.0.1:%d/json/version" % port
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ChromeCookieError("临时 Chrome 在 DevTools 就绪前退出", "launch_failed")
        try:
            with opener.open(url, timeout=min(1.0, max(0.1, deadline - time.monotonic()))) as response:
                payload = json.loads(response.read().decode("utf-8"))
            websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
            if websocket_url.startswith("ws://127.0.0.1:") or websocket_url.startswith(
                    "ws://localhost:"):
                return websocket_url
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        time.sleep(0.1)
    raise ChromeCookieError("等待 Chrome DevTools 超时", "devtools_timeout")


def _read_all_cookies(websocket_url: str, timeout: float) -> Sequence[Mapping[str, object]]:
    try:
        import websocket
    except ImportError:
        raise ChromeCookieError(
            "缺少 websocket-client；请运行 pip install websocket-client",
            "dependency_missing") from None

    connection = None
    try:
        connection = websocket.create_connection(
            websocket_url, timeout=timeout, enable_multithread=False)
        targets = _cdp_call(connection, 1, "Target.getTargets", timeout=timeout)
        target_infos = targets.get("targetInfos") if isinstance(targets, Mapping) else None
        page_target = next(
            (item for item in (target_infos or [])
             if isinstance(item, Mapping) and item.get("type") == "page" and
             item.get("targetId")), None)
        if page_target is None:
            created = _cdp_call(
                connection, 2, "Target.createTarget", {"url": "about:blank"}, timeout=timeout)
            target_id = str(created.get("targetId") or "")
        else:
            target_id = str(page_target.get("targetId") or "")
        if not target_id:
            raise ChromeCookieError("Chrome DevTools 没有可用页面", "cdp_target_missing")

        attached = _cdp_call(
            connection, 3, "Target.attachToTarget",
            {"targetId": target_id, "flatten": True}, timeout=timeout)
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            raise ChromeCookieError("无法连接 Chrome 页面会话", "cdp_attach_failed")

        _cdp_call(connection, 4, "Network.enable", timeout=timeout, session_id=session_id)
        result = _cdp_call(
            connection, 5, "Network.getAllCookies", timeout=timeout,
            session_id=session_id)
        cookies = result.get("cookies") if isinstance(result, Mapping) else None
        if not isinstance(cookies, list):
            raise ChromeCookieError("Chrome 未返回有效 Cookie 列表", "cdp_invalid_response")
        return cookies
    except ChromeCookieError:
        raise
    except Exception:
        # Do not include the websocket exception text; it can contain protocol
        # frames and therefore potentially credential material.
        raise ChromeCookieError("Chrome DevTools Cookie 读取失败", "cdp_failed") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _cdp_call(
        connection, request_id: int, method: str,
        params: Optional[Mapping[str, object]] = None, *, timeout: float,
        session_id: str = "") -> Mapping[str, object]:
    request = {"id": request_id, "method": method}
    if params:
        request["params"] = dict(params)
    if session_id:
        request["sessionId"] = session_id
    connection.send(json.dumps(request, separators=(",", ":")))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            message = json.loads(connection.recv())
        except Exception:
            raise ChromeCookieError("Chrome DevTools 通信失败", "cdp_failed") from None
        if not isinstance(message, Mapping) or message.get("id") != request_id:
            continue
        if message.get("error"):
            # CDP error objects are intentionally not interpolated here.
            raise ChromeCookieError(
                "Chrome DevTools 不支持所需的 %s 操作" % method,
                "cdp_method_failed")
        result = message.get("result")
        return result if isinstance(result, Mapping) else {}
    raise ChromeCookieError("Chrome DevTools 操作超时", "cdp_timeout")


def _target_cookies(
        raw_cookies: Sequence[Mapping[str, object]],
        target_domains: Sequence[str]) -> List[BrowserCookie]:
    result = []
    for item in raw_cookies:
        if not isinstance(item, Mapping):
            continue
        domain = str(item.get("domain") or "").strip().lower()
        host = domain.lstrip(".")
        if not any(host == target or host.endswith("." + target)
                   for target in target_domains):
            continue
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            continue
        try:
            expires = float(item.get("expires") or 0.0)
        except (TypeError, ValueError):
            expires = 0.0
        result.append(BrowserCookie(
            name=name,
            value=value,
            domain=domain,
            path=str(item.get("path") or "/"),
            expires=expires,
            secure=bool(item.get("secure")),
            http_only=bool(item.get("httpOnly")),
            same_site=str(item.get("sameSite") or ""),
        ))
    return result


def _stop_process(process) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _secure_unlink(path: str) -> None:
    try:
        if os.path.islink(path):
            os.unlink(path)
            return
        size = os.path.getsize(path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with open(path, "r+b", buffering=0) as handle:
            block = b"\0" * 65536
            remaining = size
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


def _is_safe_snapshot_root(path: str) -> bool:
    try:
        root = os.path.realpath(os.path.abspath(path))
        temp_root = os.path.realpath(os.path.abspath(tempfile.gettempdir()))
        return (os.path.commonpath((root, temp_root)) == temp_root and
                os.path.basename(root).startswith(TEMP_PREFIX) and root != temp_root)
    except (OSError, ValueError):
        return False


def _secure_remove_tree(root: str) -> bool:
    """Best-effort overwrite and deletion of a known temporary snapshot."""
    if not root or not os.path.exists(root):
        return True
    if not _is_safe_snapshot_root(root):
        return False

    for _attempt in range(3):
        try:
            for current, directories, files in os.walk(root, topdown=False):
                for filename in files:
                    _secure_unlink(os.path.join(current, filename))
                for dirname in directories:
                    path = os.path.join(current, dirname)
                    try:
                        if os.path.islink(path):
                            os.unlink(path)
                        else:
                            os.rmdir(path)
                    except OSError:
                        pass
            try:
                os.rmdir(root)
            except OSError:
                shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass
        if not os.path.exists(root):
            return True
        time.sleep(0.1)
    return not os.path.exists(root)


__all__ = ["BrowserCookie", "ChromeCookieError", "extract_cookies"]
