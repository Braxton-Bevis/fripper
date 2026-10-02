"""Download exact TIDAL catalog items exposed to the signed-in account.

Only unencrypted, full-length audio manifests are supported. Account credentials
stay in tidal_account; CDN requests and FFmpeg never receive account tokens.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urljoin, urlsplit
import uuid
import xml.etree.ElementTree as ET

import imageio_ffmpeg
import mutagen
import requests
from tidalapi import Quality


class DirectError(RuntimeError):
    """An actionable error safe to print without exposing service credentials."""


class RateLimitError(DirectError):
    """Stop all queued TIDAL requests after a server throttle response."""
    counts = (0, 0, 0)


class BandwidthLimiter:
    def __init__(self, rate=256 * 1024, clock=None, sleep=None):
        self.rate = rate
        self.clock = clock or time.monotonic
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))
        self.deadline = 0.0

    def consume(self, count):
        now = self.clock()
        self.deadline = max(now, self.deadline) + count / self.rate
        delay = self.deadline - now
        if delay > 0:
            self.sleep(delay)


class TrackPacer:
    def __init__(self, interval=5.0, clock=None, sleep=None):
        self.interval = interval
        self.clock = clock or time.monotonic
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))
        self.last_start = None

    def wait(self):
        now = self.clock()
        if self.last_start is not None:
            delay = self.interval - (now - self.last_start)
            if delay > 0:
                self.sleep(delay)
        self.last_start = self.clock()


@dataclass
class Collection:
    name: str
    tracks: list
    identifier: str = ""
    playlist: bool = False


@dataclass
class AudioSource:
    urls: list[str]
    extension: str
    segmented: bool
    quality: str


_HOSTS = {"tidal.com", "www.tidal.com", "listen.tidal.com", "browse.tidal.com", "embed.tidal.com"}
_FORMATS = {"original", "flac", "mp3", "aac", "m4a", "opus", "ogg", "wav"}
_OUTPUTS = {"flac": ("flac", "flac"), "mp3": ("libmp3lame", "mp3"),
            "aac": ("aac", "ipod"), "m4a": ("aac", "ipod"),
            "opus": ("libopus", "opus"), "ogg": ("libvorbis", "ogg"), "wav": ("pcm_s24le", "wav")}
_NATIVE_MUX = {".flac": "flac", ".m4a": "ipod", ".mp3": "mp3"}
_LOG_LOCK = threading.Lock()
_BANDWIDTH_LIMITER = BandwidthLimiter()
_TRACK_PACER = TrackPacer()


def _is_rate_limit(exc):
    return (getattr(exc, "status_code", None) == 429
            or getattr(getattr(exc, "response", None), "status_code", None) == 429
            or type(exc).__name__ in {"TooManyRequests", "AccountRateLimitError"})


def _rate_limit_error(retry_after=None):
    seconds = None
    try:
        if str(retry_after).strip().isdigit():
            seconds = int(str(retry_after).strip())
        elif retry_after:
            date = parsedate_to_datetime(str(retry_after))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        pass
    wait = f" Wait at least {seconds} seconds before manually retrying." if seconds is not None and seconds >= 0 else " Wait before manually retrying."
    return RateLimitError("TIDAL RATE LIMIT: stopped. No more tracks will be requested." + wait)


def _throttle_from_exception(exc):
    retry_after = getattr(exc, "retry_after", None)
    response = getattr(exc, "response", None)
    if response is not None:
        retry_after = response.headers.get("Retry-After", retry_after)
    return _rate_limit_error(retry_after)


def log(message):
    with _LOG_LOCK:
        print(message, flush=True)


def safe_error(exc):
    if isinstance(exc, DirectError):
        return str(exc)
    name = type(exc).__name__
    if _is_rate_limit(exc):
        return str(_throttle_from_exception(exc))
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if name == "AccountError":
        return str(exc)  # tidal_account guarantees sanitized user-facing errors.
    if status in (401, 403) or name == "AuthenticationError":
        return "TIDAL did not authorize this request. Reconnect your account and check its subscription."
    if status == 404 or name in {"ObjectNotFound", "StreamNotAvailable", "AssetNotAvailable"}:
        return "This TIDAL item or full audio stream is unavailable for the signed-in account or region."
    if status == 429 or name == "TooManyRequests":
        return "TIDAL rate-limited this request. Wait a little, then retry with fewer parallel downloads."
    if isinstance(exc, requests.RequestException):
        return "The TIDAL connection failed or the audio link expired. Check the connection and retry."
    if isinstance(exc, OSError):
        return "A local file could not be read or written. Check the output folder, permissions, and free space."
    return f"The TIDAL operation could not complete ({name}). No unverified file was marked complete."


def tidal_reference(value, expected=None):
    try:
        parsed = urlsplit(value.strip())
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0] == "browse":
            parts = parts[1:]
        # TIDAL also shares track links nested beneath their album.
        if len(parts) == 4 and parts[0] == "album" and parts[2] == "track":
            parts = parts[2:]
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname in _HOSTS
                 and parsed.username is None and parsed.password is None
                 and parsed.port in {None, 80, 443} and len(parts) == 2)
    except ValueError:
        valid = False
    if not valid:
        raise DirectError("Direct TIDAL mode accepts exact TIDAL track, album, or playlist links; text searches are not supported.")
    kind = {"tracks": "track", "albums": "album", "playlists": "playlist"}.get(parts[0], parts[0])
    item_id = parts[1]
    pattern = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}" if kind == "playlist" else r"[1-9][0-9]{0,19}"
    if kind not in {"track", "album", "playlist"} or not re.fullmatch(pattern, item_id):
        raise DirectError("This TIDAL link does not contain a valid catalog ID.")
    if expected and kind != expected:
        raise DirectError(f"This is a TIDAL {kind} link. Choose the matching source mode.")
    return kind, item_id


def _all_tracks(collection, playlist=False):
    total = getattr(collection, "num_tracks", None)
    videos = getattr(collection, "num_videos", 0) or 0
    if not isinstance(total, int) or isinstance(total, bool) or total < 0 or total > 20_000:
        raise DirectError("TIDAL did not supply a usable collection track count.")
    if videos:
        raise DirectError("This collection contains videos. Direct audio import requires a music-only collection.")
    fetch = collection.items if playlist else collection.tracks
    tracks = []
    revision = None
    while len(tracks) < total:
        page = list(fetch(limit=min(100, total - len(tracks)), offset=len(tracks)))
        if playlist:
            current_revision = getattr(collection, "_etag", None)
            if revision is not None and current_revision != revision:
                raise DirectError("The TIDAL playlist changed while its pages were being read. Retry to import one complete revision.")
            if current_revision:
                revision = current_revision
        if not page or len(tracks) + len(page) > total:
            raise DirectError(f"TIDAL returned only {len(tracks)} of {total} expected tracks. Retry; no partial collection was imported.")
        if any(not callable(getattr(track, "get_stream", None)) for track in page):
            raise DirectError("TIDAL returned a non-music item; no partial collection was imported.")
        tracks.extend(page)  # Do not deduplicate: each playlist position is meaningful.
    return tracks


def resolve_collections(session, kind, inputs):
    references = [tidal_reference(value, {"tracks": "track", "albums": "album", "playlist": "playlist"}[kind]) for value in inputs]
    if kind == "tracks":
        return [Collection("Tracks", [session.track(item_id) for _, item_id in references])]
    result = []
    for _, item_id in references:
        if kind == "albums":
            album = session.album(item_id)
            result.append(Collection(str(album.name or "Album"), _all_tracks(album), item_id))
        else:
            playlist = session.playlist(item_id)
            result.append(Collection(str(playlist.name or "Playlist"), _all_tracks(playlist, True), item_id, True))
    return result


def _https_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DirectError("TIDAL supplied an unsupported audio URL; no audio was downloaded.")
    return url


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _children(element, name):
    return [child for child in element if _tag(child) == name]


def _child(element, name):
    return next(iter(_children(element, name)), None)


def _substitute(template, representation, number=0, timestamp=0):
    fields = {"RepresentationID": representation.get("id", ""),
              "Bandwidth": representation.get("bandwidth", ""), "Number": number, "Time": timestamp}
    escaped = template.replace("$$", "\0")
    def replace(match):
        value = fields[match.group(1)]
        width = match.group(2)
        return f"{int(value):0{int(width)}d}" if width else str(value)
    result = re.sub(r"\$(RepresentationID|Bandwidth|Number|Time)(?:%0(\d+)d)?\$", replace, escaped)
    if "$" in result:
        raise DirectError("TIDAL supplied an unsupported DASH segment template.")
    return result.replace("\0", "$")


def _dash_source(raw, quality):
    if len(raw) > 2_000_000 or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise DirectError("TIDAL supplied an unsupported DASH manifest.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DirectError("TIDAL supplied an unreadable DASH manifest.") from exc
    if any(_tag(element).lower() == "contentprotection" for element in root.iter()):
        raise DirectError("TIDAL supplied DRM-protected audio. This app cannot download or decrypt protected streams.")
    periods = _children(root, "Period")
    if _tag(root) != "MPD" or root.get("type", "static") != "static" or len(periods) != 1:
        raise DirectError("This TIDAL DASH stream layout is not supported.")
    period = periods[0]
    candidates = []
    for adaptation in _children(period, "AdaptationSet"):
        for representation in _children(adaptation, "Representation"):
            mime = representation.get("mimeType", adaptation.get("mimeType", ""))
            if adaptation.get("contentType") == "audio" or mime.startswith("audio/"):
                candidates.append((adaptation, representation))
    if not candidates:
        raise DirectError("TIDAL's manifest did not contain an audio representation.")
    adaptation, representation = max(candidates, key=lambda pair: int(pair[1].get("bandwidth", "0")))
    codec = representation.get("codecs", adaptation.get("codecs", "")).lower()
    extension = ".flac" if "flac" in codec else ".m4a" if "mp4a" in codec else None
    if not extension:
        raise DirectError("This TIDAL audio codec is not supported; choose stereo lossless audio.")
    base = ""
    for element in (root, period, adaptation, representation):
        item = _child(element, "BaseURL")
        if item is not None and item.text:
            base = urljoin(base, item.text.strip())
    template = _child(representation, "SegmentTemplate")
    if template is None:
        template = _child(adaptation, "SegmentTemplate")
    if template is None:
        raise DirectError("TIDAL supplied an unsupported DASH segment layout.")
    initialization, media = template.get("initialization"), template.get("media")
    if not initialization or not media:
        raise DirectError("TIDAL's DASH manifest is missing audio segments.")
    urls = [_https_url(urljoin(base, _substitute(initialization, representation)))]
    number = int(template.get("startNumber", "1"))
    timeline = _child(template, "SegmentTimeline")
    if timeline is None:
        raise DirectError("TIDAL's DASH manifest lacks a complete segment timeline.")
    timestamp = 0
    for segment in _children(timeline, "S"):
        timestamp = int(segment.get("t", timestamp))
        duration, repeats = int(segment.get("d", "0")), int(segment.get("r", "0"))
        if duration <= 0 or repeats < 0 or len(urls) + repeats + 1 > 20_000:
            raise DirectError("TIDAL's DASH timeline is unbounded or invalid.")
        for _ in range(repeats + 1):
            urls.append(_https_url(urljoin(base, _substitute(media, representation, number, timestamp))))
            number += 1
            timestamp += duration
    if len(urls) < 2:
        raise DirectError("TIDAL's manifest did not include complete audio segments.")
    return AudioSource(urls, extension, True, quality)


def source_from_stream(stream, track_id):
    if str(stream.track_id) != str(track_id):
        raise DirectError("TIDAL returned a different track's stream; download refused.")
    raw = stream.get_manifest_data()
    quality = str(getattr(stream, "audio_quality", "unknown"))
    if stream.is_mpd:
        # tidalapi 0.8.11 assumes MPD encryption_type=NONE; inspect the XML ourselves.
        return _dash_source(raw, quality)
    if not stream.is_bts:
        raise DirectError("TIDAL supplied an unsupported stream manifest.")
    try:
        manifest = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise DirectError("TIDAL supplied unreadable audio metadata.") from exc
    encryption = manifest.get("encryptionType")
    if not isinstance(encryption, str) or encryption.upper() != "NONE" or manifest.get("keyId"):
        raise DirectError("TIDAL supplied encrypted audio. This app cannot download or decrypt protected streams.")
    codec = str(manifest.get("codecs", "")).upper().split(".")[0]
    extension = {"FLAC": ".flac", "AAC": ".m4a", "MP4A": ".m4a", "MP3": ".mp3"}.get(codec)
    urls = manifest.get("urls")
    if not extension or not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
        raise DirectError("TIDAL supplied an unsupported or incomplete audio manifest.")
    # BTS URLs identify whole-file alternatives; they are not concatenated segments.
    return AudioSource([_https_url(urls[0])], extension, False, quality)


def safe_name(value, limit=96):
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip(" .")
    value = " ".join(value.split())[:limit].rstrip(" .") or "Untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value


def track_metadata(track):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(track.id)):
        raise DirectError("TIDAL returned an invalid track identifier.")
    artists = [str(artist.name) for artist in getattr(track, "artists", []) or [] if getattr(artist, "name", None)]
    if not artists and getattr(track, "artist", None):
        artists = [str(track.artist.name)]
    title = str(getattr(track, "full_name", None) or getattr(track, "title", None) or "")
    if not title or not artists:
        raise DirectError("TIDAL returned a track without its title or artist; refusing an ambiguous file.")
    album = getattr(track, "album", None)
    album_artist = getattr(album, "artist", None)
    release = getattr(album, "release_date", None)
    return {"id": str(track.id), "title": title, "artists": artists,
            "album": str(getattr(album, "name", None) or ""),
            "albumartist": str(getattr(album_artist, "name", None) or artists[0]),
            "tracknumber": int(getattr(track, "track_num", 1) or 1),
            "discnumber": int(getattr(track, "volume_num", 1) or 1),
            "date": release.strftime("%Y-%m-%d") if release else "",
            "isrc": str(getattr(track, "isrc", None) or ""),
            "duration": float(getattr(track, "duration", 0) or 0)}


def _ffmpeg(arguments, timeout=900, capture=False):
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, arguments)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise DirectError("FFmpeg could not validate or convert this audio. Any validated original has been kept.")
    return result.stdout if capture else None


def validate_audio(path, expected_duration=0, decode=True):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 32:
        raise DirectError("The audio file is empty or incomplete.")
    with path.open("rb") as stream:
        header = stream.read(256).lstrip().lower()
    if header.startswith((b"<", b"{", b"[")):
        raise DirectError("The server returned a web page or error response instead of audio.")
    try:
        audio = mutagen.File(path)
        duration = float(audio.info.length) if audio is not None and getattr(audio, "info", None) else 0
    except Exception as exc:
        raise DirectError("The downloaded file could not be identified as valid audio.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise DirectError("The downloaded file did not contain playable audio.")
    if expected_duration > 0 and abs(duration - expected_duration) > max(5.0, expected_duration * 0.02):
        raise DirectError("The audio duration does not match the full TIDAL track; a preview or incomplete file was refused.")
    if decode:
        progress = _ffmpeg(["-xerror", "-i", path, "-map", "0:a:0", "-progress", "pipe:1", "-nostats",
                            "-f", "null", "NUL" if os.name == "nt" else "/dev/null"], capture=True)
        timestamps = re.findall(rb"(?m)^out_time_us=(\d+)\s*$", progress or b"")
        decoded_duration = max((int(value) / 1_000_000 for value in timestamps), default=0)
        # FLAC/MP4 headers can still advertise full duration after truncation.
        if decoded_duration <= 0 or abs(decoded_duration - duration) > 0.5:
            raise DirectError("The audio decoder did not reach the file's declared duration; an incomplete file was refused.")
    return duration


def _write_tags(path, metadata, extension):
    from mutagen.id3 import TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC
    audio = mutagen.File(path, easy=False)
    if audio is None:
        raise DirectError("The audio format could not be tagged.")
    if audio.tags is None:
        audio.add_tags()
    if extension in {".mp3", ".wav"}:
        frames = [TIT2(encoding=3, text=metadata["title"]), TPE1(encoding=3, text=metadata["artists"]),
                  TALB(encoding=3, text=metadata["album"]), TPE2(encoding=3, text=metadata["albumartist"]),
                  TRCK(encoding=3, text=str(metadata["tracknumber"])), TPOS(encoding=3, text=str(metadata["discnumber"]))]
        if metadata["date"]:
            frames.append(TDRC(encoding=3, text=metadata["date"]))
        if metadata["isrc"]:
            frames.append(TSRC(encoding=3, text=metadata["isrc"]))
        for frame in frames:
            audio.tags.add(frame)
    elif extension == ".m4a":
        audio["\xa9nam"] = [metadata["title"]]
        audio["\xa9ART"] = metadata["artists"]
        audio["\xa9alb"] = [metadata["album"]]
        audio["aART"] = [metadata["albumartist"]]
        audio["trkn"] = [(metadata["tracknumber"], 0)]
        audio["disk"] = [(metadata["discnumber"], 0)]
        if metadata["date"]:
            audio["\xa9day"] = [metadata["date"]]
        if metadata["isrc"]:
            audio["----:com.apple.iTunes:ISRC"] = [metadata["isrc"].encode("utf-8")]
    else:
        values = {"title": [metadata["title"]], "artist": metadata["artists"], "album": [metadata["album"]],
                  "albumartist": [metadata["albumartist"]], "tracknumber": [str(metadata["tracknumber"])],
                  "discnumber": [str(metadata["discnumber"])], "tidal_track_id": [metadata["id"]]}
        for key in ("date", "isrc"):
            if metadata[key]:
                values[key] = [metadata[key]]
        audio.update(values)
    audio.save()


def _stream_response(http, url):
    # Validate redirects before sending each request; never follow a downgrade.
    for _ in range(6):
        response = http.get(_https_url(url), stream=True, timeout=(15, 60), allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise DirectError("TIDAL's audio server returned an incomplete redirect.")
        url = _https_url(urljoin(url, location))
    raise DirectError("TIDAL's audio server returned too many redirects.")


def _download_urls(urls, destination, request_session=None, rate_limiter=None):
    own_session = request_session is None
    http = request_session or requests.Session()
    limiter = rate_limiter or _BANDWIDTH_LIMITER
    http.headers.update({"Accept-Encoding": "identity", "User-Agent": "LucidaDesktop/1.0"})
    try:
        with Path(destination).open("wb") as output:
            for url in urls:
                start = output.tell()
                for attempt in range(3):
                    output.seek(start)
                    output.truncate()
                    try:
                        with _stream_response(http, url) as response:
                            if response.status_code != 200:
                                if response.status_code == 429:
                                    raise _rate_limit_error(response.headers.get("Retry-After"))
                                if response.status_code >= 500:
                                    response.raise_for_status()
                                raise DirectError(f"The authenticated audio link returned HTTP {response.status_code}; retry to refresh it.")
                            _https_url(response.url)
                            content_type = response.headers.get("Content-Type", "").lower()
                            if any(part in content_type for part in ("text/", "json", "html", "xml")):
                                raise DirectError("TIDAL's audio server returned a web page or error instead of audio.")
                            received = 0
                            for chunk in response.iter_content(64 * 1024):
                                if not chunk:
                                    continue
                                if received == 0 and chunk[:256].lstrip().lower().startswith((b"<!doctype", b"<html", b"{\"error")):
                                    raise DirectError("TIDAL's audio server returned an error instead of audio.")
                                limiter.consume(len(chunk))
                                output.write(chunk)
                                received += len(chunk)
                            declared = response.headers.get("Content-Length")
                            if received == 0 or (declared and declared.isdigit() and received != int(declared)):
                                raise DirectError("An audio transfer ended before the complete file arrived.")
                            break
                    except requests.RequestException as exc:
                        if attempt == 2:
                            raise DirectError("An audio transfer failed. Retry to refresh its authenticated link.") from exc
                        time.sleep(attempt + 1)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        Path(destination).unlink(missing_ok=True)
        raise
    finally:
        if own_session:
            http.close()


def _atomic_audio(source_path, final_path, metadata, extension, codec="copy", bitrate="320k"):
    final_path = Path(final_path)
    temporary = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex[:8]}.part")
    mux = _NATIVE_MUX.get(extension) or {".wav": "wav", ".opus": "opus", ".ogg": "ogg"}[extension]
    arguments = ["-i", source_path, "-map", "0:a:0", "-vn", "-c:a", codec]
    if codec in {"libmp3lame", "aac", "libopus", "libvorbis"}:
        arguments += ["-b:a", bitrate]
    arguments += ["-f", mux, temporary]
    try:
        _ffmpeg(arguments)
        validate_audio(temporary, metadata["duration"], decode=False)
        _write_tags(temporary, metadata, extension)
        validate_audio(temporary, metadata["duration"])
        os.replace(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)


def _provenance_path(path):
    path = Path(path)
    return path.parent / ".lucida" / (path.name + ".json")


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _request_profile(audio_format, bitrate):
    audio_format = "aac" if audio_format == "m4a" else audio_format
    return {"format": audio_format,
            "bitrate": bitrate if audio_format in {"mp3", "aac", "opus", "ogg"} else None,
            "quality": "LOSSLESS"}


def _write_provenance(path, metadata, source, audio_format, bitrate, is_source):
    path = Path(path)
    record = {"version": 1, "track_id": metadata["id"], "is_source": bool(is_source),
              "source": {"extension": source.extension, "quality": source.quality},
              "request": _request_profile(audio_format, bitrate),
              "size": path.stat().st_size, "sha256": _file_digest(path)}
    destination = _provenance_path(path)
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _existing_matches(path, metadata, audio_format, bitrate):
    if not path.is_file():
        return False
    try:
        record = json.loads(_provenance_path(path).read_text(encoding="utf-8"))
        if (record.get("version") != 1 or record.get("track_id") != metadata["id"]
                or record.get("size") != path.stat().st_size):
            return False
        native = (record.get("is_source") is True and record.get("source", {}).get("extension") == path.suffix
                  and record.get("request", {}).get("quality") == "LOSSLESS")
        if audio_format == "original":
            if not native:
                return False
        elif audio_format == "flac" and native and path.suffix == ".flac":
            pass  # An original FLAC already fulfills a lossless FLAC request.
        elif record.get("request") != _request_profile(audio_format, bitrate):
            return False
        if record.get("sha256") != _file_digest(path):
            return False
        validate_audio(path, metadata["duration"])
        return True
    except (DirectError, OSError, ValueError, TypeError, AttributeError):
        return False


def _remove_audio(path):
    path.unlink()
    _provenance_path(path).unlink(missing_ok=True)


def _kept_original_matches(candidate, stem, metadata, bitrate):
    try:
        record = json.loads(_provenance_path(candidate).read_text(encoding="utf-8"))
        extension = record.get("source", {}).get("extension")
        if extension not in _NATIVE_MUX:
            return False
        suffix = ".original" if record.get("is_source") is not True and candidate.suffix == extension else ""
        original = candidate.parent / (stem + suffix + extension)
        return _existing_matches(original, metadata, "original", bitrate)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def download_track(track, directory, position, total, options, session_lock):
    metadata = track_metadata(track)
    label = f"{', '.join(metadata['artists'])} - {metadata['title']}"
    prefix = f"[{position:0{max(3, len(str(total)))}d}/{total}] {label}"
    stem = f"{position:0{max(3, len(str(total)))}d} {safe_name(label, 78)} [{metadata['id']}]"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    wanted = options.to
    extension = ".m4a" if wanted in {"aac", "m4a"} else f".{wanted}"
    candidates = [directory / (stem + ext) for ext in (".flac", ".m4a", ".mp3")] if wanted == "original" else [directory / (stem + extension)]
    for candidate in candidates:
        if _existing_matches(candidate, metadata, wanted, options.bitrate):
            if (options.keep_original and wanted != "original"
                    and not _kept_original_matches(candidate, stem, metadata, options.bitrate)):
                continue
            log(f"{prefix} — already complete ({candidate.suffix[1:].upper()})")
            return candidate, metadata, True
    log(f"{prefix} — requesting account audio")
    with session_lock:
        if getattr(track, "available", True) is False:
            raise DirectError("This exact track is unavailable for the signed-in account or region.")
        _TRACK_PACER.wait()
        try:
            source = source_from_stream(track.get_stream(), track.id)
        except Exception as exc:
            if _is_rate_limit(exc):
                raise _throttle_from_exception(exc) from exc
            raise
    convert = wanted != "original" and not (wanted == "flac" and source.extension == ".flac")
    original_suffix = ".original" if convert and extension == source.extension else ""
    original = directory / (stem + original_suffix + source.extension)
    raw = directory / f".{stem}.{uuid.uuid4().hex[:8]}.stream.part"
    try:
        if not _existing_matches(original, metadata, "original", options.bitrate):
            _download_urls(source.urls, raw)
            _atomic_audio(raw, original, metadata, source.extension)
            _write_provenance(original, metadata, source, "original", options.bitrate, is_source=True)
        if not convert:
            log(f"{prefix} — saved {source.extension[1:].upper()} ({source.quality})")
            return original, metadata, False
        target = directory / (stem + extension)
        codec, _mux = _OUTPUTS[wanted]
        # Conversion is atomic; the validated original survives any failure.
        _atomic_audio(original, target, metadata, extension, codec, options.bitrate)
        _write_provenance(target, metadata, source, wanted, options.bitrate, is_source=False)
        if not options.keep_original:
            _remove_audio(original)
        log(f"{prefix} — saved {wanted.upper()} ({source.quality} source)")
        return target, metadata, False
    finally:
        raw.unlink(missing_ok=True)


def write_playlist(path, results):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.part")
    lines = ["#EXTM3U"]
    for audio_path, metadata, _skipped in results:
        label = f"{', '.join(metadata['artists'])} - {metadata['title']}".replace("\r", " ").replace("\n", " ")
        lines += [f"#EXTINF:{round(metadata['duration'])},{label}", Path(os.path.relpath(audio_path, path.parent)).as_posix()]
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_collection(collection, options, session_lock):
    directory = Path(options.out)
    if not options.flat:
        suffix = f" [{collection.identifier[:8]}]" if collection.identifier else ""
        directory /= safe_name(collection.name, 50) + suffix
    total = len(collection.tracks)
    log(f"TIDAL collection: {collection.name} — {total} exact tracks")
    if options.dry_run:
        for index, track in enumerate(collection.tracks, 1):
            metadata = track_metadata(track)
            log(f"{index:03d}. {', '.join(metadata['artists'])} - {metadata['title']} [TIDAL {metadata['id']}]")
        log("Preview complete; no audio downloaded.")
        return 0, 0, 0
    results, failures, skipped = {}, 0, 0
    rate_error = None
    for index, track in enumerate(collection.tracks, 1):
        try:
            results[index] = download_track(track, directory, index, total, options, session_lock)
            skipped += bool(results[index][2])
        except Exception as exc:
            failures += 1
            if isinstance(exc, RateLimitError) or _is_rate_limit(exc):
                rate_error = exc if isinstance(exc, RateLimitError) else _throttle_from_exception(exc)
                log(f"[{index:03d}/{total}] {rate_error}")
                log(f"Stopped with {total - index} remaining tracks unattempted. Completed files are retained.")
                break
            log(f"[{index:03d}/{total}] FAILED: {safe_error(exc)}")
    counts = (len(results) - skipped, skipped, failures)
    if rate_error is not None:
        # Preserve the stop signal even if the destination is full or unwritable.
        # Completed audio is already durable; playlist writes can wait for retry.
        rate_error.counts = counts
        raise rate_error
    if collection.playlist:
        playlist_path = directory / (safe_name(collection.name, 70) + ".m3u8")
        if failures:
            # A previous complete playlist remains intact; partial results are explicit.
            if results:
                partial = playlist_path.with_name(playlist_path.stem + ".partial.m3u8")
                write_playlist(partial, [results[index] for index in sorted(results)])
                log(f"Partial playlist written with {len(results)} of {total} tracks; the complete playlist was not replaced.")
        else:
            directory.mkdir(parents=True, exist_ok=True)
            write_playlist(playlist_path, [results[index] for index in range(1, total + 1)])
            log(f"Saved playlist: {playlist_path.name} ({total} entries, original order)")
    return counts


def parser():
    result = argparse.ArgumentParser(description="Download exact TIDAL items through your signed-in account.")
    commands = result.add_subparsers(dest="kind", required=True)
    for kind in ("tracks", "albums", "playlist"):
        command = commands.add_parser(kind)
        if kind == "playlist":
            command.add_argument("url")
            command.add_argument("--dry-run", action="store_true")
        else:
            command.add_argument("--file", required=True)
            command.set_defaults(dry_run=False)
        command.add_argument("--out", default=str(Path.cwd() / "downloads"))
        command.add_argument("--jobs", type=int, choices=range(1, 9), default=3)
        command.add_argument("--to", choices=sorted(_FORMATS), default="original")
        command.add_argument("--bitrate", default="320k")
        command.add_argument("--keep-original", action="store_true")
        command.add_argument("--flat", action="store_true")
    return result


def main(argv=None):
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="replace")
    options = parser().parse_args(argv)
    logging.getLogger("tidalapi").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    session = None
    result = 1
    totals = [0, 0, 0]
    try:
        if not re.fullmatch(r"[1-9][0-9]{1,3}k", options.bitrate):
            raise DirectError("Choose a valid audio bitrate, such as 320k.")
        if options.kind == "playlist":
            inputs = [options.url]
        else:
            inputs = [line.strip() for line in Path(options.file).read_text(encoding="utf-8-sig").splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
        if not inputs:
            raise DirectError("Add at least one exact TIDAL link.")
        # Reject mismatched/non-TIDAL inputs before loading any account data.
        for value in inputs:
            tidal_reference(value, {"tracks": "track", "albums": "album", "playlist": "playlist"}[options.kind])
        from tidal_account import load_session, save_session
        session = load_session()
        session.config.quality = Quality.high_lossless
        collections = resolve_collections(session, options.kind, inputs)
        lock = threading.Lock()
        log("TIDAL conservative mode: one transfer, 256 KiB/s, at least 5 seconds between track requests.")
        for collection in collections:
            counts = run_collection(collection, options, lock)
            totals = [old + new for old, new in zip(totals, counts)]
        log(f"Summary: {totals[0]} downloaded, {totals[1]} already complete, {totals[2]} failed.")
        result = 1 if totals[2] else 0
    except KeyboardInterrupt:
        log("Download interrupted. Completed files remain available; retry the queue to continue.")
        result = 130
    except RateLimitError as exc:
        totals = [old + new for old, new in zip(totals, exc.counts)]
        log(str(exc))
        log(f"Summary: {totals[0]} downloaded, {totals[1]} already complete, {totals[2]} failed; remaining work stopped.")
        result = 75
    except Exception as exc:
        log(f"TIDAL: {safe_error(exc)}")
        result = 75 if _is_rate_limit(exc) else 1
    finally:
        if session is not None:
            try:
                from tidal_account import save_session
                save_session(session)
            except Exception as exc:
                log(f"Could not save the account session: {safe_error(exc)}")
                if result != 75:
                    result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
