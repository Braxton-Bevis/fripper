"""TIDAL's interactive OAuth device login with current-user DPAPI storage.

Passwords stay in TIDAL's own browser page. This module never writes a plaintext
token file and never emits provider response bodies or token-bearing errors.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import webbrowser

import requests
import tidalapi


ROOT = Path(__file__).resolve().parent
_MAGIC = b"TIDAL-DPAPI\x01"
_ENTROPY = b"Lucida Desktop TIDAL session v1"
_SIGN_IN = "Use Connect TIDAL to sign in again."
_API_HOSTS = {"api.tidal.com", "openapi.tidal.com"}
_API_REQUEST_INTERVAL = 1.0


class AccountError(RuntimeError):
    """An account error whose message is safe to show in the GUI."""


class AccountRateLimitError(AccountError):
    """Stop this account session on HTTP 429 without retaining its response."""

    status_code = 429

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after
        wait = (f" Wait at least {retry_after} seconds before manually retrying."
                if retry_after is not None else " Wait before manually retrying.")
        super().__init__("TIDAL rate-limited this request (HTTP 429). Account requests have stopped." + wait)


def _retry_after_seconds(value) -> int | None:
    """Interpret Retry-After without echoing arbitrary response text."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal() and len(value) <= 10:
        return int(value)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


def session_path() -> Path:
    folder = os.environ.get("TIDAL_DESKTOP_HOME")
    return (Path(folder).expanduser() if folder else ROOT / ".local" / "tidal") / "session.bin"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise AccountError("Secure TIDAL session storage requires Windows DPAPI; no plaintext session was saved.")
    if not isinstance(data, bytes) or not data:
        raise AccountError("The protected TIDAL session is empty or invalid. " + _SIGN_IN)
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        source, source_buffer = _blob(data)
        entropy, entropy_buffer = _blob(_ENTROPY)
        result = _DataBlob()
        if decrypt:
            call = crypt32.CryptUnprotectData
            call.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
            description = None
        else:
            call = crypt32.CryptProtectData
            call.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
            description = "TIDAL desktop session"
        call.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately omit LOCAL_MACHINE scope.
        ok = call(ctypes.byref(source), description, ctypes.byref(entropy), None, None,
                  0x1, ctypes.byref(result))
        try:
            if not ok:
                raise AccountError("Windows could not unlock this TIDAL session for the current user. " + _SIGN_IN
                                   if decrypt else "Windows could not protect the TIDAL session; nothing was saved.")
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            if result.pbData:
                kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
    except AccountError:
        raise
    except Exception:
        raise AccountError("Windows secure session storage failed. " + _SIGN_IN) from None


def protect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=False)


def unprotect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=True)


def _quiet_library_logs():
    # tidalapi logs raw OAuth response bodies on failure. Its child loggers all
    # propagate here; suppress these records before starting background polling.
    parent = logging.getLogger("tidalapi")
    parent.handlers = [logging.NullHandler()]
    parent.propagate = False
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("tidalapi.") and isinstance(logger, logging.Logger):
            logger.handlers = []
            logger.propagate = True


class _BoundedHttpSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.authorization_deadline = None
        self.authorization_cancelled = threading.Event()
        self._request_lock = threading.Lock()
        self._last_api_start = None
        self._rate_limit_error = None
        # Do not retry connection failures or provider responses automatically.
        self.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))

    def _request_timeout(self):
        if self.authorization_cancelled.is_set():
            raise requests.Timeout("TIDAL authorization ended")
        timeout = 25.0
        if self.authorization_deadline is not None:
            remaining = self.authorization_deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("TIDAL authorization expired")
            timeout = min(timeout, remaining)
        return timeout

    def request(self, method, url, **kwargs):
        # Only the authenticated catalog endpoints are paced here. OAuth polling
        # keeps tidalapi's server-supplied interval, and refreshes remain usable.
        is_api = urlsplit(url).hostname in _API_HOSTS
        with self._request_lock:
            if self._rate_limit_error is not None:
                raise AccountRateLimitError(self._rate_limit_error.retry_after) from None
            timeout = self._request_timeout()
            if is_api and self._last_api_start is not None:
                delay = max(0.0, self._last_api_start + _API_REQUEST_INTERVAL - time.monotonic())
                if delay:
                    self.authorization_cancelled.wait(min(delay, timeout))
                    timeout = self._request_timeout()
            # A caller cannot accidentally disable the finite network timeout.
            def bounded(value):
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    return min(float(value), timeout)
                return timeout
            requested = kwargs.get("timeout")
            kwargs["timeout"] = (tuple(bounded(value) for value in requested)
                                 if isinstance(requested, tuple) and len(requested) == 2
                                 else bounded(requested))
            if is_api:
                self._last_api_start = time.monotonic()
            response = super().request(method, url, **kwargs)
            if getattr(response, "status_code", None) == 429:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                response.close()
                self._rate_limit_error = AccountRateLimitError(retry_after)
                raise self._rate_limit_error from None
            return response


def create_session() -> tidalapi.Session:
    _quiet_library_logs()
    session = tidalapi.Session(tidalapi.Config(quality=tidalapi.Quality.high_lossless))
    session.request_session.close()
    session.request_session = _BoundedHttpSession()
    return session


def _expiry_text(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise AccountError("TIDAL returned invalid session expiry information; nothing was saved.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _session_data(session) -> dict:
    token_type = getattr(session, "token_type", None)
    access_token = getattr(session, "access_token", None)
    refresh_token = getattr(session, "refresh_token", None)
    if (not isinstance(token_type, str) or not token_type.strip() or
            not isinstance(access_token, str) or not access_token.strip() or
            not isinstance(refresh_token, str) or not refresh_token.strip()):
        raise AccountError("TIDAL did not provide a reusable session. " + _SIGN_IN)
    return {"version": 1, "token_type": token_type, "access_token": access_token,
            "refresh_token": refresh_token, "expiry_time": _expiry_text(session.expiry_time),
            "is_pkce": bool(getattr(session, "is_pkce", False))}


def save_session(session) -> None:
    """Atomically save current tokens, encrypted for this Windows user."""
    temporary = None
    try:
        data = _session_data(session)
        encrypted = protect(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        destination = session_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".session-", suffix=".tmp",
                                         dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(_MAGIC + encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    except AccountError:
        raise
    except Exception:
        raise AccountError("Could not save the encrypted TIDAL session. Check folder permissions, then connect again.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_session() -> dict:
    path = session_path()
    try:
        if not path.is_file():
            raise AccountError("TIDAL is not connected. Use Connect TIDAL and sign in in your browser.")
        if path.stat().st_size > 1_000_000:
            raise ValueError("invalid size")
        encrypted = path.read_bytes()
        if not encrypted.startswith(_MAGIC):
            raise ValueError("invalid format")
        data = json.loads(unprotect(encrypted[len(_MAGIC):]).decode("utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("invalid version")
        for key in ("token_type", "access_token", "refresh_token"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError("missing session field")
        expiry = data.get("expiry_time")
        if expiry is not None:
            expiry = datetime.fromisoformat(expiry)
            if expiry.tzinfo is not None:
                expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        if not isinstance(data.get("is_pkce", False), bool):
            raise ValueError("invalid session type")
        return {"token_type": data["token_type"], "access_token": data["access_token"],
                "refresh_token": data["refresh_token"], "expiry_time": expiry,
                "is_pkce": data.get("is_pkce", False)}
    except AccountError:
        raise
    except Exception:
        raise AccountError("The saved TIDAL connection could not be read securely. " + _SIGN_IN) from None


def load_session() -> tidalapi.Session:
    """Return a verified session and persist any automatically refreshed tokens."""
    data = _read_session()
    session = None
    try:
        session = create_session()
        if not session.load_oauth_session(**data) or not session.check_login():
            raise AccountError("The saved TIDAL connection is no longer valid. " + _SIGN_IN)
        save_session(session)
        return session
    except AccountError:
        if session is not None:
            session.request_session.close()
        raise
    except Exception:
        if session is not None:
            session.request_session.close()
        raise AccountError("Could not verify the TIDAL connection. Check your connection or use Connect TIDAL again.") from None


def account_status() -> bool:
    """Check saved access without returning a name, account ID, or credential."""
    try:
        session = load_session()
    except AccountRateLimitError:
        raise
    except AccountError:
        return False
    session.request_session.close()
    return True


def _verification_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise AccountError("TIDAL did not return a valid sign-in link. Please try Connect TIDAL again.")
    url = value if "://" in value else "https://" + value
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in {"link.tidal.com", "login.tidal.com"}
                 and not parsed.username and not parsed.password and parsed.port in {None, 443})
    except ValueError:
        valid = False
    if not valid:
        raise AccountError("TIDAL returned an unexpected sign-in host. No browser was opened; please retry later.")
    return url


def login(*, open_browser: bool = True, emit=print) -> None:
    """Start standard device OAuth and let the user complete TIDAL's sign-in."""
    session = None
    future = None
    try:
        if os.name != "nt":
            raise AccountError("TIDAL account connections require Windows DPAPI on this desktop.")
        session = create_session()
        try:
            link, future = session.login_oauth()
        except AccountRateLimitError:
            raise
        except Exception:
            raise AccountError("TIDAL could not start device sign-in. Its device client may be unavailable or outdated; "
                               "check your connection and retry. No account credentials were saved.") from None
        seconds = float(link.expires_in)
        if not math.isfinite(seconds) or seconds <= 0 or seconds > 86_400:
            raise AccountError("TIDAL returned an invalid sign-in expiry. Please retry Connect TIDAL.")
        deadline = time.monotonic() + seconds
        session.request_session.authorization_deadline = deadline
        url = _verification_url(link.verification_uri_complete)
        emit("Open this official TIDAL page and finish signing in yourself:")
        emit(url)
        emit(f"Waiting for TIDAL approval (up to {math.ceil(seconds)} seconds).")
        if open_browser:
            try:
                webbrowser.open(url, new=2)
            except Exception:
                emit("Open the link above in your browser to continue.")
        try:
            future.result(timeout=max(0, deadline - time.monotonic()))
        except AccountRateLimitError:
            raise
        except concurrent.futures.TimeoutError:
            raise AccountError("TIDAL sign-in expired. Use Connect TIDAL to generate a new sign-in link.") from None
        except Exception:
            raise AccountError("TIDAL did not complete sign-in. Finish approval in your browser, or use Connect TIDAL again.") from None
        session.request_session.authorization_deadline = None
        if not session.check_login():
            raise AccountError("TIDAL did not confirm an active session. Use Connect TIDAL again.")
        save_session(session)
        emit("TIDAL connected")
    except AccountError:
        raise
    except KeyboardInterrupt:
        raise AccountError("TIDAL sign-in cancelled. No new connection was saved.") from None
    except Exception:
        raise AccountError("TIDAL sign-in could not be completed. Please try Connect TIDAL again.") from None
    finally:
        if future is not None and not future.done():
            future.cancel()
        if session is not None:
            session.request_session.authorization_cancelled.set()
            session.request_session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect your TIDAL subscription securely on this Windows desktop.")
    parser.add_argument("command", choices=("login", "status"))
    parser.add_argument("--no-browser", action="store_true", help="Print the official sign-in link without opening it.")
    args = parser.parse_args()
    try:
        if args.command == "login":
            login(open_browser=not args.no_browser)
            return 0
        connected = account_status()
        print("TIDAL connected" if connected else "TIDAL is not connected")
        return 0 if connected else 1
    except AccountRateLimitError as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
