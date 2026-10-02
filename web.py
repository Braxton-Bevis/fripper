"""FRipper Web: run the download queue from a phone or iPad browser.

Serves a touch-friendly page plus a small JSON API over the same QueueManager
the desktop app uses, and lets finished music be downloaded to the browsing
device (single files, or whole folders as a zip).

    python web.py                 # listen on 0.0.0.0:8787
    python web.py --set-pin 4821  # choose the access PIN
    python web.py --port 9000

Access needs the PIN (shown on first run and kept in .local/web-pin.txt).
Restrict network exposure with the firewall (see web-setup.ps1); never expose
this server to the public internet.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit
import zipfile

from manager import QueueManager, ROOT

APP_NAME = "FRipper"
WEB_DIR = ROOT / "web"
ASSETS = ROOT / "assets"
DATA = ROOT / ".local"
COOKIE = "fripper_session"
SESSION_DAYS = 30
MODES = {"tidal", "soundcloud", "tracks", "albums", "playlist"}
SERVICES = {"tidal", "soundcloud", "amazon", "qobuz", "grilledcheese"}
FORMATS = {"original", "mp3", "flac", "aac", "opus", "ogg", "wav"}
BITRATES = {"320k", "256k", "192k", "128k"}
UTILITIES = {"tidal_login": "tidal", "setup": "amazon", "doctor": None}
AUDIO = {".flac", ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav", ".alac", ".mp4", ".webm"}
_PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")
_SUMMARY = re.compile(r"Summary: (\d+) downloaded, (\d+) already complete, (\d+) failed")
log = logging.getLogger("fripper.web")


# ----- queue helpers (mirror the desktop app's behaviour) -------------------

def classify_links(lines):
    """Return "tidal" / "soundcloud" when every line is a link to that service."""
    lines = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return None
    hosts = []
    for line in lines:
        match = re.match(r"https?://([^/\s]+)", line, re.IGNORECASE)
        hosts.append(match.group(1).lower() if match else "")
    if all(host.endswith("tidal.com") for host in hosts):
        return "tidal"
    if all(host.endswith("soundcloud.com") for host in hosts):
        return "soundcloud"
    return None


def job_progress(job):
    for line in reversed(job.get("logs") or []):
        match = _PROGRESS.search(str(line))
        if match:
            index, total = int(match.group(1)), int(match.group(2))
            done = index - 1 if "requesting" in str(line) else index
            current = re.sub(r"\s+—\s+(requesting account audio|saved .*|already complete.*)$", "",
                             str(line)[match.end():].strip())
            return {"done": max(0, min(done, total)), "total": total, "current": current}
    return None


def status_label(job):
    status = str(job.get("status", "queued"))
    if status == "failed":
        for line in reversed(job.get("logs") or []):
            summary = _SUMMARY.search(str(line))
            if summary:
                downloaded, existing, failures = map(int, summary.groups())
                if downloaded + existing and failures:
                    return f"Partial {downloaded + existing}/{downloaded + existing + failures}"
                break
    return status.capitalize()


def ready_key(service):
    return "tidal_ready" if service == "tidal" else "soundcloud_ready" if service == "soundcloud" else "client_ready"


class FRipperWeb:
    """Queue operations and library access behind the HTTP handler."""

    def __init__(self, manager: QueueManager, data_dir: Path = DATA):
        self.manager = manager
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "web.json"
        self.config = self._load_config()
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        settings = manager.snapshot().get("settings", {})
        default = (ROOT / "downloads").resolve()
        music = Path.home() / "Music"
        if (not manager.snapshot().get("jobs") and not default.exists() and music.is_dir()
                and Path(settings.get("output") or default).resolve() == default):
            manager.update_settings({"output": str(music / APP_NAME)})

    # --- access control -------------------------------------------------------

    def _load_config(self):
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            if {"salt", "pin_hash", "secret"} <= config.keys():
                return config
        except (OSError, ValueError):
            pass
        return self.set_pin(f"{secrets.randbelow(1_000_000):06d}", announce=True)

    def set_pin(self, pin: str, announce=False):
        if not re.fullmatch(r"\d{4,12}", pin):
            raise ValueError("The PIN must be 4 to 12 digits.")
        salt = secrets.token_hex(16)
        config = {"salt": salt, "pin_hash": self._hash(pin, salt),
                  "secret": secrets.token_hex(32)}  # New secret signs everyone out.
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.data_dir / "web-pin.txt").write_text(
            f"FRipper web PIN: {pin}\nChange it with: python web.py --set-pin NEWPIN\n", encoding="utf-8")
        if announce:
            print(f"FRipper web PIN: {pin}  (also saved in {self.data_dir / 'web-pin.txt'})", flush=True)
        self.config = config
        return config

    @staticmethod
    def _hash(pin, salt):
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()

    def login(self, pin: str, client: str):
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < 300]
            self._failures[client] = recent
            if len(recent) >= 5:
                raise PermissionError("Too many attempts. Wait five minutes, then try again.")
        if not hmac.compare_digest(self._hash(str(pin), self.config["salt"]), self.config["pin_hash"]):
            with self._lock:
                self._failures[client].append(now)
            raise PermissionError("That PIN is not right.")
        expires = int(time.time()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(str(expires))}"

    def _sign(self, value):
        return hmac.new(bytes.fromhex(self.config["secret"]), value.encode(), hashlib.sha256).hexdigest()

    def valid_session(self, token: str | None):
        if not token or "." not in token:
            return False
        expires, signature = token.split(".", 1)
        return (expires.isdigit() and int(expires) > time.time()
                and hmac.compare_digest(signature, self._sign(expires)))

    # --- queue -----------------------------------------------------------------

    def state(self, job_id=None):
        snapshot = self.manager.snapshot()
        jobs = []
        for job in snapshot.get("jobs", []):
            utility = job.get("kind") in UTILITIES
            item = {"id": job["id"], "label": job.get("label", "Untitled"), "kind": job.get("kind"),
                    "status": job.get("status"), "status_label": status_label(job),
                    "service": None if utility else job.get("options", {}).get("service"),
                    "preview": bool(job.get("preview")), "progress": job_progress(job)}
            if job["id"] == job_id:
                item["logs"] = [str(line) for line in (job.get("logs") or [])[-400:]]
            jobs.append(item)
        settings = snapshot.get("settings", {})
        output = Path(settings.get("output") or ROOT / "downloads")
        try:
            usage = shutil.disk_usage(output if output.exists() else output.anchor)
            disk = {"free": usage.free, "total": usage.total}
        except OSError:
            disk = None
        return {"jobs": jobs, "active_id": snapshot.get("active_id"), "paused": snapshot.get("paused"),
                "running": snapshot.get("running"), "notice": snapshot.get("notice", ""),
                "ready": {key: bool(snapshot.get(key, True)) for key in ("tidal_ready", "soundcloud_ready", "client_ready")},
                "tidal_connected": bool(snapshot.get("tidal_connected")),
                "settings": {key: settings.get(key) for key in ("service", "format", "bitrate", "keep_original", "flat", "jobs")},
                "output": str(output), "disk": disk, "windows": os.name == "nt"}

    def _options(self, body):
        settings = self.manager.snapshot().get("settings", {})
        service = body.get("service", settings.get("service", "tidal"))
        fmt = body.get("format", settings.get("format", "original"))
        bitrate = body.get("bitrate", settings.get("bitrate", "320k"))
        if service not in SERVICES or fmt not in FORMATS or bitrate not in BITRATES:
            raise ValueError("Choose a valid source, format and bitrate.")
        jobs = 1 if service in ("tidal", "soundcloud") else int(settings.get("jobs") or 3)
        return {"service": service, "format": fmt, "bitrate": bitrate, "jobs": jobs,
                "output": settings.get("output") or str(ROOT / "downloads"),
                "keep_original": bool(body.get("keep_original", settings.get("keep_original", False))),
                "flat": bool(body.get("flat", settings.get("flat", False)))}

    def require_ready(self, service):
        if not self.manager.snapshot().get(ready_key(service), True):
            raise ValueError("Download support for this source is not installed on the server. Run the FRipper installer there.")

    def add(self, body):
        inputs = [line.strip() for line in str(body.get("text", "")).splitlines() if line.strip()]
        if not inputs:
            raise ValueError("Paste at least one link or search first.")
        mode = body.get("mode", "tidal")
        if mode not in MODES:
            raise ValueError("Choose a valid link type.")
        detected = classify_links(inputs)
        if detected == "soundcloud" or mode == "soundcloud":
            mode, body = "soundcloud", {**body, "service": "soundcloud"}
        elif detected == "tidal":
            mode = "tidal"
        if mode == "tidal" and body.get("service") == "soundcloud":
            body = {**body, "service": "tidal"}
        options = self._options(body)
        self.manager.update_settings(options)
        preview = bool(body.get("preview"))
        ids = self.manager.add(mode, inputs, options, preview=preview)
        return {"ids": ids, "mode": mode, "service": options["service"]}

    def utility(self, kind):
        if kind not in UTILITIES:
            raise ValueError("Unknown task.")
        service = UTILITIES[kind] or self.manager.snapshot().get("settings", {}).get("service", "tidal")
        self.require_ready(service)
        options = self._options({"service": service})
        ids = self.manager.add(kind, [], options)
        self.manager.start()
        return {"ids": ids}

    def control(self, action, body):
        manager = self.manager
        if action == "start":
            queued = next((job for job in manager.snapshot().get("jobs", []) if job.get("status") == "queued"), None)
            if queued:
                kind = queued.get("kind")
                self.require_ready(UTILITIES.get(kind) or queued.get("options", {}).get("service"))
            manager.start()
        elif action == "pause":
            manager.start() if manager.snapshot().get("paused") else manager.pause()
        elif action == "stop":
            manager.cancel_active()
        elif action == "retry":
            manager.retry_failed()
        elif action == "clear":
            manager.clear_completed()
        elif action == "remove":
            active = manager.snapshot().get("active_id")
            ids = [str(job_id) for job_id in body.get("ids", []) if str(job_id) != active]
            if ids:
                manager.remove(ids)
        elif action == "settings":
            options = self._options(body)
            manager.update_settings(options)
        else:
            raise ValueError("Unknown action.")
        return {"ok": True}

    # --- library ---------------------------------------------------------------

    def library_root(self):
        settings = self.manager.snapshot().get("settings", {})
        return Path(settings.get("output") or ROOT / "downloads").resolve()

    def resolve(self, relative):
        root = self.library_root()
        relative = str(relative or "").replace("\\", "/").strip("/")
        if any(part in ("..",) for part in relative.split("/")) or ":" in relative:
            raise PermissionError("That path is outside the music folder.")
        target = (root / relative).resolve() if relative else root
        if target != root and root not in target.parents:
            raise PermissionError("That path is outside the music folder.")
        return root, target

    def library(self, relative):
        root, folder = self.resolve(relative)
        if not folder.exists():
            return {"path": "", "entries": [], "missing": True}
        if not folder.is_dir():
            raise ValueError("Not a folder.")
        entries = []
        for entry in folder.iterdir():
            if entry.name.startswith(".") or entry.name.endswith((".part", ".tmp")):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            item = {"name": entry.name, "path": entry.relative_to(root).as_posix(), "modified": stat.st_mtime}
            if entry.is_dir():
                files = [f for f in entry.rglob("*") if f.is_file()]
                item.update(type="folder", tracks=sum(f.suffix.lower() in AUDIO for f in files),
                            size=sum(f.stat().st_size for f in files))
            else:
                item.update(type="audio" if entry.suffix.lower() in AUDIO else "file", size=stat.st_size)
            entries.append(item)
        entries.sort(key=lambda e: (e["type"] != "folder", -e["modified"]))
        return {"path": "" if folder == root else folder.relative_to(root).as_posix(), "entries": entries}

    def delete(self, relative):
        root, target = self.resolve(relative)
        if target == root:
            raise PermissionError("The music folder itself cannot be deleted.")
        if not target.exists():
            return {"ok": True}
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        return {"ok": True}


# ----- HTTP layer ------------------------------------------------------------

class _Sink:
    """Write-only stream for zipfile that forwards to the socket."""

    def __init__(self, stream):
        self.stream, self.position = stream, 0

    def write(self, data):
        self.stream.write(data)
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        self.stream.flush()


def make_handler(app: FRipperWeb):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FRipperWeb/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        # --- responses
        def _headers(self, status, content_type, length=None, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if length is not None:
                self.send_header("Content-Length", str(length))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _json(self, payload, status=HTTPStatus.OK, extra=None):
            body = json.dumps(payload).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(body), extra)
            self.wfile.write(body)

        def _error(self, status, message):
            self._json({"error": message}, status)

        def _session(self):
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return None

        def _authorized(self):
            if app.valid_session(self._session()):
                return True
            self._error(HTTPStatus.UNAUTHORIZED, "Sign in with your PIN.")
            return False

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("Request too large.")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Invalid request.")
            return body

        def _static(self, path: Path, content_type):
            data = path.read_bytes()
            cache = {"Cache-Control": "public, max-age=3600"} if path.suffix == ".png" else None
            self._headers(HTTPStatus.OK, content_type, len(data), cache)
            self.wfile.write(data)

        # --- routes
        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    return self._static(WEB_DIR / "index.html", "text/html; charset=utf-8")
                if url.path == "/manifest.webmanifest":
                    return self._static(WEB_DIR / "manifest.webmanifest", "application/manifest+json")
                if url.path.startswith("/assets/"):
                    name = url.path.rsplit("/", 1)[-1]
                    if re.fullmatch(r"fripper[\w-]*\.png", name) and (ASSETS / name).is_file():
                        return self._static(ASSETS / name, "image/png")
                    return self._error(HTTPStatus.NOT_FOUND, "Not found.")
                if url.path == "/api/session":
                    return self._json({"signed_in": app.valid_session(self._session())})
                if not self._authorized():
                    return
                if url.path == "/api/state":
                    return self._json(app.state((query.get("job") or [None])[0]))
                if url.path == "/api/library":
                    return self._json(app.library((query.get("path") or [""])[0]))
                if url.path == "/download":
                    return self._download((query.get("path") or [""])[0])
                return self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except PermissionError as exc:
                self._error(HTTPStatus.FORBIDDEN, str(exc))
            except (ValueError, OSError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self):
            url = urlsplit(self.path)
            # A custom header cannot be sent cross-site without CORS approval: CSRF guard.
            if self.headers.get("X-FRipper") != "1":
                return self._error(HTTPStatus.FORBIDDEN, "Missing request header.")
            try:
                body = self._body()
                if url.path == "/api/login":
                    token = app.login(str(body.get("pin", "")), self.client_address[0])
                    cookie = f"{COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"
                    return self._json({"ok": True}, extra={"Set-Cookie": cookie})
                if url.path == "/api/logout":
                    return self._json({"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
                if not self._authorized():
                    return
                if url.path == "/api/add":
                    return self._json(app.add(body))
                if url.path == "/api/utility":
                    return self._json(app.utility(body.get("kind")))
                if url.path == "/api/library/delete":
                    return self._json(app.delete(body.get("path")))
                match = re.fullmatch(r"/api/(start|pause|stop|retry|clear|remove|settings)", url.path)
                if match:
                    return self._json(app.control(match.group(1), body))
                return self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except PermissionError as exc:
                self._error(HTTPStatus.FORBIDDEN, str(exc))
            except (ValueError, OSError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def _download(self, relative):
            root, target = app.resolve(relative)
            if not target.exists():
                return self._error(HTTPStatus.NOT_FOUND, "That file is no longer on the server.")
            if target.is_file():
                name = target.name
                disposition = f"attachment; filename*=UTF-8''{quote(name)}"
                with target.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    self._headers(HTTPStatus.OK, "application/octet-stream", size, {"Content-Disposition": disposition})
                    shutil.copyfileobj(stream, self.wfile, 1024 * 256)
                return
            name = (target.name if target != root else APP_NAME) + ".zip"
            files = sorted(f for f in target.rglob("*") if f.is_file() and not f.name.endswith((".part", ".tmp")))
            # Audio is already compressed: store, and stream without knowing the length.
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            base = target.parent
            with zipfile.ZipFile(_Sink(self.wfile), "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                for file in files:
                    archive.write(file, file.relative_to(base).as_posix())

    return Handler


def _single_instance():
    """Share the desktop app's lock so only one process drives this queue."""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    identity = hashlib.sha256(str(ROOT.resolve()).lower().encode("utf-8")).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, f"Local\\LucidaDesktop_{identity}")
    if not handle or ctypes.get_last_error() == 183:
        return False
    _single_instance.handle = handle  # Keep it open for the life of the process.
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="FRipper web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--set-pin", metavar="PIN", help="Set a new 4-12 digit PIN, then exit.")
    args = parser.parse_args(argv)
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA / "web.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.set_pin:
        config_path = DATA / "web.json"
        config = {}
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        holder = FRipperWeb.__new__(FRipperWeb)
        holder.data_dir, holder.config_path, holder.config = DATA, config_path, config
        holder.set_pin(args.set_pin)
        print("PIN updated. Existing browser sessions are signed out.")
        return 0
    if not _single_instance():
        print("FRipper is already running for this folder (desktop app or web server). Close it first.", file=sys.stderr)
        log.error("Another FRipper instance owns this queue; exiting.")
        return 1
    manager = QueueManager()
    app = FRipperWeb(manager)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    server.daemon_threads = True
    log.info("Listening on %s:%s", args.host, args.port)
    print(f"FRipper web is running on http://{args.host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
