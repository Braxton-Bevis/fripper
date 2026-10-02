"""SoundCloud originals and full streams through the official yt-dlp extractor."""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import math
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit
import uuid

import imageio_ffmpeg
import mutagen
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as YTDLP_VERSION


class SoundCloudError(RuntimeError):
    """User-facing error containing no authenticated or signed URLs."""


class RateLimitedError(SoundCloudError):
    """Stop all subsequent work until the user decides to retry later."""


@dataclass
class Collection:
    title: str
    entries: list[dict]
    identifier: str = "tracks"
    playlist: bool = False


_LOCK = threading.Lock()
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_FORMATS = {"original", "mp3", "aac", "m4a", "flac", "opus", "ogg", "wav"}
_CODECS = {"mp3": "libmp3lame", "aac": "aac", "m4a": "aac", "flac": "flac",
           "opus": "libopus", "ogg": "libvorbis", "wav": "pcm_s24le"}
RATE_LIMIT_BYTES = 256 * 1024
REQUEST_DELAY_SECONDS = 1
DOWNLOAD_DELAY_SECONDS = 5


def is_rate_limited(exc):
    pending, seen = [exc], set()
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, RateLimitedError):
            return True
        if any(getattr(item, field, None) == 429 for field in ("status", "status_code", "code")):
            return True
        if re.search(r"\b429\b|too many requests|rate.limit", str(item), re.I):
            return True
        pending.extend((getattr(item, "cause", None), getattr(item, "__cause__", None), getattr(item, "response", None)))
    return False


def log(message):
    with _LOCK:
        print(_URL.sub("[URL omitted]", str(message)).replace("\r", " "), flush=True)


def safe_error(exc):
    text = _URL.sub("[URL omitted]", str(exc))
    text = re.sub(r"(?i)(oauth[_ -]?token|access[_ -]?token|authorization|secret_token)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    lower = text.lower()
    if "drm" in lower or "encrypted media" in lower:
        return "SoundCloud exposes protected audio for this track; this app cannot download protected streams."
    if any(word in lower for word in ("login required", "sign in", "403", "401", "private track", "private playlist")):
        return "SoundCloud requires account access for this item, or denies guest playback. Browser cookies were not read. " + text[:350]
    if is_rate_limited(exc):
        return "SoundCloud rate-limited this request. Downloads have stopped; wait before retrying."
    if isinstance(exc, SoundCloudError):
        return text
    if isinstance(exc, OSError):
        return "A local file operation failed; check the output folder and free disk space."
    return text[:500] or type(exc).__name__


class SafeLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        lower = str(message).lower()
        if "original download" in lower and any(word in lower for word in ("registered", "login", "sign in")):
            log("Uploader original requires SoundCloud sign-in; using the best available public stream.")
            return
        log("SoundCloud notice: " + safe_error(SoundCloudError(message)))

    def error(self, message):
        # The caller reports a single sanitized error with the track position.
        pass


def soundcloud_kind(value):
    """Validate user inputs; short links are resolved by SoundCloud's extractor."""
    try:
        parsed = urlsplit(value)
        host, port = (parsed.hostname or "").lower(), parsed.port
    except (ValueError, TypeError) as exc:
        raise SoundCloudError("Paste a valid HTTPS SoundCloud track or playlist link.") from exc
    if (not isinstance(value, str) or any(ord(char) < 32 for char in value)
            or parsed.scheme != "https" or host not in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}
            or parsed.username is not None or parsed.password is not None or port not in {None, 443}):
        raise SoundCloudError("Use an HTTPS soundcloud.com track/set link or an on.soundcloud.com share link.")
    parts = parsed.path.strip("/").split("/")
    if host == "on.soundcloud.com":
        if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", parts[0]):
            raise SoundCloudError("Paste the complete SoundCloud share link.")
        return "unknown"
    if len(parts) in {3, 4} and parts[1] == "sets" and all(parts):
        return "playlist"
    if (len(parts) in {2, 3} and all(parts) and parts[0] not in {"discover", "you", "search", "stream", "upload", "settings", "notifications", "messages"}
            and (len(parts) == 2 or parts[2].startswith("s-"))):
        return "tracks"
    raise SoundCloudError("Choose an individual SoundCloud track, set, or Discover mix; profile and search pages are not supported.")


def safe_name(value, limit=80):
    text = unicodedata.normalize("NFC", str(value))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", text)
    text = " ".join(text.split()).strip(" .")[:limit].rstrip(" .") or "Untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", text, re.I):
        text = "_" + text
    return text


def ydl_options(**overrides):
    options = {"quiet": True, "noprogress": True, "logger": SafeLogger(),
               "ignoreerrors": False, "extract_flat": "in_playlist", "cachedir": False,
               "cookiefile": None, "cookiesfrombrowser": None, "usenetrc": False,
               "socket_timeout": 30, "retries": 0, "fragment_retries": 0, "extractor_retries": 0,
               "ratelimit": RATE_LIMIT_BYTES, "concurrent_fragment_downloads": 1,
               "sleep_interval_requests": REQUEST_DELAY_SECONDS,
               "sleep_interval": DOWNLOAD_DELAY_SECONDS, "max_sleep_interval": DOWNLOAD_DELAY_SECONDS,
               "skip_unavailable_fragments": False, "overwrites": True,
               # Our stream-copy normalization and full-decode validation below
               # replace auto fixups, whose HLS detector otherwise needs ffprobe.
               "fixup": "never",
               "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
               "format": "bestaudio/best", "noplaylist": False, "writethumbnail": False,
               "writeinfojson": False, "getcomments": False}
    options.update(overrides)
    return options


def resolve_collections(kind, inputs, ydl_factory=YoutubeDL):
    references = [(value, soundcloud_kind(value)) for value in inputs]
    if not references:
        raise SoundCloudError("Add at least one SoundCloud link.")
    for _, inferred in references:
        if inferred != "unknown" and ((kind == "tracks" and inferred != "tracks") or (kind in {"albums", "playlist"} and inferred != "playlist")):
            raise SoundCloudError("This SoundCloud link does not match the selected track or playlist mode.")
    collections, pending = [], []
    with ydl_factory(ydl_options()) as ydl:
        for value, inferred in references:
            info = ydl.extract_info(value, download=False)
            if not isinstance(info, dict):
                raise SoundCloudError("SoundCloud returned no metadata for this item.")
            if "entries" in info:
                if kind == "tracks" and inferred != "unknown":
                    raise SoundCloudError("A playlist was returned for a track link; choose playlist mode.")
                if pending:
                    collections.append(Collection("SoundCloud tracks", pending))
                    pending = []
                entries = list(info["entries"])
                if not entries or len(entries) > 20_000 or any(not isinstance(entry, dict) for entry in entries):
                    raise SoundCloudError("SoundCloud returned an empty, incomplete, or excessively large playlist.")
                expected = info.get("playlist_count")
                if isinstance(expected, int) and expected != len(entries):
                    raise SoundCloudError(f"SoundCloud returned {len(entries)} of {expected} playlist entries; no partial collection was accepted.")
                collections.append(Collection(str(info.get("title") or "SoundCloud playlist"), entries,
                                              str(info.get("id") or "playlist"), True))
            else:
                if kind in {"albums", "playlist"}:
                    raise SoundCloudError("This SoundCloud share link resolves to a track. Choose Tracks or SoundCloud links mode.")
                pending.append(info)
    if pending:
        collections.append(Collection("SoundCloud tracks", pending))
    return collections


def select_formats(info):
    formats = [item for item in info.get("formats", []) if isinstance(item, dict) and item.get("url")
               and item.get("vcodec", "none") == "none" and not item.get("has_drm")
               and "preview" not in str(item.get("format_id", "")).lower()
               and (item.get("preference") is None or item.get("preference", 0) > -10)]
    originals = [item for item in formats if item.get("format_id") == "download"]
    streams = [item for item in formats if item.get("format_id") != "download"]
    # yt-dlp sorts formats worst-to-best using its codec/bitrate quality rules.
    result = originals[-1:] + streams[-1:]
    if not result:
        raise SoundCloudError("No full, unprotected SoundCloud audio is available. Preview-only audio was refused.")
    return result


def _ffmpeg(arguments, timeout=900, capture=False):
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, arguments)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise SoundCloudError("FFmpeg could not decode or convert the audio; the file was not marked complete.")
    return result.stdout if capture else None


def validate_audio(path, expected_duration=0):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 32:
        raise SoundCloudError("The downloaded audio is empty or incomplete.")
    try:
        audio = mutagen.File(path)
        duration = float(audio.info.length) if audio is not None and getattr(audio, "info", None) else 0
    except Exception as exc:
        raise SoundCloudError("The response could not be identified as an audio file.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise SoundCloudError("The response does not contain playable audio.")
    if expected_duration > 0 and abs(duration - expected_duration) > max(5, expected_duration * .02):
        raise SoundCloudError("The audio duration does not match the full SoundCloud track; partial audio was refused.")
    progress = _ffmpeg(["-xerror", "-i", path, "-map", "0:a:0", "-progress", "pipe:1", "-nostats",
                       "-f", "null", "NUL" if os.name == "nt" else "/dev/null"], capture=True)
    timestamps = re.findall(rb"(?m)^out_time_us=(\d+)\s*$", progress or b"")
    decoded = max((int(value) / 1_000_000 for value in timestamps), default=0)
    if decoded <= 0 or abs(decoded - duration) > .5:
        raise SoundCloudError("The decoder did not reach the file's declared duration; incomplete audio was refused.")
    return duration


def _entry_info(entry, ydl):
    if entry.get("formats"):
        return copy.deepcopy(entry)
    reference = entry.get("webpage_url") or entry.get("url")
    if not isinstance(reference, str):
        raise SoundCloudError("SoundCloud returned an entry without a usable track link.")
    parsed = urlsplit(reference)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"soundcloud.com", "www.soundcloud.com", "api.soundcloud.com", "api-v2.soundcloud.com"}:
        raise SoundCloudError("SoundCloud returned an unsupported track reference.")
    info = ydl.extract_info(reference, download=False)
    if not isinstance(info, dict) or "entries" in info or str(info.get("id")) != str(entry.get("id")):
        raise SoundCloudError("SoundCloud returned a different or missing playlist track; it was not downloaded.")
    return info


def _metadata(info, position):
    identifier = str(info.get("id", ""))
    title = str(info.get("title") or info.get("track") or "")
    artist = ", ".join(info.get("artists") or []) or str(info.get("uploader") or "")
    duration = float(info.get("duration") or 0)
    if not re.fullmatch(r"\d{1,20}", identifier) or not title or not artist or not math.isfinite(duration) or duration <= 0:
        raise SoundCloudError("SoundCloud returned incomplete track identity or duration metadata.")
    return {"id": identifier, "title": title, "artist": artist, "duration": duration,
            "album": str(info.get("album") or ""), "position": position}


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(metadata, selected, options):
    return {"version": 1, "soundcloud_id": metadata["id"],
            "source_kind": "uploader_original" if selected.get("format_id") == "download" else "stream",
            "source_format": str(selected.get("format_id") or ""), "source_extension": str(selected.get("ext") or ""),
            "source_codec": selected.get("acodec"), "source_bitrate_kbps": selected.get("abr"),
            "output_format": options.to,
            "keep_original": bool(getattr(options, "keep_original", False)) if options.to != "original" else False,
            "output_bitrate": options.bitrate if options.to in {"mp3", "aac", "m4a", "opus", "ogg"} else None}


def record_provenance(path, metadata, selected, options, original_path=None):
    """Record only public source/encoding fields; never save signed media URLs."""
    path = Path(path)
    record = {**_provenance(metadata, selected, options), "sha256": _hash_file(path), "bytes": path.stat().st_size}
    if original_path is not None:
        original_path = Path(original_path)
        if original_path.parent != path.parent:
            raise SoundCloudError("The retained original must be in the same output folder.")
        record["retained_original"] = original_path.name
    sidecar = path.with_suffix(path.suffix + ".source.json")
    temporary = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, sidecar)
    finally:
        temporary.unlink(missing_ok=True)
    return sidecar


def _matches_provenance(path, expected):
    path = Path(path)
    sidecar = path.with_suffix(path.suffix + ".source.json")
    try:
        if not path.is_file() or not sidecar.is_file() or sidecar.stat().st_size > 65536:
            return False
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        matches = (isinstance(saved, dict) and all(saved.get(key) == value for key, value in expected.items())
                   and saved.get("bytes") == path.stat().st_size and saved.get("sha256") == _hash_file(path))
        if matches and expected.get("keep_original"):
            retained = saved.get("retained_original")
            if not isinstance(retained, str) or Path(retained).name != retained:
                return False
            original_expected = {**expected, "output_format": "original", "output_bitrate": None, "keep_original": False}
            return _matches_provenance(path.parent / retained, original_expected)
        return matches
    except (OSError, ValueError, TypeError):
        return False


def _target_path(base, expected, duration):
    """Preserve earlier versions and unrecorded files instead of reclassifying them."""
    base = Path(base)
    fingerprint = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()[:8]
    variant = base.with_name(f"{base.stem}.{fingerprint}{base.suffix}")
    for candidate in (base, variant):
        if not candidate.exists():
            return candidate, False
        if _matches_provenance(candidate, expected):
            try:
                validate_audio(candidate, duration)
                return candidate, True
            except SoundCloudError:
                pass
    return base.with_name(f"{base.stem}.{uuid.uuid4().hex[:8]}{base.suffix}"), False


def _write_tags(path, metadata):
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise SoundCloudError("The audio container could not be opened for metadata tagging.")
    if audio.tags is None:
        audio.add_tags()
    if path.suffix.lower() in {".wav", ".aiff", ".aif"}:
        from mutagen.id3 import TIT2, TPE1, TALB, TRCK
        for frame in (TIT2(encoding=3, text=metadata["title"]), TPE1(encoding=3, text=metadata["artist"]),
                      TALB(encoding=3, text=metadata.get("album", "")), TRCK(encoding=3, text=str(metadata.get("position", 1)))):
            audio.tags.add(frame)
    else:
        audio["title"] = [metadata["title"]]
        audio["artist"] = [metadata["artist"]]
        audio["album"] = [metadata.get("album", "")]
        audio["tracknumber"] = [str(metadata.get("position", 1))]
    audio.save()


def tag_audio(path, metadata):
    """Atomically add tags to an existing file without changing its audio codec."""
    path = Path(path)
    validate_audio(path, metadata.get("duration", 0))
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex[:8]}{path.suffix}")
    try:
        shutil.copy2(path, temporary)
        _write_tags(temporary, metadata)
        validate_audio(temporary, metadata.get("duration", 0))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_stream(raw, stage, extension, metadata):
    normalized = stage / f"normalized.{extension}"
    arguments = ["-i", raw, "-map", "0:a:0", "-vn", "-c:a", "copy"]
    if extension == "m4a":
        arguments += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
    _ffmpeg([*arguments, normalized])
    _write_tags(normalized, metadata)
    return normalized


def download_track(entry, directory, position, total, options, ydl_factory=YoutubeDL):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".soundcloud-", dir=directory) as temporary:
        stage = Path(temporary)
        with ydl_factory(ydl_options(outtmpl=str(stage / "source.%(ext)s"))) as ydl:
            info = _entry_info(entry, ydl)
            metadata = _metadata(info, position)
            label = f"{metadata['artist']} - {metadata['title']}"
            prefix = f"[{position:03d}/{total}] {label}"
            stem = f"{position:03d} {safe_name(label, 65)} [SC {metadata['id']}]"
            formats = select_formats(info)
            last_error = None
            for attempt, selected in enumerate(formats):
                extension = str(selected.get("ext") or "")
                if not re.fullmatch(r"[a-zA-Z0-9]{1,8}", extension):
                    raise SoundCloudError("SoundCloud returned an unsupported audio extension.")
                requested_ext = "m4a" if options.to in {"aac", "m4a"} else options.to
                target_ext = extension if options.to == "original" else requested_ext
                provenance = _provenance(metadata, selected, options)
                target, complete = _target_path(directory / f"{stem}.{target_ext}", provenance, metadata["duration"])
                if complete:
                    log(f"{prefix} — already complete ({target_ext.upper()}, matching source and encoding settings)")
                    return target, metadata, True
                original = selected.get("format_id") == "download"
                quality = "uploader original" if original else "best available SoundCloud stream"
                if selected.get("abr"):
                    quality += f", {selected['abr']:g} kb/s"
                log(f"{prefix} — {quality} ({extension.upper()})")
                validated_source = False
                retained_path = None
                try:
                    chosen = copy.deepcopy(info)
                    chosen["formats"] = [copy.deepcopy(selected)]
                    for key in ("requested_downloads", "requested_formats", "url", "format_id", "format"):
                        chosen.pop(key, None)
                    ydl.process_ie_result(chosen, download=True)
                    candidates = [path for path in stage.glob("source.*") if path.is_file() and path.suffix not in {".part", ".ytdl", ".temp"}]
                    if len(candidates) != 1:
                        raise SoundCloudError("The audio transfer did not produce one complete file.")
                    raw = candidates[0]
                    if not original:
                        raw = _normalize_stream(raw, stage, extension, metadata)
                    validate_audio(raw, metadata["duration"])
                    validated_source = True
                    if options.to == "original":
                        os.replace(raw, target)
                    else:
                        original_options = argparse.Namespace(to="original", bitrate=options.bitrate)
                        original_provenance = _provenance(metadata, selected, original_options)
                        original_path, original_existed = _target_path(directory / f"{stem}.original.{extension}", original_provenance, metadata["duration"])
                        if not original_existed:
                            os.replace(raw, original_path)
                            record_provenance(original_path, metadata, selected, original_options)
                        converted = stage / f"converted.{target_ext}"
                        args = ["-i", original_path, "-map", "0:a:0", "-vn", "-c:a", _CODECS[options.to]]
                        if options.to in {"mp3", "aac", "m4a", "opus", "ogg"}:
                            args += ["-b:a", options.bitrate]
                        args += ["-metadata", f"title={metadata['title']}", "-metadata", f"artist={metadata['artist']}",
                                 "-metadata", f"album={metadata['album']}", "-metadata", f"track={position}", converted]
                        if not original and options.to in {"flac", "wav"}:
                            log(f"{prefix} — converting the stream container/codec does not increase source quality")
                        _ffmpeg(args)
                        validate_audio(converted, metadata["duration"])
                        os.replace(converted, target)
                        if not options.keep_original and not original_existed:
                            original_path.unlink()
                            original_path.with_suffix(original_path.suffix + ".source.json").unlink(missing_ok=True)
                        if options.keep_original:
                            retained_path = original_path
                    record_provenance(target, metadata, selected, options, retained_path)
                    log(f"{prefix} — saved {target_ext.upper()} from {quality}")
                    return target, metadata, False
                except Exception as exc:
                    last_error = exc
                    if is_rate_limited(exc):
                        raise RateLimitedError("SoundCloud rate-limited this request. No fallback or further track was requested.") from exc
                    if validated_source or not original or attempt == len(formats) - 1:
                        raise
                    log(f"{prefix} — original download unavailable; trying the full stream ({safe_error(exc)})")
                    for path in stage.iterdir():
                        if path.is_file():
                            path.unlink()
            raise SoundCloudError(safe_error(last_error or SoundCloudError("No audio could be downloaded.")))


def write_playlist(path, results):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.part")
    lines = ["#EXTM3U"]
    for audio, metadata, _skipped in results:
        title = f"{metadata['artist']} - {metadata['title']}".replace("\r", " ").replace("\n", " ")
        lines += [f"#EXTINF:{round(metadata['duration'])},{title}", Path(os.path.relpath(audio, path.parent)).as_posix()]
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_collection(collection, options):
    directory = Path(options.out)
    if not options.flat:
        directory /= safe_name(collection.title, 48) + f" [SC {safe_name(collection.identifier, 16)}]"
    total = len(collection.entries)
    log(f"SoundCloud: {collection.title} — {total} entries, original order")
    if options.dry_run:
        for index, entry in enumerate(collection.entries, 1):
            log(f"{index:03d}. {entry.get('title') or 'SoundCloud track'} [SC {entry.get('id', 'unknown')}]")
        log("Preview complete; no audio downloaded. Stream availability is checked when downloading.")
        return 0, 0, 0
    results, failed, skipped, rate_limited = {}, 0, 0, False
    for index, entry in enumerate(collection.entries, 1):
        try:
            results[index] = download_track(entry, directory, index, total, options)
            skipped += bool(results[index][2])
        except Exception as exc:
            failed += 1
            log(f"[{index:03d}/{total}] FAILED: {safe_error(exc)}")
            if is_rate_limited(exc):
                rate_limited = True
                log(f"SOUNDCLOUD RATE LIMIT: stopped. {total - index} remaining entries were not requested.")
                break
    if collection.playlist and results:
        directory.mkdir(parents=True, exist_ok=True)
        name = safe_name(collection.title, 64) + (".partial.m3u8" if failed else ".m3u8")
        write_playlist(directory / name, [results[index] for index in sorted(results)])
        log(f"Saved {'partial ' if failed else ''}playlist: {len(results)}/{total} entries in source order")
    if rate_limited:
        raise RateLimitedError("SoundCloud rate limit reached; remaining tracks and collections were left unrequested.")
    return len(results) - skipped, skipped, failed


def run_downloads(kind, inputs, options):
    collections = resolve_collections(kind, inputs)
    totals = [0, 0, 0]
    log("SoundCloud originals are preferred when offered; otherwise the best available full stream is used. No browser cookies are read.")
    log("Conservative limits: one transfer, 256 KiB/s maximum, 1 second between metadata requests, 5 seconds before each download. These limits do not guarantee account safety.")
    for collection in collections:
        totals = [old + new for old, new in zip(totals, run_collection(collection, options))]
    log(f"Summary: {totals[0]} downloaded, {totals[1]} already complete, {totals[2]} failed.")
    return 1 if totals[2] else 0


def parser():
    result = argparse.ArgumentParser(description="Download SoundCloud originals or available full audio streams.")
    commands = result.add_subparsers(dest="kind", required=True)
    commands.add_parser("status")
    for kind in ("tracks", "albums", "playlist"):
        command = commands.add_parser(kind)
        if kind == "playlist":
            command.add_argument("url")
        else:
            command.add_argument("--file", required=True)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--out", default=str(Path.cwd() / "downloads"))
        command.add_argument("--jobs", type=int, choices=range(1, 9), default=3)
        command.add_argument("--to", choices=sorted(_FORMATS), default="original")
        command.add_argument("--bitrate", default="320k")
        command.add_argument("--keep-original", action="store_true")
        command.add_argument("--flat", action="store_true")
    return result


def main(argv=None):
    options = parser().parse_args(argv)
    try:
        if options.kind == "status":
            _ffmpeg(["-version"])
            log(f"SoundCloud ready: yt-dlp {YTDLP_VERSION}; FFmpeg available; guest access, no browser cookies.")
            return 0
        if not re.fullmatch(r"[1-9][0-9]{1,3}k", options.bitrate):
            raise SoundCloudError("Choose a bitrate such as 192k or 320k.")
        inputs = [options.url] if options.kind == "playlist" else [line.strip() for line in Path(options.file).read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if len(inputs) > 1000:
            raise SoundCloudError("Add at most 1000 SoundCloud links at a time.")
        return run_downloads(options.kind, inputs, options)
    except KeyboardInterrupt:
        log("Download interrupted. Completed files remain available.")
        return 130
    except Exception as exc:
        log("SoundCloud: " + safe_error(exc))
        return 75 if is_rate_limited(exc) else 1


if __name__ == "__main__":
    raise SystemExit(main())
