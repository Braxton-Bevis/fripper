"""Persistent, sequential subprocess queue for the Lucida desktop interface.

Only the CLI's internal download workers run concurrently. A single CLI process
owns the client profile at a time, including setup and diagnostics.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Sequence
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parent
LOG_LIMIT = 400
INPUT_LIMIT = 1000
DEFAULT_SETTINGS = {
    "service": "amazon",
    "format": "original",
    "bitrate": "320k",
    "jobs": 3,
    "output": str(ROOT / "downloads"),
    "keep_original": False,
    "flat": False,
}
_ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_STATUSES = {"queued", "running", "completed", "failed", "cancelled", "interrupted"}
_RETRYABLE = {"failed", "cancelled", "interrupted"}
_UTILITIES = {"setup", "doctor", "tidal_login"}
_KINDS = {"tracks", "albums", "playlist"} | _UTILITIES
_HTTP_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class _WindowsJob:
    """Own the entire CLI process tree, including children outliving its parent."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._ctypes = ctypes
        self._lock = threading.Lock()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
        self._kernel = kernel
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, pid: int) -> None:
        """Attach before executing any client code (Popen used CREATE_SUSPENDED)."""
        ctypes, kernel = self._ctypes, self._kernel
        from ctypes import wintypes

        process_handle = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(process_handle)

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == pid:
                    thread_handle = kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread_handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread_handle) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        kernel.CloseHandle(thread_handle)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError("Could not locate the suspended client thread.")
        finally:
            kernel.CloseHandle(snapshot)

    def close(self) -> None:
        with self._lock:
            if self._handle:
                # Venv launchers create nested jobs. Explicit termination also
                # stops those jobs when they still hold references to this one.
                self._kernel.TerminateJobObject(self._handle, 1)
                self._kernel.CloseHandle(self._handle)
                self._handle = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_python() -> str:
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if candidate.is_file():
        return str(candidate)
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return str(executable)


def _direct_python(command: list[str], environment: dict) -> list[str]:
    """Avoid Windows Store activation redirectors escaping process containment.

    Run the actual interpreter with this project's installed dependencies on its
    process-local module path. Python descendants then use the base interpreter
    without reentering the virtual-environment redirector.
    """
    if os.name != "nt":
        return command
    launcher = Path(command[0])
    physical = Path(sys.base_prefix) / "python.exe"
    venv = ROOT / ".venv"
    if (launcher != venv / "Scripts" / "python.exe" or not physical.is_file()
            or "windowsapps" not in str(physical).lower()):
        return command
    configuration = venv / "pyvenv.cfg"
    if configuration.is_file():
        settings = dict(line.split(" = ", 1) for line in configuration.read_text(encoding="utf-8").splitlines() if " = " in line)
        configured_base = settings.get("executable")
        if configured_base and Path(configured_base) == Path(getattr(sys, "_base_executable", sys.executable)):
            environment["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(venv / "Lib" / "site-packages")))
            environment["PYTHONNOUSERSITE"] = "1"
            environment.pop("__PYVENV_LAUNCHER__", None)
            return [str(physical), *command[1:]]
    return command


def _options(values: dict, base: dict | None = None) -> dict:
    result = {**DEFAULT_SETTINGS, **(base or {})}
    if not isinstance(values, dict):
        raise ValueError("Options must be a dictionary.")
    for key in DEFAULT_SETTINGS:
        if key in values:
            result[key] = values[key]
    if result["service"] not in {"amazon", "qobuz", "grilledcheese", "tidal", "soundcloud"}:
        raise ValueError("Choose SoundCloud, TIDAL account, Amazon, Qobuz, or GrilledCheese as the service.")
    if result["format"] not in {"original", "mp3", "flac", "aac", "m4a", "opus", "ogg", "wav"}:
        raise ValueError("Choose a supported audio format.")
    if isinstance(result["jobs"], bool):
        raise ValueError("Parallel downloads must be a whole number from 1 to 8.")
    try:
        workers = int(result["jobs"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Parallel downloads must be a whole number from 1 to 8.") from exc
    if str(workers) != str(result["jobs"]).strip() or not 1 <= workers <= 8:
        raise ValueError("Parallel downloads must be a whole number from 1 to 8.")
    result["jobs"] = workers
    if not isinstance(result["bitrate"], str) or not re.fullmatch(r"[1-9]\d{1,3}k", result["bitrate"]):
        raise ValueError("Bitrate must use a value such as 192k or 320k.")
    for flag in ("keep_original", "flat"):
        if not isinstance(result[flag], bool):
            raise ValueError(f"{flag} must be true or false.")
    output = result["output"]
    if not isinstance(output, (str, os.PathLike)) or not str(output).strip():
        raise ValueError("Choose an output folder.")
    if any(ord(char) < 32 for char in str(output)):
        raise ValueError("Output folder contains an invalid control character.")
    output_path = Path(output).expanduser()
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    result["output"] = str(output_path.resolve())
    return result


def _validate_playlist(value: str) -> None:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid public playlist URL.") from exc
    hosts = {
        "open.spotify.com", "www.deezer.com", "deezer.com", "link.deezer.com",
        "tidal.com", "www.tidal.com", "listen.tidal.com", "browse.tidal.com",
        "play.qobuz.com", "open.qobuz.com", "qobuz.com", "www.qobuz.com",
        "music.apple.com", "music.youtube.com", "youtube.com", "www.youtube.com",
        "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com",
    }
    amazon = bool(re.fullmatch(r"music\.amazon\.(?:com|co\.uk|de|fr|it|es|co\.jp|ca|com\.au|com\.br|in|com\.mx)", host))
    if (parsed.scheme not in {"https", "http"} or (host not in hosts and not amazon)
            or parsed.username is not None or parsed.password is not None or port not in {None, 80, 443}):
        raise ValueError("Use a public playlist URL from a supported music service.")
    if not parsed.path.strip("/"):
        raise ValueError("Paste the full playlist URL, including its playlist ID.")


def _tidal_kind(value: str) -> str:
    message = "Use a full HTTPS TIDAL track, album, or playlist link (for example, https://tidal.com/browse/track/123)."
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError(message) from exc
    if (parsed.scheme != "https" or host not in {"tidal.com", "www.tidal.com", "listen.tidal.com", "embed.tidal.com"}
            or parsed.username is not None or parsed.password is not None or port not in {None, 443}):
        raise ValueError(message)
    match = re.fullmatch(r"/(?:browse/)?(tracks?|albums?|playlists?)/([^/]+)/?", parsed.path)
    if match is None:
        raise ValueError(message)
    kind, identity = match.groups()
    if kind in {"track", "tracks", "album", "albums"}:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", identity):
            raise ValueError("TIDAL track and album links must include a numeric ID.")
        return "tracks" if kind in {"track", "tracks"} else "albums"
    if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", identity):
        raise ValueError("TIDAL playlist links must include the full playlist ID.")
    return "playlist"


def _soundcloud_kind(value: str) -> str:
    # Keep this stdlib-only: the GUI must still start before yt-dlp is installed.
    try:
        parsed = urlsplit(value)
        host, port = (parsed.hostname or "").lower(), parsed.port
    except ValueError as exc:
        raise ValueError("Paste a valid HTTPS SoundCloud track, set, or share link.") from exc
    if (parsed.scheme != "https" or host not in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}
            or parsed.username is not None or parsed.password is not None or port not in {None, 443}):
        raise ValueError("Use an HTTPS soundcloud.com link or an on.soundcloud.com share link.")
    parts = parsed.path.strip("/").split("/")
    if host == "on.soundcloud.com":
        if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{3,100}", parts[0]):
            return "unknown"
    elif len(parts) in {3, 4} and parts[1] == "sets" and all(parts):
        return "playlist"
    elif (len(parts) in {2, 3} and all(parts) and parts[0] not in {"discover", "you", "search", "stream", "upload", "settings", "notifications", "messages"}
          and (len(parts) == 2 or parts[2].startswith("s-"))):
        return "tracks"
    raise ValueError("Paste a SoundCloud track, set, Discover mix, or short share link; profile pages are not supported.")


class QueueManager:
    """Thread-safe queue. Creating it never starts a download.

    command_builder is a test seam: it receives a copied job and must return an
    argument list. Commands are always executed without a shell.
    """

    def __init__(self, data_dir: str | Path | None = None,
                 python_executable: str | Path | None = None,
                 command_builder: Callable[[dict], Sequence[str]] | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else ROOT / ".local"
        self.data_dir = self.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.inputs_dir = self.data_dir / "inputs"
        self.logs_dir = self.data_dir / "logs"
        self.inputs_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.state_path = self.data_dir / "queue.json"
        self.python_executable = str(python_executable or _default_python())
        self._command_builder = command_builder
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._jobs: list[dict] = []
        self._settings = dict(DEFAULT_SETTINGS)
        self._paused = True
        self._closing = False
        self._closed = False
        self._active_id: str | None = None
        self._process: subprocess.Popen | None = None
        self._cancelled: set[str] = set()
        self._notice = ""
        self._load()
        self._thread = threading.Thread(target=self._worker, name="lucida-queue", daemon=True)
        self._thread.start()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            saved_text = self.state_path.read_text(encoding="utf-8")
            state = json.loads(saved_text)
            if not isinstance(state, dict):
                raise ValueError("Saved queue must contain an object.")
            settings = _options(state.get("settings", {}))
            loaded = state.get("jobs", [])
            if not isinstance(loaded, list):
                raise ValueError("Invalid saved jobs.")
            jobs = []
            seen = set()
            for item in loaded:
                if not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(item.get("id", ""))):
                    raise ValueError("Invalid saved job.")
                if item["id"] in seen or item.get("kind") not in _KINDS or item.get("status") not in _STATUSES:
                    raise ValueError("Invalid saved job metadata.")
                seen.add(item["id"])
                item["options"] = _options(item.get("options", {}))
                if not isinstance(item.get("inputs", []), list):
                    raise ValueError("Invalid saved inputs.")
                if not isinstance(item.get("logs", []), list):
                    raise ValueError("Invalid saved logs.")
                item["logs"] = [str(line)[:8192] for line in item.get("logs", [])][-LOG_LIMIT:]
                logfile = self.logs_dir / f"{item['id']}.log"
                if logfile.is_file():
                    with logfile.open(encoding="utf-8", errors="replace") as stream:
                        item["logs"] = [line.rstrip("\r\n")[:8192] for line in deque(stream, maxlen=LOG_LIMIT)]
                if item["status"] == "running":
                    item["status"] = "interrupted"
                    item["finished_at"] = _now()
                    item["logs"].append("Previous session ended during this job. Review its output before retrying.")
                    item["logs"] = item["logs"][-LOG_LIMIT:]
                jobs.append(item)
            self._settings, self._jobs = settings, jobs
        except (ValueError, TypeError, KeyError) as exc:
            # Preserve the original instead of silently erasing a damaged queue.
            backup = self.data_dir / f"queue.corrupt-{uuid.uuid4().hex[:8]}.json"
            self.state_path.replace(backup)
            self._notice = f"Saved queue could not be read ({exc}). Original saved as {backup.name}."
            return
        try:
            self._save_locked()
        except OSError as exc:
            self._notice = f"Loaded queue, but updated state could not be saved: {exc}"

    def _save_locked(self) -> None:
        state = {"version": 1, "settings": self._settings, "jobs": self._jobs}
        temporary = self.state_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("The queue is closed.")

    def _client_ready(self) -> bool:
        venv = ROOT / ".venv"
        if Path(self.python_executable).parent == venv / "Scripts":
            return (venv / "Scripts" / "lucidadl.exe").is_file() or (venv / "Lib" / "site-packages" / "lucidadl").is_dir()
        try:
            return importlib.util.find_spec("lucidadl") is not None
        except (ImportError, ValueError):
            return False

    def _tidal_ready(self) -> bool:
        venv = ROOT / ".venv"
        if Path(self.python_executable).parent == venv / "Scripts":
            return (venv / "Lib" / "site-packages" / "tidalapi").is_dir()
        try:
            return importlib.util.find_spec("tidalapi") is not None
        except (ImportError, ValueError):
            return False

    def _soundcloud_ready(self) -> bool:
        venv = ROOT / ".venv"
        if Path(self.python_executable).parent == venv / "Scripts":
            return (venv / "Lib" / "site-packages" / "yt_dlp").is_dir()
        try:
            return importlib.util.find_spec("yt_dlp") is not None
        except (ImportError, ValueError):
            return False

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "jobs": self._jobs, "settings": self._settings,
                "running": self._active_id is not None or not self._paused,
                "paused": self._paused, "active_id": self._active_id,
                "client_ready": self._client_ready(), "notice": self._notice,
                "tidal_ready": self._tidal_ready(),
                "tidal_connected": (self.data_dir / "tidal" / "session.bin").is_file(),
                "soundcloud_ready": self._soundcloud_ready(),
            })

    def update_settings(self, values: dict) -> None:
        with self._lock:
            self._ensure_open()
            previous = self._settings
            self._settings = _options(values, self._settings)
            try:
                self._save_locked()
            except OSError:
                self._settings = previous
                raise

    def add(self, kind: str, inputs: list[str] | None = None,
            options: dict | None = None, preview: bool = False) -> list[str]:
        if kind not in _KINDS | {"tidal", "soundcloud"}:
            raise ValueError("Unknown job type.")
        if preview and kind not in {"playlist", "tidal", "soundcloud"}:
            raise ValueError("Preview is available for playlists only.")
        if inputs is None:
            inputs = []
        if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
            raise ValueError("Provide inputs as a list of text lines.")
        if len(inputs) > INPUT_LIMIT:
            raise ValueError(f"Add at most {INPUT_LIMIT} inputs at a time.")
        if any(any(ord(char) < 32 or ord(char) == 127 for char in item) for item in inputs):
            raise ValueError("Each input must be one line without control characters.")
        clean = [item.strip() for item in inputs if item.strip() and not item.lstrip().startswith("#")]
        if any(len(item) > 4096 for item in clean):
            raise ValueError("An input is too long (maximum 4096 characters).")
        if kind in {"tracks", "albums", "playlist", "tidal", "soundcloud"} and not clean:
            raise ValueError("Enter at least one track, album, or playlist URL.")
        if kind == "playlist":
            for value in clean:
                _validate_playlist(value)
        if kind in _UTILITIES:
            clean = []
        if kind in {"tidal", "soundcloud"}:
            # Validate the complete paste before writing files or changing queue
            # state. Keep input order, batching only consecutive tracks/albums.
            routed = [((_tidal_kind(item) if kind == "tidal" else _soundcloud_kind(item)), item) for item in clean]
            if kind == "tidal" and preview and any(item_kind != "playlist" for item_kind, _ in routed):
                raise ValueError("Preview is available for playlists only. Remove TIDAL track and album links to preview.")
            groups: list[tuple[str, list[str]]] = []
            for item_kind, item in routed:
                if item_kind == "unknown":
                    item_kind = "tracks"  # Short links resolve at runtime, including shared sets.
                if item_kind != "playlist" and groups and groups[-1][0] == item_kind:
                    groups[-1][1].append(item)
                else:
                    groups.append((item_kind, [item]))
        elif kind == "playlist":
            groups = [(kind, [item]) for item in clean]
        else:
            groups = [(kind, clean)]
        with self._condition:
            self._ensure_open()
            normalized = _options(options or {}, self._settings)
            if normalized["service"] == "tidal":
                for job_kind, group in groups:
                    if job_kind in _UTILITIES:
                        continue
                    for value in group:
                        if _tidal_kind(value) != job_kind:
                            singular = {"tracks": "track", "albums": "album", "playlist": "playlist"}[job_kind]
                            raise ValueError(f"TIDAL account with {job_kind} mode requires TIDAL {singular} links. Choose TIDAL links mode for a mixed paste.")
            if normalized["service"] == "soundcloud":
                for job_kind, group in groups:
                    if job_kind in _UTILITIES:
                        continue
                    for value in group:
                        inferred = _soundcloud_kind(value)
                        if inferred != "unknown" and ((job_kind == "tracks" and inferred != "tracks") or (job_kind in {"albums", "playlist"} and inferred != "playlist")):
                            raise ValueError("Choose SoundCloud links mode for mixed track and playlist links.")
            if kind == "soundcloud" and normalized["service"] != "soundcloud":
                raise ValueError("Choose SoundCloud as the audio source for SoundCloud links mode.")
            ids = []
            previous = self._jobs[:]
            pending = []
            for job_kind, group in groups:
                job_id = uuid.uuid4().hex
                if job_kind in {"tracks", "albums"}:
                    label = f"{len(group)} {job_kind}" if len(group) != 1 else group[0]
                elif job_kind == "playlist":
                    label = ("Preview: " if preview else "") + group[0]
                else:
                    label = {"setup": "Browser setup", "doctor": "Diagnostics", "tidal_login": "Connect TIDAL account"}[job_kind]
                job = {"id": job_id, "kind": job_kind, "label": label,
                       "status": "queued", "inputs": group, "options": dict(normalized),
                       "preview": bool(preview), "logs": [], "created_at": _now()}
                if job_kind in {"tracks", "albums"}:
                    self._write_inputs(job)
                pending.append(job)
                ids.append(job_id)
            for job in pending:
                if job["kind"] in _UTILITIES:
                    first_queued = next((index for index, existing in enumerate(self._jobs)
                                         if existing["status"] == "queued"), len(self._jobs))
                    self._jobs.insert(first_queued, job)
                else:
                    self._jobs.append(job)
            try:
                self._save_locked()
            except OSError:
                self._jobs = previous
                raise
            self._condition.notify_all()
            return ids

    def _write_inputs(self, job: dict) -> Path:
        path = self.inputs_dir / f"{job['id']}.txt"
        path.write_text("\n".join(job["inputs"]) + "\n", encoding="utf-8")
        return path

    def build_command(self, job: dict) -> list[str]:
        if job["kind"] == "tidal_login":
            return [self.python_executable, "-u", "-m", "tidal_account", "login"]
        if job["kind"] == "doctor" and job["options"]["service"] == "tidal":
            return [self.python_executable, "-u", "-m", "tidal_account", "status"]
        if job["kind"] == "doctor" and job["options"]["service"] == "soundcloud":
            return [self.python_executable, "-u", "-m", "soundcloud_direct", "status"]
        direct = job["options"]["service"] in {"tidal", "soundcloud"} and job["kind"] not in _UTILITIES
        module = job["options"]["service"] + "_direct" if direct else "client_bridge"
        command = [self.python_executable, "-u", "-m", module, job["kind"]]
        if job["kind"] in {"setup", "doctor"}:
            return command
        options = job["options"]
        if job["kind"] in {"tracks", "albums"}:
            command += ["--file", str(self._write_inputs(job))]
        if not direct:
            command += ["--service", options["service"]]
        command += ["--jobs", str(options["jobs"]), "--out", options["output"]]
        if options["format"] != "original":
            command += ["--to", options["format"], "--bitrate", options["bitrate"]]
        if options["keep_original"]:
            command.append("--keep-original")
        if options["flat"]:
            command.append("--flat")
        if job.get("preview"):
            command.append("--dry-run")
        if job["kind"] == "playlist":
            command += ["--", job["inputs"][0]]
        return command

    def start(self) -> None:
        with self._condition:
            self._ensure_open()
            self._paused = False
            self._condition.notify_all()

    def pause(self) -> None:
        with self._condition:
            self._paused = True
            self._condition.notify_all()

    def cancel_active(self) -> None:
        with self._condition:
            self._paused = True
            active = self._active_id
            process = self._process
            if active is not None:
                self._cancelled.add(active)
            self._condition.notify_all()
        if process is not None:
            self._terminate_tree(process)

    def retry_failed(self) -> list[str]:
        with self._condition:
            self._ensure_open()
            ids = []
            previous = []
            for job in self._jobs:
                if job["status"] in _RETRYABLE:
                    previous.append((job, dict(job)))
                    job["status"] = "queued"
                    for field in ("started_at", "finished_at", "returncode"):
                        job.pop(field, None)
                    ids.append(job["id"])
            try:
                self._save_locked()
            except OSError:
                for job, original in previous:
                    job.clear()
                    job.update(original)
                raise
            self._condition.notify_all()
            return ids

    def remove(self, ids: Sequence[str]) -> None:
        targets = set(ids)
        with self._lock:
            self._ensure_open()
            previous = self._jobs
            self._jobs = [job for job in self._jobs if job["id"] not in targets or job["id"] == self._active_id]
            try:
                self._save_locked()
            except OSError:
                self._jobs = previous
                raise

    def clear_completed(self) -> None:
        with self._lock:
            self._ensure_open()
            previous = self._jobs
            self._jobs = [job for job in self._jobs if job["status"] != "completed"]
            try:
                self._save_locked()
            except OSError:
                self._jobs = previous
                raise

    def _log(self, job: dict, line: str) -> None:
        line = _ANSI.sub("", line).replace("\r", "").replace("\x00", "").rstrip("\n")
        if job.get("options", {}).get("service") in {"tidal", "soundcloud"} and job["kind"] not in _UTILITIES:
            # Signed media URLs can contain credentials. Keep the provider's
            # status text, but never persist download URLs in queue logs.
            line = _HTTP_URL.sub("[URL omitted]", line)
        line = line[:8192]
        if not line:
            return
        with self._lock:
            job["logs"].append(line)
            del job["logs"][:-LOG_LIMIT]
            with (self.logs_dir / f"{job['id']}.log").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")

    @staticmethod
    def _terminate_tree(process: subprocess.Popen) -> None:
        job = getattr(process, "_lucida_job", None)
        if job is not None:
            job.close()
        if os.name == "nt":
            if job is None and process.poll() is None:
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=8,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _worker(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._closing:
                        return
                    job = next((job for job in self._jobs if job["status"] == "queued"), None)
                    if not self._paused and job is not None:
                        break
                    if job is None:
                        self._paused = True
                    self._condition.wait()
                self._active_id = job["id"]
                job["status"] = "running"
                job["started_at"] = _now()
                try:
                    self._save_locked()
                except OSError as exc:
                    job["status"] = "queued"
                    job.pop("started_at", None)
                    self._active_id = None
                    self._paused = True
                    self._notice = f"Cannot start work because queue state could not be saved: {exc}"
                    continue
            self._run(job)

    def _run(self, job: dict) -> None:
        returncode = None
        process = None
        process_job = None
        error = None
        try:
            self._log(job, f"Started {job['kind']} at {job['started_at']}")
            if not self._command_builder:
                tidal_job = job["kind"] == "tidal_login" or (job["options"]["service"] == "tidal" and job["kind"] != "setup")
                soundcloud_job = job["options"]["service"] == "soundcloud" and job["kind"] not in {"setup", "tidal_login"}
                if soundcloud_job and not self._soundcloud_ready():
                    self._log(job, "SoundCloud support is not installed in this Python environment. Run Install client.cmd first.")
                elif tidal_job and not self._tidal_ready():
                    self._log(job, "TIDAL account support is not installed in this Python environment. Run Install client.cmd first.")
                elif not tidal_job and not soundcloud_job and not self._client_ready():
                    self._log(job, "Client is not installed in this Python environment. Run Install client.cmd first.")
            command = list(self._command_builder(copy.deepcopy(job)) if self._command_builder else self.build_command(job))
            if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
                raise ValueError("Command builder must return a nonempty list of arguments.")
            with self._lock:
                cancelled_before_launch = job["id"] in self._cancelled or self._closing
            if not cancelled_before_launch:
                environment = os.environ.copy()
                environment.update({"LUCIDADL_HOME": str(self.data_dir / "client"),
                                    "TIDAL_DESKTOP_HOME": str(self.data_dir / "tidal"),
                                    "LUCIDADL_MUSIC": job["options"]["output"],
                                    "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / ".local" / "browsers"),
                                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
                command = _direct_python(command, environment)
                if os.name == "nt":
                    process_job = _WindowsJob()
                    popen_options = {"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000004}  # CREATE_SUSPENDED
                else:
                    popen_options = {"start_new_session": True}
                process = subprocess.Popen(command, cwd=str(ROOT), env=environment,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace", bufsize=1, shell=False, **popen_options)
                if process_job is not None:
                    process._lucida_job = process_job
                    process_job.attach_and_resume(process.pid)
                with self._lock:
                    self._process = process
                    should_cancel = job["id"] in self._cancelled or self._closing
                if should_cancel:
                    self._terminate_tree(process)
                assert process.stdout is not None
                for line in iter(lambda: process.stdout.readline(65536), ""):
                    self._log(job, line)
                returncode = process.wait()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                self._log(job, error)
            except OSError:
                pass
            if process is not None:
                self._terminate_tree(process)
                try:
                    returncode = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process_job is not None:
                process_job.close()
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self._condition:
                cancelled = job["id"] in self._cancelled or self._closing
                self._cancelled.discard(job["id"])
                job["status"] = "cancelled" if cancelled else ("completed" if returncode == 0 and error is None else "failed")
                job["returncode"] = returncode
                job["finished_at"] = _now()
                self._process = None
                self._active_id = None
                if job["kind"] in _UTILITIES:
                    self._paused = True
                if returncode == 75:
                    self._paused = True
                    self._notice = "The service rate-limited this job. The queue is paused; wait before retrying."
                try:
                    self._log(job, f"Job {job['status']}." + (f" Exit code: {returncode}." if returncode is not None else ""))
                    self._save_locked()
                except OSError as exc:
                    self._paused = True
                    self._notice = f"Could not save queue state: {exc}"
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closing = True
            self._paused = True
            if self._active_id:
                self._cancelled.add(self._active_id)
            process = self._process
            self._condition.notify_all()
        if process is not None:
            self._terminate_tree(process)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise RuntimeError("The background client has not stopped yet. Keep the application open and try closing again.")
        with self._lock:
            self._save_locked()
            self._closed = True

