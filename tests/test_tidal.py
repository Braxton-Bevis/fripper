"""TIDAL metadata and matching regressions; network is mocked."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import client_bridge as bridge
from lucidadl.api import LucidaError


def embed(kind="track", item_id="123", title="Song (Live)", artists=None):
    artists = artists or ["Earth, Wind &amp; Fire", "AC/DC"]
    heading = "media-title" if kind == "track" else "media-album"
    links = ", ".join(f'<a href="https://tidal.com/artist/{i}">{name}</a>'
                      for i, name in enumerate(artists, 1))
    return (f'<div><tidal-play-trigger product-type="{kind}" product-id="{item_id}">'
            f'<img src="cover.jpg"></tidal-play-trigger><header>'
            f'<h1 class="{heading}"><a href="#">{title}</a></h1></header>'
            f'<span class="media-artist">{links}</span>'
            '<list-item product-type="track"><span slot="title">Other song</span>'
            '<span slot="artist"><a>Other artist</a></span></list-item></div>')


class TidalMetadataTests(unittest.TestCase):
    def test_supported_forms_normalize_kind_and_id(self):
        for url in ("https://tidal.com/track/123?u=share", "https://listen.tidal.com/track/123/",
                    "https://www.tidal.com/browse/track/123", "https://embed.tidal.com/tracks/123"):
            with self.subTest(url=url):
                self.assertEqual(bridge.tidal_reference(url, "track"), ("track", "123"))
        self.assertEqual(bridge.tidal_reference("https://tidal.com/browse/album/456"), ("album", "456"))

    def test_other_providers_and_playlist_are_unchanged(self):
        for value in ("Artist - Song", "https://music.amazon.com/albums/B001",
                      "https://tidal.com.evil.example/track/123", "https://eviltidal.com/track/123",
                      "https://tidal.com/playlist/abc"):
            with self.subTest(value=value):
                self.assertIsNone(bridge.tidal_reference(value))

    def test_malformed_ids_and_unsafe_urls_are_rejected(self):
        for value in ("https://tidal.com/track/xyz", "https://tidal.com/track/0",
                      "https://tidal.com/album/123/extra", "https://tidal.com/track/",
                      "https://tidal.com/track/%31", "https://tidal.com/track/-12",
                      "https://name:password@tidal.com/track/123",
                      "https://tidal.com:8080/track/123", "ftp://tidal.com/track/123"):
            with self.subTest(value=value), self.assertRaises(LucidaError):
                bridge.tidal_reference(value)

    def test_wrong_mode_is_explicit(self):
        with self.assertRaisesRegex(LucidaError, "Choose Albums mode"):
            bridge.tidal_reference("https://tidal.com/album/123", "track")

    def test_track_preserves_full_artist_names_and_version(self):
        result = bridge.parse_tidal_metadata(embed(), "track", "123")
        self.assertEqual(result, {"title": "Song (Live)", "artist": "Earth, Wind & Fire, AC/DC"})

    def test_album_uses_album_heading_and_main_credits(self):
        result = bridge.parse_tidal_metadata(embed("album", "456", "Album (Deluxe Edition)",
                                                 ["Artist One", "Artist Two"]), "album", "456")
        self.assertEqual(result, {"title": "Album (Deluxe Edition)", "artist": "Artist One, Artist Two"})

    def test_wrong_product_cannot_be_matched(self):
        with self.assertRaisesRegex(LucidaError, "different or unavailable"):
            bridge.parse_tidal_metadata(embed(), "track", "456")

    def test_missing_and_ambiguous_metadata_rejected(self):
        for raw in (embed().replace('class="media-artist"', 'class="missing"'),
                    embed() + '<h1 class="media-title">Different Song</h1>'):
            with self.subTest(raw=raw), self.assertRaisesRegex(LucidaError, "missing or ambiguous"):
                bridge.parse_tidal_metadata(raw, "track", "123")

    def test_artist_with_upstream_query_delimiter_rejected(self):
        with self.assertRaisesRegex(LucidaError, "search separator"):
            bridge.parse_tidal_metadata(embed(artists=["Artist - Stage Name"]), "track", "123")


class TidalResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_arbitrary_url_does_not_fetch_metadata(self):
        original = AsyncMock(return_value="https://example.test/audio")
        client, log = object(), lambda _: None
        resolver = bridge.make_resolver(original)
        with patch.object(bridge, "tidal_metadata", new_callable=AsyncMock) as metadata:
            value = await resolver(client, "https://example.test/audio", "amazon", "track", log, True, quiet=True)
        self.assertEqual(value, "https://example.test/audio")
        metadata.assert_not_awaited()
        original.assert_awaited_once_with(client, "https://example.test/audio", "amazon", "track", log, True, quiet=True)

    async def test_tidal_query_uses_selected_provider_and_reports_actual_source(self):
        original = AsyncMock(return_value="https://music.amazon.com/tracks/B123")
        client, logs = object(), []
        with patch.object(bridge, "tidal_metadata", new_callable=AsyncMock,
                          return_value={"artist": "First & Second, Third", "title": "Song (Acoustic)"}):
            result = await bridge.make_resolver(original)(client, "https://tidal.com/track/123", "amazon", "track", logs.append)
        original.assert_awaited_once_with(client, "First & Second, Third - Song (Acoustic)",
                                         "amazon", "track", logs.append, False, quiet=False)
        self.assertEqual(result, "https://music.amazon.com/tracks/B123")
        self.assertIn("music.amazon.com", logs[-1])

    async def test_album_match_uses_album_bucket(self):
        original = AsyncMock(return_value="https://open.qobuz.com/album/example")
        with patch.object(bridge, "tidal_metadata", new_callable=AsyncMock,
                          return_value={"artist": "Artist", "title": "Album (Remastered)"}):
            await bridge.make_resolver(original)(None, "https://tidal.com/album/123", "qobuz", "album", lambda _: None)
        self.assertEqual(original.await_args.args[1:4], ("Artist - Album (Remastered)", "qobuz", "album"))

    async def test_ambiguous_match_is_failure_not_tidal_url_passthrough(self):
        original = AsyncMock(return_value=None)
        with patch.object(bridge, "tidal_metadata", new_callable=AsyncMock,
                          return_value={"artist": "Artist", "title": "Song"}), \
                self.assertRaisesRegex(LucidaError, "No confident audio-provider match"):
            await bridge.make_resolver(original)(None, "https://tidal.com/track/123", "amazon", "track", lambda _: None)

    async def test_public_metadata_fetch_and_host_validation(self):
        response = SimpleNamespace(url="https://embed.tidal.com/tracks/123", text=embed())
        with patch.object(bridge.api, "_public_get", new_callable=AsyncMock, return_value=response) as fetch:
            result = await bridge.tidal_metadata("track", "123")
        fetch.assert_awaited_once_with("https://embed.tidal.com/tracks/123")
        self.assertEqual(result["title"], "Song (Live)")
        response.url = "https://elsewhere.example/tracks/123"
        with patch.object(bridge.api, "_public_get", new_callable=AsyncMock, return_value=response), \
                self.assertRaisesRegex(LucidaError, "outside its official embed host"):
            await bridge.tidal_metadata("track", "123")

    async def test_http_errors_are_actionable(self):
        response = httpx.Response(404, request=httpx.Request("GET", "https://embed.tidal.com/tracks/123"))
        error = httpx.HTTPStatusError("not found", request=response.request, response=response)
        with patch.object(bridge.api, "_public_get", new_callable=AsyncMock, side_effect=error), \
                self.assertRaisesRegex(LucidaError, r"metadata \(HTTP 404\)"):
            await bridge.tidal_metadata("track", "123")

    async def test_timeout_is_actionable(self):
        with patch.object(bridge.api, "_public_get", new_callable=AsyncMock, side_effect=httpx.ReadTimeout("timeout")), \
                self.assertRaisesRegex(LucidaError, "network request failed"):
            await bridge.tidal_metadata("album", "123")


if __name__ == "__main__":
    unittest.main()
