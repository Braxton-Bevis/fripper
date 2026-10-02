"""Read complete public TIDAL playlists when their embed is unavailable.

The fallback uses a fresh anonymous browser context and only the public metadata
requests made by TIDAL's own page. It never opens an existing browser profile.
"""
from __future__ import annotations

from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from lucidadl import api
from lucidadl.api import LucidaError


_HOSTS = {"tidal.com", "www.tidal.com", "listen.tidal.com", "browse.tidal.com", "embed.tidal.com"}
_API_HOSTS = {"tidal.com", "api.tidal.com"}
_MAX_ITEMS = 10_000


def _playlist_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0] == "browse":
            parts = parts[1:]
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname in _HOSTS
                 and parsed.username is None and parsed.password is None
                 and parsed.port in {None, 80, 443} and len(parts) == 2
                 and parts[0] in {"playlist", "playlists"}
                 and re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", parts[1]))
    except ValueError:
        valid = False
    if not valid:
        raise LucidaError("Paste the full public TIDAL playlist link with its playlist ID.")
    return parts[1].lower()


class _MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "meta":
            values = dict(attrs)
            key = values.get("property") or values.get("name")
            if key:
                self.meta[key.lower()] = values.get("content") or ""


def _public_metadata(raw: str) -> tuple[str, int]:
    parser = _MetadataParser()
    parser.feed(raw)
    parser.close()
    name = " ".join(parser.meta.get("og:title", "").split())
    description = parser.meta.get("og:description") or parser.meta.get("description", "")
    count = re.search(r"\b(\d[\d,]*)\s+(?:items?|tracks?|songs?)\b", description, re.I)
    if not name or not count:
        raise LucidaError("TIDAL did not expose a public playlist title and item count. Make the playlist public, then retry.")
    total = int(count.group(1).replace(",", ""))
    if total > _MAX_ITEMS:
        raise LucidaError(f"This playlist has {total} items; public import supports up to {_MAX_ITEMS}.")
    return name, total


def _items_endpoint(url: str, playlist_id: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in _API_HOSTS
            and parsed.path == f"/v1/playlists/{playlist_id}/items"
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 443})


def _page_tracks(data: dict, expected_offset: int, expected_total: int):
    if not isinstance(data, dict):
        raise LucidaError("TIDAL returned unreadable public playlist metadata.")
    for field, expected in (("offset", expected_offset), ("totalNumberOfItems", expected_total)):
        actual = data.get(field)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            raise LucidaError("TIDAL's playlist count or page position changed while reading. Retry to get a complete list.")
    tracks, total, rows, skipped = api._tidal_items_from_obj(data)
    if skipped:
        raise LucidaError(f"TIDAL returned {skipped} non-music or unavailable item(s); refusing to import an incomplete playlist.")
    if rows != len(tracks) or expected_offset + rows > expected_total:
        raise LucidaError("TIDAL returned inconsistent playlist positions; no partial list was imported.")
    if rows == 0 and expected_offset < total:
        raise LucidaError(f"TIDAL exposed only {expected_offset} of {total} playlist items. Make the playlist public and retry.")
    for track, row in zip(tracks, data["items"]):
        item_id = str(row["item"].get("id", ""))
        if re.fullmatch(r"[1-9][0-9]{0,19}", item_id):
            track["id"] = item_id
            track["url"] = f"https://tidal.com/track/{item_id}"
    return tracks, rows


async def _read_pages(request, first_response, playlist_id: str, expected_total: int, log):
    if not _items_endpoint(first_response.url, playlist_id):
        raise LucidaError("TIDAL's public playlist request went to an unexpected endpoint.")
    if first_response.status != 200:
        raise LucidaError(f"TIDAL's public playlist is unavailable (HTTP {first_response.status}). Make it public, wait a moment, then retry.")
    headers = await first_response.request.all_headers()
    token = headers.get("x-tidal-token", "")
    if not token:
        raise LucidaError("TIDAL's anonymous playlist session did not become available. Retry shortly.")
    parsed = urlsplit(first_response.url)
    country = (parse_qs(parsed.query).get("countryCode") or ["US"])[0]
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise LucidaError("TIDAL returned an invalid public playlist region.")
    endpoint = f"https://{parsed.hostname}{parsed.path}"
    public_headers = {"X-Tidal-Token": token, "Referer": f"https://tidal.com/playlist/{playlist_id}", "Accept": "application/json"}
    data = await first_response.json()
    tracks, read = _page_tracks(data, 0, expected_total)
    while read < expected_total:
        response = await request.get(
            endpoint, params={"offset": read, "limit": 50, "countryCode": country,
                              "locale": "en_US", "deviceType": "BROWSER"},
            headers=public_headers, timeout=30_000, max_redirects=0)
        if response.status != 200 or not _items_endpoint(response.url, playlist_id):
            raise LucidaError(f"TIDAL exposed only {read} of {expected_total} playlist items (HTTP {response.status}). Retry; no partial list was imported.")
        more, size = _page_tracks(await response.json(), read, expected_total)
        tracks.extend(more)  # Positions and intentional duplicate songs are preserved.
        read += size
        if read % 100 == 0:
            log(f"  Read {read} of {expected_total} public TIDAL items.")
    if len(tracks) != expected_total:
        raise LucidaError(f"TIDAL exposed only {len(tracks)} of {expected_total} music tracks.")
    return tracks


async def _browser_tracklist(playlist_id: str, expected_total: int, log):
    from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(locale="en-US")
            page = await context.new_page()
            try:
                async with page.expect_response(
                    lambda response: _items_endpoint(response.url, playlist_id), timeout=30_000
                ) as pending:
                    await page.goto(f"https://tidal.com/playlist/{playlist_id}",
                                    wait_until="domcontentloaded", timeout=45_000)
                first_response = await pending.value
                return await _read_pages(context.request, first_response, playlist_id, expected_total, log)
            except BrowserTimeout as exc:
                raise LucidaError("TIDAL's public playlist did not load in time. Make it public, check the connection, and retry.") from exc
        finally:
            await browser.close()


async def _fallback(url: str, log):
    playlist_id = _playlist_id(url)
    canonical = f"https://tidal.com/playlist/{playlist_id}"
    try:
        response = await api._public_get(canonical, {"Accept-Language": "en-US"})
    except httpx.HTTPStatusError as exc:
        raise LucidaError(f"TIDAL's playlist is unavailable (HTTP {exc.response.status_code}). Make it public, then retry.") from exc
    parsed = urlsplit(str(response.url))
    if parsed.scheme != "https" or parsed.hostname not in {"tidal.com", "www.tidal.com"}:
        raise LucidaError("TIDAL redirected playlist metadata outside its public website.")
    name, total = _public_metadata(response.text)
    if not total:
        return name, []
    log(f"  Reading all {total} public TIDAL items in a separate anonymous browser.")
    tracks = await _browser_tracklist(playlist_id, total, log)
    log(f"  Verified complete TIDAL playlist: {name} ({len(tracks)} items).")
    return name, tracks


def make_tracklist(original):
    async def tidal_tracklist(url: str, log=print):
        _playlist_id(url)
        try:
            return await original(url, log)
        except api.TidalPlaylistWindow:
            pass
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {401, 403, 404, 410}:
                raise
        except LucidaError as exc:
            if "public playlist data was not found" not in str(exc):
                raise
        log("  TIDAL's embed is unavailable or limited; checking its public playlist page.")
        return await _fallback(url, log)
    return tidal_tracklist


def install_patch():
    """Install once, retaining the upstream fast embed implementation."""
    if not hasattr(api, "_desktop_original_tidal_tracklist"):
        api._desktop_original_tidal_tracklist = api.tidal_tracklist
        api.tidal_tracklist = make_tracklist(api.tidal_tracklist)
