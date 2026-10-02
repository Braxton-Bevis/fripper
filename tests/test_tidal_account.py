"""Account lifecycle tests use dummy tokens and mocked OAuth, never live sign-in."""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tidalapi
import tidal_account as account


def fake_session():
    return SimpleNamespace(
        token_type="Bearer", access_token="DUMMY-ACCESS-ONLY", refresh_token="DUMMY-REFRESH-ONLY",
        expiry_time=datetime(2030, 1, 2, 3, 4, 5), is_pkce=False,
        check_login=Mock(return_value=True), load_oauth_session=Mock(return_value=True),
        request_session=SimpleNamespace(close=Mock(), authorization_deadline=None,
                                        authorization_cancelled=threading.Event()),
    )


def fake_protect(raw):
    return b"TEST-ENCRYPTED:" + raw[::-1]


def fake_unprotect(raw):
    if not raw.startswith(b"TEST-ENCRYPTED:"):
        raise ValueError("invalid dummy ciphertext")
    return raw[len(b"TEST-ENCRYPTED:"):][::-1]


class SessionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "session.bin"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"TIDAL_DESKTOP_HOME": self.temporary.name}).start()
        patch.object(account, "protect", side_effect=fake_protect).start()
        patch.object(account, "unprotect", side_effect=fake_unprotect).start()

    def test_environment_path_and_encrypted_atomic_write(self):
        session = fake_session()
        account.save_session(session)
        self.assertEqual(account.session_path(), self.path)
        stored = self.path.read_bytes()
        self.assertTrue(stored.startswith(account._MAGIC))
        self.assertNotIn(session.access_token.encode(), stored)
        self.assertNotIn(session.refresh_token.encode(), stored)
        self.assertEqual(list(Path(self.temporary.name).glob("*.tmp")), [])
        data = account._read_session()
        self.assertEqual(data["access_token"], session.access_token)
        self.assertEqual(data["refresh_token"], session.refresh_token)
        self.assertEqual(data["expiry_time"], session.expiry_time)
        self.assertIs(data["is_pkce"], False)

    def test_aware_expiry_is_restored_as_naive_utc(self):
        session = fake_session()
        session.expiry_time = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        account.save_session(session)
        self.assertEqual(account._read_session()["expiry_time"], datetime(2030, 1, 2, 3, 4, 5))

    def test_missing_session_is_actionable(self):
        with self.assertRaisesRegex(account.AccountError, "Use Connect TIDAL"):
            account.load_session()

    def test_plaintext_or_corrupt_data_is_never_accepted(self):
        for value in (b'{"access_token":"DUMMY"}', account._MAGIC + b"invalid"):
            self.path.write_bytes(value)
            with self.subTest(value=value), self.assertRaisesRegex(account.AccountError, "securely"):
                account._read_session()

    def test_invalid_schema_is_rejected_without_echoing_token(self):
        data = {"version": 1, "token_type": "Bearer", "access_token": "DUMMY-PRIVATE"}
        self.path.write_bytes(account._MAGIC + fake_protect(json.dumps(data).encode()))
        with self.assertRaises(account.AccountError) as caught:
            account._read_session()
        self.assertNotIn("DUMMY-PRIVATE", str(caught.exception))

    def test_failed_replace_preserves_prior_session_and_cleans_temp_file(self):
        account.save_session(fake_session())
        old = self.path.read_bytes()
        with patch.object(account.os, "replace", side_effect=PermissionError("sensitive path")), \
                self.assertRaisesRegex(account.AccountError, "folder permissions"):
            account.save_session(fake_session())
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(list(Path(self.temporary.name).glob("*.tmp")), [])

    def test_protection_failure_does_not_write_plaintext(self):
        with patch.object(account, "protect", side_effect=account.AccountError("DPAPI unavailable")), \
                self.assertRaises(account.AccountError):
            account.save_session(fake_session())
        self.assertFalse(self.path.exists())

    def test_load_verifies_login_and_saves_refreshed_tokens(self):
        account.save_session(fake_session())
        refreshed = fake_session()
        refreshed.access_token = "DUMMY-NEW-ACCESS"
        refreshed.refresh_token = "DUMMY-NEW-REFRESH"
        with patch.object(account, "create_session", return_value=refreshed):
            loaded = account.load_session()
        self.assertIs(loaded, refreshed)
        refreshed.load_oauth_session.assert_called_once()
        refreshed.check_login.assert_called_once()
        self.assertEqual(refreshed.load_oauth_session.call_args.kwargs["access_token"], "DUMMY-ACCESS-ONLY")
        self.assertEqual(account._read_session()["access_token"], "DUMMY-NEW-ACCESS")
        self.assertEqual(account._read_session()["refresh_token"], "DUMMY-NEW-REFRESH")

    def test_invalid_login_is_sanitized_and_closed(self):
        account.save_session(fake_session())
        session = fake_session()
        session.load_oauth_session.side_effect = RuntimeError("DUMMY-ACCESS-ONLY raw API response")
        with patch.object(account, "create_session", return_value=session), \
                self.assertRaises(account.AccountError) as caught:
            account.load_session()
        self.assertNotIn("DUMMY", str(caught.exception))
        session.request_session.close.assert_called_once()

    def test_status_returns_only_boolean(self):
        session = fake_session()
        with patch.object(account, "load_session", return_value=session):
            self.assertIs(account.account_status(), True)
        session.request_session.close.assert_called_once()
        with patch.object(account, "load_session", side_effect=account.AccountError("Not connected")):
            self.assertIs(account.account_status(), False)


class DeviceLoginTests(unittest.TestCase):
    def prepared(self, future=None, url="link.tidal.com/DUMMY-CODE"):
        session = fake_session()
        if future is None:
            future = concurrent.futures.Future()
            future.set_result(True)
        link = SimpleNamespace(expires_in=300, verification_uri_complete=url)
        session.login_oauth = Mock(return_value=(link, future))
        return session, future

    def test_only_official_https_signin_hosts_are_allowed(self):
        self.assertEqual(account._verification_url("link.tidal.com/abcd"), "https://link.tidal.com/abcd")
        self.assertEqual(account._verification_url("https://login.tidal.com/device"), "https://login.tidal.com/device")
        for url in ("https://link.tidal.com.evil.test/a", "http://link.tidal.com/a",
                    "https://name@link.tidal.com/a", "https://link.tidal.com:999/a", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(account.AccountError):
                account._verification_url(url)

    def test_standard_device_flow_saves_only_after_confirmation(self):
        session, _ = self.prepared()
        logs = []
        with patch.object(account, "create_session", return_value=session), \
                patch.object(account, "save_session") as save, patch.object(account.webbrowser, "open") as browser:
            account.login(emit=logs.append)
        browser.assert_called_once_with("https://link.tidal.com/DUMMY-CODE", new=2)
        save.assert_called_once_with(session)
        session.check_login.assert_called_once()
        self.assertEqual(logs[-1], "TIDAL connected")
        self.assertNotIn("DUMMY-ACCESS-ONLY", "\n".join(logs))
        self.assertNotIn("DUMMY-REFRESH-ONLY", "\n".join(logs))
        self.assertTrue(session.request_session.authorization_cancelled.is_set())

    def test_timeout_is_bounded_and_no_session_saved(self):
        future = Mock()
        future.result.side_effect = concurrent.futures.TimeoutError()
        future.done.return_value = False
        session, _ = self.prepared(future)
        with patch.object(account, "create_session", return_value=session), \
                patch.object(account, "save_session") as save, self.assertRaisesRegex(account.AccountError, "expired"):
            account.login(open_browser=False, emit=lambda _: None)
        self.assertLessEqual(future.result.call_args.kwargs["timeout"], 300)
        self.assertGreater(future.result.call_args.kwargs["timeout"], 0)
        future.cancel.assert_called_once()
        save.assert_not_called()

    def test_no_save_when_subscription_check_fails(self):
        session, _ = self.prepared()
        session.check_login.return_value = False
        with patch.object(account, "create_session", return_value=session), \
                patch.object(account, "save_session") as save, self.assertRaisesRegex(account.AccountError, "active session"):
            account.login(open_browser=False, emit=lambda _: None)
        save.assert_not_called()

    def test_bad_provider_url_is_not_opened_or_printed(self):
        session, _ = self.prepared(url="https://evil.test/?DUMMY-PRIVATE")
        logs = []
        with patch.object(account, "create_session", return_value=session), \
                patch.object(account.webbrowser, "open") as browser, self.assertRaises(account.AccountError):
            account.login(emit=logs.append)
        browser.assert_not_called()
        self.assertNotIn("DUMMY-PRIVATE", "".join(logs))

    def test_device_client_rejection_is_sanitized(self):
        session = fake_session()
        session.login_oauth = Mock(side_effect=RuntimeError("DUMMY-SECRET raw server body"))
        with patch.object(account, "create_session", return_value=session), \
                patch.object(account, "save_session") as save, self.assertRaises(account.AccountError) as caught:
            account.login(open_browser=False)
        self.assertIn("device client may be unavailable or outdated", str(caught.exception))
        self.assertNotIn("DUMMY-SECRET", str(caught.exception))
        save.assert_not_called()

    def test_library_raw_errors_do_not_propagate(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            account._quiet_library_logs()
            logging.getLogger("tidalapi.session").error("DUMMY-PRIVATE raw response")
            self.assertEqual(output.getvalue(), "")
        finally:
            root.removeHandler(handler)

    def test_session_uses_standard_lossless_enum_and_finite_http_timeout(self):
        session = account.create_session()
        try:
            self.assertEqual(session.audio_quality, tidalapi.Quality.high_lossless)
            with patch("requests.Session.request", return_value=object()) as request:
                session.request_session.get("https://api.tidal.com/v1/sessions")
            self.assertEqual(request.call_args.kwargs["timeout"], 25.0)
        finally:
            session.request_session.close()


class AccountTransportTests(unittest.TestCase):
    def setUp(self):
        self.session = account._BoundedHttpSession()
        self.addCleanup(self.session.close)

    def response(self, status=200, retry_after=None):
        return SimpleNamespace(status_code=status,
                               headers={} if retry_after is None else {"Retry-After": retry_after},
                               close=Mock())

    def test_catalog_starts_are_spaced_and_oauth_uses_its_own_cadence(self):
        clock = [100.0]
        starts = []
        def wait(seconds):
            clock[0] += seconds
            return False
        def send(_method, url, **_kwargs):
            starts.append((url, clock[0]))
            return self.response()
        with patch.object(account.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.session.authorization_cancelled, "wait", side_effect=wait) as waiting, \
                patch("requests.Session.request", side_effect=send):
            self.session.get("https://api.tidal.com/v1/sessions")
            self.session.post("https://auth.tidal.com/v1/oauth2/token", data={"grant_type": "refresh_token"})
            self.session.post("https://auth.tidal.com/v1/oauth2/token", data={"grant_type": "device_code"})
            self.session.get("https://openapi.tidal.com/v2/tracks/1")
            self.session.get("https://api.tidal.com/v1/tracks/2")
        self.assertEqual([when for _, when in starts], [100, 100, 100, 101, 102])
        self.assertEqual([call.args[0] for call in waiting.call_args_list], [1, 1])

    def test_timeout_cannot_be_disabled_and_adapters_do_not_retry(self):
        with patch("requests.Session.request", return_value=self.response()) as request:
            for supplied in (None, float("inf"), (None, 1000), (3, 4)):
                self.session.post("https://auth.tidal.com/v1/oauth2/token", timeout=supplied)
        self.assertEqual([call.kwargs["timeout"] for call in request.call_args_list],
                         [25, 25, (25, 25), (3, 4)])
        self.assertEqual(self.session.get_adapter("https://api.tidal.com").max_retries.total, 0)
        self.assertEqual(self.session.get_adapter("http://api.tidal.com").max_retries.total, 0)

    def test_rate_limit_stops_all_following_requests_without_retry_or_secret(self):
        response = self.response(429, "120")
        response.text = "DUMMY-PRIVATE raw response"
        with patch("requests.Session.request", return_value=response) as request:
            for url in ("https://api.tidal.com/v1/sessions", "https://auth.tidal.com/v1/oauth2/token"):
                with self.assertRaises(account.AccountRateLimitError) as caught:
                    self.session.get(url)
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(caught.exception.retry_after, 120)
                self.assertNotIn("DUMMY", str(caught.exception))
                self.assertIn("120 seconds", str(caught.exception))
        request.assert_called_once()
        response.close.assert_called_once()

    def test_retry_after_dates_and_invalid_headers_are_sanitized(self):
        date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
        self.assertGreaterEqual(account._retry_after_seconds(date), 89)
        self.assertLessEqual(account._retry_after_seconds(date), 90)
        for header in (None, "DUMMY-PRIVATE", "-1", "NaN", "x" * 1000):
            self.assertIsNone(account._retry_after_seconds(header))
        self.assertEqual(account._retry_after_seconds("0"), 0)

    def test_cancel_during_pacing_does_not_start_another_request(self):
        self.session._last_api_start = 100
        def cancel(_seconds):
            self.session.authorization_cancelled.set()
            return True
        with patch.object(account.time, "monotonic", return_value=100), \
                patch.object(self.session.authorization_cancelled, "wait", side_effect=cancel), \
                patch("requests.Session.request") as request, \
                self.assertRaises(account.requests.Timeout):
            self.session.get("https://api.tidal.com/v1/sessions")
        request.assert_not_called()

    def test_load_and_status_preserve_rate_limit_instead_of_requesting_reconnect(self):
        session = fake_session()
        session.load_oauth_session.side_effect = account.AccountRateLimitError(60)
        with patch.object(account, "_read_session", return_value={}), \
                patch.object(account, "create_session", return_value=session), \
                self.assertRaises(account.AccountRateLimitError):
            account.load_session()
        session.request_session.close.assert_called_once()
        with patch.object(account, "load_session", side_effect=account.AccountRateLimitError(60)), \
                self.assertRaises(account.AccountRateLimitError):
            account.account_status()

    def test_cli_rate_limit_returns_queue_pause_status(self):
        output = io.StringIO()
        with patch.object(account.sys, "argv", ["tidal_account", "status"]), \
                patch.object(account.sys, "stderr", output), \
                patch.object(account, "account_status", side_effect=account.AccountRateLimitError(60)):
            self.assertEqual(account.main(), 75)
        self.assertIn("HTTP 429", output.getvalue())
        self.assertIn("60 seconds", output.getvalue())


class DpapiTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI roundtrip")
    def test_real_current_user_dpapi_roundtrip_with_dummy_data(self):
        dummy = b"DPAPI TEST ONLY - not an account credential"
        protected = account.protect(dummy)
        self.assertNotEqual(protected, dummy)
        self.assertNotIn(dummy, protected)
        self.assertEqual(account.unprotect(protected), dummy)

    def test_no_plaintext_fallback_outside_windows(self):
        with patch.object(account.os, "name", "posix"), self.assertRaisesRegex(account.AccountError, "no plaintext"):
            account.protect(b"dummy")


if __name__ == "__main__":
    unittest.main()
