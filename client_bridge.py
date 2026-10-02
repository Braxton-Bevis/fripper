"""Run lucidadl with public TIDAL track/album metadata translation.

TIDAL links identify the music. Audio comes from the provider matched by
lucidadl; this adapter does not claim to download audio directly from TIDAL.
"""
from __future__ import annotations

from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

from lucidadl import api, downloader
from lucidadl.api import LucidaError


_TIDAL_HOSTS = {"tidal.com", "www.tidal.com", "listen.tidal.com", "embed.tidal.com"}
_KINDS = {"track": "track", "tracks": "track", "album": "album", "albums": "album"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


def tidal_reference(value: str, expected_kind: str | None = None):
    """Return (kind, numeric ID) only for supported official TIDAL item URLs."""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host not in _TIDAL_HOSTS:
        return None
    if (parsed.scheme.lower() not in {"http", "https"} or parsed.username or
            parsed.password):
        raise LucidaError("Invalid TIDAL link: use a public https://tidal.com/track/ID or /album/ID link.")
    try:
        if parsed.port not in (None, 80, 443):
            raise ValueError("nonstandard port")
    except ValueError as exc:
        raise LucidaError("Invalid TIDAL link: custom ports are not supported.") from exc
    parts = parsed.path.strip("/").split("/")
    if parts and parts[0].lower() == "browse":
        parts = parts[1:]
    if parts and parts[0].lower() in {"playlist", "playlists"}:
        return None  # The upstream public-playlist importer owns these.
    if not parts or parts[0].lower() not in _KINDS:
        raise LucidaError("Unsupported TIDAL link: paste a track or album link, or use Playlist mode.")
    kind = _KINDS[parts[0].lower()]
    if len(parts) != 2 or not re.fullmatch(r"[1-9][0-9]{0,19}", parts[1]):
        raise LucidaError(f"Invalid TIDAL {kind} ID: paste the full public {kind} link with a numeric ID.")
    if expected_kind is not None and kind != expected_kind:
        raise LucidaError(f"This is a TIDAL {kind} link. Choose {kind.title()}s mode for this item.")
    return kind, parts[1]


class _EmbedParser(HTMLParser):
    """Read the item heading and its artist credits, excluding album track rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.captures = []
        self.titles = []
        self.artists = []
        self.products = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tidal-play-trigger":
            self.products.add((attrs.get("product-type"), attrs.get("product-id")))
        if tag in _VOID_TAGS:
            return
        self.stack.append(tag)
        classes = (attrs.get("class") or "").split()
        if tag == "h1" and ("media-title" in classes or "media-album" in classes):
            self.captures.append({"depth": len(self.stack), "type": "title", "text": [], "links": []})
        elif "media-artist" in classes:
            self.captures.append({"depth": len(self.stack), "type": "artist", "text": [], "links": []})
        if tag == "a":
            for capture in self.captures:
                if capture["type"] == "artist":
                    capture["links"].append({"depth": len(self.stack), "text": []})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        for capture in self.captures:
            capture["text"].append(data)
            for link in capture["links"]:
                if link["depth"] <= len(self.stack):
                    link["text"].append(data)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        self.stack = self.stack[:index]
        finished = [capture for capture in self.captures if capture["depth"] > len(self.stack)]
        for capture in finished:
            clean = lambda parts: " ".join("".join(parts).split())
            if capture["type"] == "title":
                self.titles.append(clean(capture["text"]))
            else:
                names = [clean(link["text"]) for link in capture["links"]]
                self.artists.append(", ".join(dict.fromkeys(name for name in names if name)) or clean(capture["text"]))
            self.captures.remove(capture)
        for capture in self.captures:
            for link in capture["links"]:
                if link["depth"] > len(self.stack):
                    link["depth"] = 10**9  # finished anchor; do not append later siblings


def parse_tidal_metadata(raw: str, kind: str, item_id: str) -> dict[str, str]:
    parser = _EmbedParser()
    parser.feed(raw)
    parser.close()
    titles = {text for text in parser.titles if text}
    artists = {text for text in parser.artists if text}
    if (kind, item_id) not in parser.products:
        raise LucidaError("TIDAL returned a different or unavailable item; check the link and region.")
    if len(titles) != 1 or len(artists) != 1:
        raise LucidaError("TIDAL metadata is missing or ambiguous; cannot safely match this item. Check the link or retry later.")
    title, artist = next(iter(titles)), next(iter(artists))
    if " - " in artist:
        # Upstream splits this delimiter to separate the artist from the title.
        raise LucidaError("This TIDAL artist name contains the search separator ' - '; use a direct supported-provider link to avoid a wrong match.")
    return {"title": title, "artist": artist}


async def tidal_metadata(kind: str, item_id: str) -> dict[str, str]:
    url = f"https://embed.tidal.com/{kind}s/{item_id}"
    try:
        response = await api._public_get(url)
        final = urlsplit(str(response.url))
        if final.scheme != "https" or final.hostname != "embed.tidal.com":
            raise LucidaError("TIDAL metadata redirected outside its official embed host.")
        return parse_tidal_metadata(response.text, kind, item_id)
    except LucidaError:
        raise
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else "network request failed"
        raise LucidaError(f"Could not read public TIDAL {kind} metadata ({detail}). Check the link, region, and connection, then retry.") from exc


def make_resolver(original):
    async def resolve(client, line, service, kind, log, strict=False, quiet=False):
        reference = tidal_reference(line, kind)
        if reference is None:
            return await original(client, line, service, kind, log, strict, quiet=quiet)
        item_kind, item_id = reference
        metadata = await tidal_metadata(item_kind, item_id)
        query = f"{metadata['artist']} - {metadata['title']}"
        log(f"TIDAL {item_kind}: {query}; matching audio through {service}.")
        result = await original(client, query, service, kind, log, strict, quiet=False)
        if not result:
            raise LucidaError(f"No confident audio-provider match for TIDAL {item_kind} '{query}'. Try another provider or a direct provider link.")
        source = urlsplit(result).hostname or service
        log(f"TIDAL link matched audio source: {source}")
        return result
    return resolve


def main():
    from lucida_search import install_patch as install_search_patch
    install_search_patch()
    from tidal_playlists import install_patch
    install_patch()
    from lucidadl.cli import cli
    downloader._resolve_url = make_resolver(downloader._resolve_url)
    cli()


if __name__ == "__main__":
    main()
