# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

from sources.chrome_cookies import BrowserCookie
from sources import x_browser_session
from sources.x_browser_session import XBrowserSession, XBrowserSessionError


class XBrowserSessionTests(unittest.TestCase):
    def test_default_profile_is_dedicated_and_outside_workspace(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}):
            session = XBrowserSession()
        normalized = session.profile_dir.replace("\\", "/")
        self.assertTrue(normalized.endswith("DanbooruGrabber/XBrowserProfile"))
        self.assertNotIn("Google/Chrome/User Data", normalized)

    def test_find_browser_falls_back_to_chromium_when_chrome_missing(self):
        chromium = os.path.abspath("/usr/bin/chromium")
        with mock.patch.object(
                x_browser_session.os.path, "isfile",
                side_effect=lambda path: path == "/usr/bin/chromium"), \
                mock.patch.object(x_browser_session.shutil, "which", return_value=""):
            executable = x_browser_session._find_browser_executable("chrome")
        self.assertEqual(executable, chromium)

    def test_find_browser_error_includes_install_hint(self):
        with mock.patch.object(x_browser_session.os.path, "isfile", return_value=False), \
                mock.patch.object(x_browser_session.shutil, "which", return_value=""):
            with self.assertRaises(XBrowserSessionError) as ctx:
                x_browser_session._find_browser_executable("chrome")
        self.assertEqual(ctx.exception.code, "executable_not_found")
        self.assertIn("Chromium", str(ctx.exception))

    @mock.patch.object(x_browser_session, "_wait_for_endpoint",
                       return_value="ws://127.0.0.1:9222/devtools/browser/managed-id")
    @mock.patch.object(x_browser_session, "_launch_visible_browser")
    @mock.patch.object(x_browser_session, "_reserve_local_port", return_value=9222)
    @mock.patch.object(x_browser_session, "_find_browser_executable",
                       return_value="C:/Chrome/chrome.exe")
    @mock.patch.object(x_browser_session.os, "chmod")
    @mock.patch.object(x_browser_session.os, "makedirs")
    def test_launch_uses_only_dedicated_profile(
            self, _makedirs, _chmod, _find, _port, launch, _wait):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        process = mock.Mock(pid=321)
        launch.return_value = process
        with mock.patch.object(session, "_connection", return_value=None), \
             mock.patch.object(session, "_remove_state"), \
             mock.patch.object(session, "_write_state") as write_state:
            status = session.launch()

        launch.assert_called_once_with(
            "C:/Chrome/chrome.exe", session.profile_dir, 9222, "https://x.com/home")
        self.assertTrue(status["running"])
        write_state.assert_called_once()

    @mock.patch.object(x_browser_session, "_launch_visible_browser")
    def test_existing_managed_session_is_reused(self, launch):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        connection = {
            "pid": 321, "port": 9222, "browser_id": "managed-id",
            "websocket_url": "ws://127.0.0.1:9222/devtools/browser/managed-id",
        }
        with mock.patch.object(session, "_connection", return_value=connection), \
             mock.patch.object(session, "_ensure_x_target") as ensure:
            status = session.launch()
        self.assertTrue(status["running"])
        ensure.assert_called_once()
        launch.assert_not_called()

    def test_read_cookies_uses_live_cdp_and_filters_x(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        connection = {
            "websocket_url": "ws://127.0.0.1:9222/devtools/browser/managed-id",
        }
        raw = [
            {"name": "auth_token", "value": "fake-auth", "domain": ".x.com"},
            {"name": "ct0", "value": "fake-csrf", "domain": ".x.com"},
            {"name": "SID", "value": "fake-other", "domain": ".google.com"},
        ]
        with mock.patch.object(session, "_connection", return_value=connection), \
             mock.patch.object(x_browser_session, "_read_all_cookies", return_value=raw):
            cookies = session.read_cookies()
        self.assertEqual([cookie.name for cookie in cookies], ["auth_token", "ct0"])
        self.assertNotIn("fake-auth", repr(cookies[0]))

    def test_check_login_never_returns_cookie_values(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        connection = {
            "pid": 321, "port": 9222, "browser_id": "managed-id",
            "websocket_url": "ws://127.0.0.1:9222/devtools/browser/managed-id",
            "headless": True,
        }
        cookies = [
            {"name": "auth_token", "value": "fake-auth", "domain": ".x.com"},
            {"name": "ct0", "value": "fake-csrf", "domain": ".x.com"},
        ]
        with mock.patch.object(session, "_connection", return_value=connection), \
             mock.patch.object(x_browser_session, "_read_all_cookies", return_value=cookies), \
             mock.patch.object(session, "close"):
            status = session.check_login()
        self.assertTrue(status["logged_in"])
        self.assertNotIn("fake-auth", repr(status))
        self.assertNotIn("fake-csrf", repr(status))

    def test_check_login_reopens_persistent_profile_automatically(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        connection = {
            "pid": 321, "port": 9222, "browser_id": "managed-id",
            "websocket_url": "ws://127.0.0.1:9222/devtools/browser/managed-id",
            "headless": True,
        }
        cookies = [
            {"name": "auth_token", "value": "fake-auth", "domain": ".x.com"},
            {"name": "ct0", "value": "fake-csrf", "domain": ".x.com"},
        ]
        with mock.patch.object(
                session, "_connection", side_effect=[None, connection]), \
             mock.patch.object(session, "launch") as launch, \
             mock.patch.object(x_browser_session, "_read_all_cookies", return_value=cookies), \
             mock.patch.object(session, "close") as close:
            status = session.check_login()
        launch.assert_called_once_with("https://x.com/home", headless=True)
        close.assert_called_once()
        self.assertTrue(status["logged_in"])
        self.assertIn("按需", status["message"])

    def test_logged_in_visible_session_closes_instead_of_staying_headless(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        visible = {
            "pid": 321, "port": 9222, "browser_id": "managed-id",
            "websocket_url": "ws://127.0.0.1:9222/devtools/browser/managed-id",
            "headless": False,
        }
        cookies = [
            {"name": "auth_token", "value": "fake-auth", "domain": ".x.com"},
            {"name": "ct0", "value": "fake-csrf", "domain": ".x.com"},
        ]
        with mock.patch.object(
                session, "_connection", return_value=visible), \
             mock.patch.object(x_browser_session, "_read_all_cookies", return_value=cookies), \
             mock.patch.object(session, "close") as close, \
             mock.patch.object(session, "launch") as launch:
            status = session.check_login()
        launch.assert_not_called()
        close.assert_called_once()
        self.assertTrue(status["logged_in"])
        self.assertFalse(status["running"])
        self.assertIn("按需", status["message"])

    def test_cookie_file_is_temporary_netscape_file(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        cookies = [
            BrowserCookie(
                "auth_token", "fake-auth", ".x.com", secure=True, http_only=True),
            BrowserCookie("ct0", "fake-csrf", ".x.com", secure=True),
        ]
        with mock.patch.object(session, "read_cookies", return_value=cookies):
            with session.cookie_file() as path:
                self.assertTrue(os.path.isfile(path))
                with open(path, "r", encoding="utf-8") as handle:
                    content = handle.read()
                self.assertIn("# Netscape HTTP Cookie File", content)
                self.assertIn("auth_token", content)
            self.assertFalse(os.path.exists(path))

    def test_close_requests_browser_shutdown_and_keeps_profile(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        connection = {
            "websocket_url": "ws://127.0.0.1:9222/devtools/browser/managed-id",
        }
        process = mock.Mock()
        session._process = process
        with mock.patch.object(session, "_connection", return_value=connection), \
             mock.patch.object(session, "_remove_state") as remove_state, \
             mock.patch.object(x_browser_session, "_request_browser_close") as request_close, \
             mock.patch.object(x_browser_session, "_stop_owned_process") as stop:
            status = session.close()
        request_close.assert_called_once()
        stop.assert_called_once_with(process)
        remove_state.assert_called_once()
        self.assertFalse(status["running"])

    def test_connection_rejects_reused_port_with_different_browser_id(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        state = {
            "pid": 321, "port": 9222, "browser_id": "expected-id",
            "profile_dir": session.profile_dir,
        }
        with mock.patch.object(session, "_read_state", return_value=state), \
             mock.patch.object(x_browser_session, "_fetch_version", return_value={
                 "webSocketDebuggerUrl":
                     "ws://127.0.0.1:9222/devtools/browser/different-id",
             }):
            self.assertIsNone(session._connection())

    @mock.patch.object(x_browser_session.subprocess, "Popen")
    def test_visible_launch_has_no_headless_flag(self, popen):
        popen.return_value = mock.Mock()
        x_browser_session._launch_visible_browser(
            "C:/Chrome/chrome.exe", "C:/Managed/XBrowserProfile", 9222,
            "https://x.com/home")
        args = popen.call_args.args[0]
        self.assertFalse(any("headless" in arg for arg in args))
        self.assertIn("--user-data-dir=C:/Managed/XBrowserProfile", args)

    @mock.patch.object(x_browser_session.subprocess, "Popen")
    def test_background_launch_uses_headless_and_hidden_process(self, popen):
        popen.return_value = mock.Mock()
        x_browser_session._launch_headless_browser(
            "C:/Chrome/chrome.exe", "C:/Managed/XBrowserProfile", 9222,
            "https://x.com/home")
        args = popen.call_args.args[0]
        self.assertIn("--headless=new", args)
        self.assertIn("--user-data-dir=C:/Managed/XBrowserProfile", args)

    def test_non_x_login_url_is_rejected(self):
        session = XBrowserSession(profile_dir="C:/Managed/XBrowserProfile")
        with self.assertRaises(XBrowserSessionError) as raised:
            session.launch("https://example.com/")
        self.assertEqual(raised.exception.code, "invalid_url")


if __name__ == "__main__":
    unittest.main()
